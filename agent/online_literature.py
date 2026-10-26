"""Abstract-level online literature search (PubMed, optional Europe PMC).

Design constraints:
- read-only HTTP, no full-text storage for non-OA records;
- every request is rate-limited, cached on disk and replayable;
- returned text is untrusted input for the model, exactly like local chunks;
- failures raise OnlineLiteratureError so the caller can report a run failure
  instead of pretending that no evidence exists.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "MedEvidence/0.3.1 (research; abstract-level retrieval)"
PUBMED_MIN_INTERVAL = 0.34      # 3 req/s without API key
PUBMED_MIN_INTERVAL_KEY = 0.11  # 10 req/s with API key
EUROPEPMC_MIN_INTERVAL = 1.0
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def has_cjk(text: str) -> bool:
    """True when a query contains Chinese characters (PubMed needs English terms)."""
    return bool(_CJK_RE.search(text or ""))


class OnlineLiteratureError(RuntimeError):
    """Safe, concise online-search failure (no keys, no raw bodies)."""


@dataclass(frozen=True)
class OnlineHit:
    source: str
    source_id: str
    title: str
    abstract: str
    year: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    license: str = ""
    is_retracted: bool = False
    url: str = ""
    retrieved_at: str = ""

    def dedup_keys(self) -> list[str]:
        keys = []
        if self.pmcid:
            keys.append("pmcid:" + self.pmcid.lower())
        if self.pmid:
            keys.append("pmid:" + self.pmid.lower())
        if self.doi:
            keys.append("doi:" + self.doi.lower())
        if self.title:
            keys.append("title:" + _normalized_title(self.title))
        return keys


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def _normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:120]


class _Throttle:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class _Http:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/xml, text/xml",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise OnlineLiteratureError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OnlineLiteratureError("网络连接失败") from exc


class PubMedClient:
    def __init__(self, http: _Http, email: str = "", api_key: str = ""):
        self.http = http
        self.email = email
        self.api_key = api_key
        self.throttle = _Throttle(PUBMED_MIN_INTERVAL_KEY if api_key else PUBMED_MIN_INTERVAL)
        self.base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

    def _params(self, extra: dict[str, Any]) -> str:
        params = {"tool": "MedEvidence", "retmode": "json"}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        params.update(extra)
        return urllib.parse.urlencode(params)

    def search(self, query: str, limit: int, publication_types: str = "") -> list[OnlineHit]:
        term = query if not publication_types else f"({query}) AND ({publication_types})"
        search_url = self.base + "esearch.fcgi?" + self._params({
            "db": "pubmed", "term": term, "retmax": limit, "sort": "relevance",
        })
        self.throttle.wait()
        payload = json.loads(self.http.get(search_url).decode("utf-8", "replace"))
        ids = payload.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        fetch_url = self.base + "efetch.fcgi?" + self._params({
            "db": "pubmed", "id": ",".join(ids), "retmode": "xml",
        })
        self.throttle.wait()
        return self._parse(self.http.get(fetch_url))

    @staticmethod
    def _parse(raw: bytes) -> list[OnlineHit]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OnlineLiteratureError("PubMed 返回的 XML 无法解析") from exc
        hits: list[OnlineHit] = []
        for article in root.findall(".//PubmedArticle"):
            pmid = (article.findtext(".//PMID") or "").strip()
            title_node = article.find(".//ArticleTitle")
            title = _clean("".join(title_node.itertext())) if title_node is not None else ""
            abstract = _clean(" ".join(
                "".join(node.itertext()) for node in article.findall(".//Abstract/AbstractText")))
            if not abstract:
                continue
            year = (article.findtext(".//PubDate/Year") or "").strip()
            if not year:
                medline = article.findtext(".//PubDate/MedlineDate") or ""
                match = re.search(r"(19|20)\d{2}", medline)
                year = match.group(0) if match else ""
            doi = pmcid = ""
            for node in article.findall(".//ArticleIdList/ArticleId"):
                kind = (node.get("IdType") or "").lower()
                if kind == "doi":
                    doi = (node.text or "").strip()
                elif kind == "pmc":
                    pmcid = (node.text or "").strip()
            types = {(node.text or "").lower() for node in article.findall(".//PublicationType")}
            refs = {(node.get("RefType") or "").lower()
                    for node in article.findall(".//CommentsCorrectionsList/CommentsCorrections")}
            retracted = "retracted publication" in types or "retractionin" in refs
            hits.append(OnlineHit(
                source="pubmed", source_id=pmid or doi, title=title, abstract=abstract,
                year=year, doi=doi, pmid=pmid, pmcid=pmcid, is_retracted=retracted,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                retrieved_at=_now()))
        return hits


class EuropePmcClient:
    def __init__(self, http: _Http):
        self.http = http
        self.throttle = _Throttle(EUROPEPMC_MIN_INTERVAL)
        self.base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"

    def search(self, query: str, limit: int, publication_types: str = "") -> list[OnlineHit]:
        if publication_types:
            query = f"({query}) AND (PUB_TYPE:\"guideline\" OR PUB_TYPE:\"review\")"
        url = self.base + urllib.parse.urlencode({
            "query": query, "format": "json", "pageSize": limit,
            "resultType": "core", "sort": "CITED desc",
        })
        self.throttle.wait()
        payload = json.loads(self.http.get(url).decode("utf-8", "replace"))
        results = payload.get("resultList", {}).get("result", [])
        hits: list[OnlineHit] = []
        for item in results:
            abstract = _clean(item.get("abstractText") or "")
            if not abstract:
                continue
            pmid = str(item.get("pmid") or "")
            pmcid = str(item.get("pmcid") or "")
            doi = str(item.get("doi") or "")
            year = str(item.get("pubYear") or "")
            if not year:
                year = str((item.get("journalInfo") or {}).get("yearOfPublication") or "")
            pub_type = item.get("pubType") or []
            if isinstance(pub_type, str):
                pub_type = [pub_type]
            types = {str(x).lower() for x in pub_type}
            corrections = item.get("commentCorrectionList") or {}
            if isinstance(corrections, dict):
                corrections = corrections.get("commentCorrection") or []
            refs = {str((x or {}).get("type") or "").lower() for x in corrections}
            retracted = "retracted publication" in types or "retraction in" in refs
            if pmid:
                url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            elif pmcid:
                url = f"https://europepmc.org/article/PMC/{pmcid}"
            else:
                url = ""
            hits.append(OnlineHit(
                source="europepmc", source_id=pmid or pmcid or str(item.get("id") or ""),
                title=_clean(str(item.get("title") or "")), abstract=abstract, year=year,
                doi=doi, pmid=pmid, pmcid=pmcid, license=str(item.get("license") or ""),
                is_retracted=retracted, url=url, retrieved_at=_now()))
        return hits


class OnlineLiteratureService:
    """Search one or more sources, deduplicate and cache the result set."""

    def __init__(self, *, cache_dir: Path, sources: str = "pubmed", timeout: int = 25,
                 ttl_hours: int = 168, email: str = "", api_key: str = ""):
        self.cache_dir = Path(cache_dir) / "search"
        self.sources = [s.strip() for s in sources.split(",") if s.strip()] or ["pubmed"]
        self.ttl_seconds = max(1, ttl_hours) * 3600
        self.http = _Http(timeout)
        self.clients: dict[str, Any] = {}
        if "pubmed" in self.sources:
            self.clients["pubmed"] = PubMedClient(self.http, email=email, api_key=api_key)
        if "europepmc" in self.sources:
            self.clients["europepmc"] = EuropePmcClient(self.http)

    def search(self, query: str, limit: int,
               publication_types: str = "") -> tuple[list[OnlineHit], dict[str, Any]]:
        query = query.strip()
        if not query:
            raise OnlineLiteratureError("查询为空")
        cached = self._read_cache(query, limit, publication_types)
        if cached is not None:
            return cached, {"cached": True, "sources": list(self.clients)}
        hits: list[OnlineHit] = []
        seen: set[str] = set()
        warnings: list[str] = []
        errors: list[str] = []
        for name, client in self.clients.items():
            try:
                found = client.search(query, limit, publication_types)
            except OnlineLiteratureError as exc:
                errors.append(f"{name}: {exc}")
                continue
            for hit in found:
                keys = hit.dedup_keys()
                if any(key in seen for key in keys):
                    continue
                seen.update(keys)
                hits.append(hit)
            if len(hits) >= limit:
                break
        hits = hits[:limit]
        if not hits and errors:
            raise OnlineLiteratureError("；".join(errors))
        if errors:
            warnings.extend(errors)
        self._write_cache(query, limit, hits, publication_types)
        return hits, {"cached": False, "sources": list(self.clients), "warnings": warnings}

    def _cache_path(self, query: str, limit: int, publication_types: str = "") -> Path:
        digest = hashlib.sha256(
            f"{query}|{limit}|{','.join(self.sources)}|{publication_types}".encode("utf-8")
        ).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, query: str, limit: int,
                    publication_types: str = "") -> list[OnlineHit] | None:
        path = self._cache_path(query, limit, publication_types)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        created = float(data.get("created_at", 0))
        if time.time() - created > self.ttl_seconds:
            return None
        try:
            return [OnlineHit(**item) for item in data.get("hits", [])]
        except TypeError:
            return None

    def _write_cache(self, query: str, limit: int, hits: list[OnlineHit],
                     publication_types: str = "") -> None:
        path = self._cache_path(query, limit, publication_types)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "created_at": time.time(), "query": query, "limit": limit,
                "publication_types": publication_types,
                "sources": self.sources, "hits": [asdict(hit) for hit in hits],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
