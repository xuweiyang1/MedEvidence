#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Metadata discovery for V1a.2. Only sends a user-confirmed TITLE, never PDF bytes.

Returns suggestions, NOT authenticated document identities. Explicit confirmation
is mandatory before saving staging metadata. No corpus/DB/index writes.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CROSSREF = "https://api.crossref.org/works"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
NCBI_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def norm_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    # Separators can differ in PDFs, preprints and metadata records.
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def title_similarity(left: str, right: str) -> float:
    a, b = norm_title(left), norm_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Jaccard and sequence ratio jointly guard against a few matching keywords.
    aw, bw = set(a.split()), set(b.split())
    jaccard = len(aw & bw) / max(1, len(aw | bw))
    seq = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return round(0.55 * seq + 0.45 * jaccard, 4)


def norm_doi(raw: str) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi\s*:\s*", "", text)
    text = text.rstrip(".,; ")
    return text if re.fullmatch(r"10\.\d{4,9}/[^\s<>\"']+", text) else ""


def norm_pmid(raw: str) -> str:
    text = re.sub(r"^PMID\s*:\s*", "", str(raw or "").strip(), flags=re.I)
    return text if re.fullmatch(r"[1-9]\d{0,9}", text) else ""


def norm_pmcid(raw: str) -> str:
    match = re.fullmatch(r"(?:PMC)?\s*(\d+)", str(raw or "").strip(), re.I)
    return "PMC" + match.group(1) if match else ""


def year_value(raw) -> str:
    found = re.search(r"(?:19|20)\d{2}", str(raw or ""))
    return found.group() if found else ""


def _get_json(url: str, params: dict, *, email: str = "", opener=None) -> dict:
    url = url + "?" + urlencode(params)
    agent = "MedEvidence-MetadataLookup/1.2"
    if email:
        agent += f" (mailto:{email})"
    opener = opener or urlopen
    for attempt in range(2):
        try:
            request = Request(url, headers={"User-Agent": agent, "Accept": "application/json"})
            with opener(request, timeout=18) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("远程书目元数据响应超过 2 MB 限制")
            data = json.loads(payload.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("远程接口返回了意外的数据格式")
            return data
        except HTTPError as exc:
            if attempt == 0 and exc.code in (429, 500, 502, 503, 504):
                time.sleep(1.5)
                continue
            raise RuntimeError(f"远程服务 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 0:
                time.sleep(1.5)
                continue
            raise RuntimeError(f"远程服务连接超时/失败：{exc}") from exc
    raise RuntimeError("远程查询失败")


def _make_candidate(source: str, source_title: str, query_title: str, *,
                    doi: str = "", pmid: str = "", pmcid: str = "",
                    year: str = "", authors: str = "", journal: str = "") -> dict:
    return {
        "source": source,
        "title": re.sub(r"\s+", " ", str(source_title or "")).strip()[:500],
        "doi": norm_doi(doi), "pmid": norm_pmid(pmid), "pmcid": norm_pmcid(pmcid),
        "year": year_value(year), "authors": str(authors or "")[:240],
        "journal": str(journal or "")[:160],
        "similarity": title_similarity(query_title, source_title),
    }


def crossref_search(title: str, *, email: str = "", fetch=_get_json) -> list[dict]:
    params = {"query.bibliographic": title, "rows": 6,
              "select": "DOI,title,author,issued,container-title,type"}
    if email:
        params["mailto"] = email
    data = fetch(CROSSREF, params, email=email)
    out = []
    for it in (data.get("message", {}).get("items") or []):
        if not isinstance(it, dict):
            continue
        names = []
        for author in (it.get("author") or [])[:2]:
            if isinstance(author, dict):
                names.append(" ".join(str(author.get(k) or "") for k in ("given", "family")).strip())
        date_parts = ((it.get("issued") or {}).get("date-parts") or [[]])[0]
        out.append(_make_candidate(
            "Crossref", (it.get("title") or [""])[0], title,
            doi=it.get("DOI", ""), year=date_parts[0] if date_parts else "",
            authors=", ".join(names), journal=(it.get("container-title") or [""])[0],
        ))
    return out


def europe_pmc_search(title: str, *, fetch=_get_json) -> list[dict]:
    # Escape query operators supplied in PDF title; search TITLE field only.
    literal = re.sub(r"[\"\\\[\]{}()]", " ", title).strip()
    data = fetch(EUROPE_PMC, {"query": f'TITLE:"{literal}"', "format": "json",
                                  "resultType": "lite", "pageSize": 8})
    out = []
    for it in ((data.get("resultList") or {}).get("result") or []):
        if not isinstance(it, dict):
            continue
        out.append(_make_candidate(
            "Europe PMC", it.get("title", ""), title, doi=it.get("doi", ""),
            pmid=it.get("id", "") if it.get("source") == "MED" else it.get("pmid", ""),
            pmcid=it.get("pmcid", ""), year=it.get("pubYear", ""),
            authors=it.get("authorString", ""), journal=it.get("journalTitle", ""),
        ))
    return out


def pubmed_search(title: str, *, email: str, api_key: str = "", fetch=_get_json) -> list[dict]:
    """Direct PubMed E-utilities (optional when NCBI_EMAIL is configured)."""
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email or ""):
        raise ValueError("未设置 NCBI_EMAIL，已跳过 PubMed 直连；Europe PMC 仍能返回 PMID")
    literal = re.sub(r"[\"\[\]()]", " ", title).strip()
    basic = {"tool": "MedEvidence", "email": email, "retmode": "json"}
    if api_key:
        basic["api_key"] = api_key
    found = fetch(NCBI_SEARCH, {**basic, "db": "pubmed", "term": f'"{literal}"[Title]', "retmax": 5}, email=email)
    ids = [norm_pmid(x) for x in ((found.get("esearchresult") or {}).get("idlist") or [])]
    ids = [x for x in ids if x][:5]
    if not ids:
        return []
    time.sleep(0.4)  # Respect NCBI's unauthenticated 3-requests/sec guideline.
    data = fetch(NCBI_SUMMARY, {**basic, "db": "pubmed", "id": ",".join(ids)}, email=email)
    records = data.get("result") or {}
    out = []
    for pid in ids:
        it = records.get(pid) or {}
        if not isinstance(it, dict) or not it.get("title"):
            continue
        identifiers = {str(x.get("idtype") or "").lower(): x.get("value", "")
                       for x in (it.get("articleids") or []) if isinstance(x, dict)}
        out.append(_make_candidate(
            "PubMed", re.sub(r"<[^>]+>", "", it.get("title", "")), title,
            doi=identifiers.get("doi") or it.get("elocationid", ""),
            pmid=pid, pmcid=identifiers.get("pmc", ""),
            year=it.get("pubdate", ""),
            authors=", ".join(str(a.get("name") or "") for a in (it.get("authors") or [])[:2] if isinstance(a, dict)),
            journal=it.get("fulljournalname", "") or it.get("source", ""),
        ))
    return out


def lookup_by_title(title: str, *, email: str = "", ncbi_email: str = "",
                    ncbi_api_key: str = "", fetch=_get_json) -> dict:
    """No data writes, non-fatal per-provider errors, no result claimed verified."""
    title = re.sub(r"\s+", " ", title or "").strip()
    if title.startswith("（") or title.startswith("(") or not 12 <= len(title) <= 500:
        raise ValueError("请先核对并填写完整论文标题（12-500 字符），不能用占位标题搜索")
    warnings, candidates = [], []
    for provider, fn in (
        ("Crossref", lambda: crossref_search(title, email=email, fetch=fetch)),
        ("Europe PMC", lambda: europe_pmc_search(title, fetch=fetch)),
    ):
        try:
            candidates.extend(fn())
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            warnings.append(f"{provider} 查询失败：{type(exc).__name__}: {exc}")
    if ncbi_email:
        try:
            candidates.extend(pubmed_search(title, email=ncbi_email,
                                            api_key=ncbi_api_key, fetch=fetch))
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
            warnings.append(f"PubMed 查询失败：{type(exc).__name__}: {exc}")
    else:
        warnings.append("未配置 NCBI_EMAIL，跳过 PubMed 直连；Europe PMC 可提供 PubMed 记录及 PMID")
    ranked = [c for c in candidates if c["title"] and c["similarity"] >= 0.68]
    ranked.sort(key=lambda x: (x["similarity"], bool(x["pmcid"]), bool(x["pmid"])), reverse=True)
    ranked = ranked[:12]
    for c in ranked:
        sim = c["similarity"]
        c["match_label"] = "高度一致（仍需核对）" if sim >= 0.97 else ("较相似（需核对）" if sim >= 0.88 else "低匹配度（谨慎使用）")
    return {"query_title": title, "candidates": ranked, "warnings": warnings,
            "searched_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")}
