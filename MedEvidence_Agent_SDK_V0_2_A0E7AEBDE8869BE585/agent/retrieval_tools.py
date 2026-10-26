"""Retriever adapters for MedEvidence snapshot indexes.

For v3_2_trial_clean, Qdrant stores only chunk metadata/embeddings; the
matching SQLite ``chunks`` table is the canonical source of full text.
Both retrieval paths return the same stable chunk_id. Reads are read-only.
"""
from __future__ import annotations
import importlib
import json
import os
import re
import sqlite3
import threading
from functools import wraps
from pathlib import Path
from typing import Any
from .config import Config, resolve_project_path
from .models import Evidence


class RetrievalError(RuntimeError):
    pass


def _serialized_retrieval(method):
    """Serialize use of the shared read-only SQLite handle and local Qdrant.

    The SDK can execute multiple tools on worker threads. RLock also supports
    nested search -> lookup_document and search -> _sparse/_lookup_chunks.
    Older offline fixtures created via __new__ do not have this lock and are
    intentionally left alone (their usage is single-threaded).
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_retrieval_lock", None)
        if lock is None:
            return method(self, *args, **kwargs)
        with lock:
            return method(self, *args, **kwargs)
    return wrapper


def _get(data: dict, *names: str) -> str:
    """First non-empty string field across top-level and common nested metadata."""
    for d in (data, data.get("metadata", {}), data.get("meta", {}), data.get("payload", {})):
        if not isinstance(d, dict):
            continue
        for n in names:
            v = d.get(n)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
    return ""


def normalize(item: Any, score: float = 0.0, source: str = "hook") -> Evidence | None:
    if isinstance(item, Evidence):
        return item if item.text.strip() else None
    if not isinstance(item, dict):
        return None
    body = _get(item, "text", "content", "chunk_text", "page_content", "body", "passage", "paragraph", "document", "body_text")
    if not body:
        return None
    ident = _get(item, "chunk_id", "id", "doc_id", "point_id", "node_id")
    if not ident:
        raise RetrievalError("检索结果缺少 chunk_id / id，无法建立可验证引用；请在检索适配器中返回稳定 id")
    raw_score = item.get("score", score)
    try:
        raw_score = float(raw_score)
    except (ValueError, TypeError):
        raw_score = score
    return Evidence(
        id=ident, text=body, title=_get(item, "title", "paper_title", "document_title"),
        pmcid=_get(item, "pmcid", "PMCID"), doi=_get(item, "doi", "DOI"),
        year=_get(item, "year", "publication_year"), section=_get(item, "section", "section_title", "section_path"),
        score=raw_score, source=source,
    )


class HookRetriever:
    """Import existing retrieval function: function(query: str, top_k: int) -> list[dict]."""
    def __init__(self, path: str):
        if ":" not in path:
            raise RetrievalError("MEDEVIDENCE_RETRIEVER_HOOK 必须是 module:function，例如 my_search:retrieve")
        mod, name = path.rsplit(":", 1)
        try:
            fn = getattr(importlib.import_module(mod), name)
        except (ImportError, AttributeError) as exc:
            raise RetrievalError(f"不能导入检索函数 {path}: {exc}") from exc
        if not callable(fn):
            raise RetrievalError("检索 Hook 不是可调用函数")
        self.fn = fn

    @_serialized_retrieval
    def search(self, query: str, top_k: int) -> list[Evidence]:
        result = self.fn(query, top_k=top_k)
        if not isinstance(result, list):
            raise RetrievalError("检索 Hook 应返回 list[dict]，字段至少有 chunk_id、text")
        out = []
        for x in result:
            y = normalize(x)
            if y:
                out.append(y)
        return out[:top_k]


def discover_indices(root: Path) -> dict[str, list[str]]:
    qdrant, sqlite = [], []
    ignore = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
    for base, dirs, files in os.walk(root):
        level = len(Path(base).relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in ignore] if level < 7 else []
        if "meta.json" in files and ("collection" in dirs or "collections" in dirs or "storage.sqlite" in files):
            qdrant.append(str(Path(base)))
        for f in files:
            if f.lower().endswith((".sqlite", ".sqlite3", ".db")) and f != "storage.sqlite":
                sqlite.append(str(Path(base) / f))
    return {"qdrant_candidates": qdrant[:40], "sqlite_candidates": sqlite[:60]}


def extract_explicit_title(query: str) -> str:
    """Recognize a user-named paper, without treating ordinary search text as a title.

    This is a routing hint, not proof that the named document is indexed.
    """
    q = query.strip()
    for pattern in (
        r"《\s*([^》]{15,180})\s*》",
        r"[\"“]\s*([^\"”]{15,180})\s*[\"”]\s*(?:这篇|这项|该|一篇)?(?:研究|论文|文献|文章)?",
        r"(?:根据|关于|基于|阅读|查找|分析)\s*([A-Za-z][^\n。？！?]{15,180}?)\s*(?:这篇|这项|该|一篇|一项)(?:研究|论文|文献|文章)",
    ):
        hit = re.search(pattern, q, flags=re.IGNORECASE)
        if hit:
            title = hit.group(1).strip(" \t\n,，:：'\"“”")
            if len(re.findall(r"[A-Za-z]{2,}", title)) >= 4:
                return title
    return ""


def _normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.casefold()))


def _focus_terms(focus: str) -> tuple[str, ...]:
    # Console line wraps and accidental spaces may split Chinese phrases (局 限性).
    focus = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", focus.casefold())
    if any(w in focus for w in ("局限", "限制", "缺陷", "limitations", "limit of", "bias", "caveat")):
        return ("limitations", "limitation", "limited by", "study limitation", "bias", "confounding", "retrospective", "control group", "small sample", "generalizability", "selection bias")
    if any(w in focus for w in ("结论", "conclusion")):
        return ("conclusion", "conclude", "in summary")
    if any(w in focus for w in ("方法", "method")):
        return ("methods", "study design", "methodology")
    return ()


class LocalQdrantRetriever:
    """Uses the existing Qdrant collection, and optional SQLite FTS5 with RRF.

    Qdrant and SQLite files are never created or modified by this adapter.
    Avoid opening the same Qdrant local DB from multiple processes.
    """
    def __init__(self, cfg: Config):
        # A single retriever instance is shared by the SDK's async tool calls.
        # Serialize its read-only SQLite handle and local Qdrant/encoder use.
        self._retrieval_lock = threading.RLock()
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise RetrievalError("缺少 qdrant-client，执行 pip install -r requirements-agent.txt") from exc
        raw_path = cfg.qdrant_path
        if not raw_path:
            candidates = discover_indices(cfg.root)["qdrant_candidates"]
            if len(candidates) == 1:
                raw_path = candidates[0]
            else:
                raise RetrievalError(f"无法唯一确定 Qdrant 目录（找到 {len(candidates)} 个）。运行 --probe 并在 .env.agent 设置 MEDEVIDENCE_QDRANT_PATH")
        path = resolve_project_path(cfg.root, raw_path)
        if not path.is_dir():
            raise RetrievalError(f"Qdrant 存储目录不存在: {path}")
        try:
            self.client = QdrantClient(path=str(path))
            names = [c.name for c in self.client.get_collections().collections]
        except Exception as exc:
            raise RetrievalError(f"Qdrant 打开失败（是否被其他进程锁定？）: {exc}") from exc
        if cfg.collection not in names:
            raise RetrievalError(f"Qdrant collection {cfg.collection!r} 不存在；实际集合: {names}")
        self.collection = cfg.collection
        self.model_name = cfg.embed_model
        self.device = cfg.device
        self.encoder = None
        self.encode_kind = ""
        self.fts = None
        self.fts_table = ""
        self.chunks_table = ""  # canonical text table of the same index version
        if cfg.bm25_db:
            db = resolve_project_path(cfg.root, cfg.bm25_db)
            if not db.is_file():
                raise RetrievalError(f"SQLite BM25/FTS 文件不存在: {db}")
            try:
                # Required: SDK calls this read-only connection inside asyncio.to_thread().
                # Disabling sqlite's thread-affinity check is safe here ONLY
                # because every retrieval path is protected by the RLock above.
                self.fts = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True,
                                           check_same_thread=False)
                self.fts.execute("PRAGMA query_only = ON")
                rows = self.fts.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
                tables = [x[0] for x in rows if x[1] and "using fts5" in x[1].lower()]
                if not tables:
                    raise RetrievalError(f"SQLite 中未发现 FTS5 虚拟表: {db}。请用 hook 模式调用原来的 BM25 检索")
                self.fts_table = next((t for t in tables if "chunk" in t.lower() or "fts" in t.lower()), tables[0])
                self.chunks_table = self._find_chunks_table()
            except sqlite3.Error as exc:
                self.fts.close()
                self.fts = None
                raise RetrievalError(f"SQLite FTS5 初始化失败: {exc}") from exc

    def _encode(self, query: str) -> list[float]:
        if self.encoder is None:
            try:
                from FlagEmbedding import BGEM3FlagModel
                self.encoder = BGEM3FlagModel(self.model_name, use_fp16=self.device.startswith("cuda"), devices=self.device)
                self.encode_kind = "flag"
            except ImportError:
                try:
                    from sentence_transformers import SentenceTransformer
                    self.encoder = SentenceTransformer(self.model_name, device=self.device)
                    self.encode_kind = "st"
                except ImportError as exc:
                    raise RetrievalError("缺少 BGE-M3 依赖，安装 FlagEmbedding 或 sentence-transformers") from exc
        if self.encode_kind == "flag":
            v = self.encoder.encode([query], batch_size=1, max_length=512)["dense_vecs"][0]
        else:
            v = self.encoder.encode([query], normalize_embeddings=True)[0]
        return [float(x) for x in v]

    @staticmethod
    def _quoted(name: str) -> str:
        """Quote known SQLite identifiers, even if a caller uses a custom FTS table."""
        return '"' + name.replace('"', '""') + '"'

    def _find_chunks_table(self) -> str:
        """Detect a canonical chunk table; never modify the database schema."""
        if self.fts is None:
            return ""
        names = [r[0] for r in self.fts.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        preferred = ["chunks"] + [n for n in names if n != "chunks"]
        for name in preferred:
            if name not in names:
                continue
            cols = {r[1] for r in self.fts.execute(
                f"PRAGMA table_info({self._quoted(name)})"
            ).fetchall()}
            if {"id", "chunk_id", "body_text"}.issubset(cols):
                return name
        return ""

    @_serialized_retrieval
    def _lookup_chunks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Hydrate dense hits by exact chunk_id using the same-version SQLite DB.

        A PMID/document ID or FTS row number must never stand in for chunk_id.
        Lookup is batched and read-only. Unknown IDs are not silently invented.
        """
        if not ids or self.fts is None or not getattr(self, "chunks_table", ""):
            return {}
        unique_ids = list(dict.fromkeys(x for x in ids if x))
        name = self._quoted(self.chunks_table)
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 400):
            batch = unique_ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            try:
                rows = self.fts.execute(
                    f"SELECT * FROM {name} WHERE chunk_id IN ({marks})", batch
                )
                keys = [column[0] for column in rows.description]
                for row in rows.fetchall():
                    data = dict(zip(keys, row))
                    found[str(data["chunk_id"])] = data
            except sqlite3.Error as exc:
                raise RetrievalError(f"chunk_id -> SQLite 正文关联失败: {exc}") from exc
        return found

    @_serialized_retrieval
    def _dense(self, query: str, limit: int) -> list[Evidence]:
        vec = self._encode(query)
        try:
            response = self.client.query_points(collection_name=self.collection, query=vec,
                                                limit=limit, with_payload=True)
            points = response.points
        except AttributeError:
            try:
                points = self.client.search(collection_name=self.collection, query_vector=vec,
                                            limit=limit, with_payload=True)
            except Exception as exc:
                raise RetrievalError(f"Qdrant 向量检索失败: {exc}") from exc
        except Exception as exc:
            raise RetrievalError(f"Qdrant 向量检索失败（检查向量维度和 collection）: {exc}") from exc

        payloads = [dict(point.payload or {}) for point in points]
        # Fetch complete evidence using the exact real IDs already stored in Qdrant.
        matches = self._lookup_chunks([_get(p, "chunk_id") for p in payloads])
        out = []
        for point, payload in zip(points, payloads):
            chunk_id = _get(payload, "chunk_id")
            if chunk_id in matches:
                # The chunk database is authoritative for title/text/section;
                # Qdrant supplies the immutable id and retrieval score.
                merged = {**payload, **matches[chunk_id]}
                merged["chunk_id"] = chunk_id
                merged["score"] = float(point.score)
            else:
                # Supports older full-text-in-payload collections, without
                # inventing a citation ID from Qdrant's numeric point ID.
                merged = {**payload, "score": float(point.score)}
            if not _get(merged, "chunk_id"):
                continue
            e = normalize(merged, float(point.score), "qdrant_dense")
            if e:
                if not e.pmcid:
                    doc_id = _get(merged, "document_id")
                    if doc_id.upper().startswith("PMC"):
                        e.pmcid = doc_id
                out.append(e)
        if points and not out:
            if not self.fts or not getattr(self, "chunks_table", ""):
                reason = ("未配置同版本 MEDEVIDENCE_BM25_DB，或 SQLite 中不存在 "
                          "chunks(id, chunk_id, body_text) 表")
            else:
                reason = "Qdrant 命中的 chunk_id 无法在同版本 SQLite chunks 表中关联到非空 body_text"
            raise RetrievalError(
                f"Qdrant 返回 {len(points)} 个向量命中，但无法读取正文：{reason}。"
                "请检查 Qdrant/SQLite 是否都指向 v3_2_trial_clean。"
            )
        return out

    @_serialized_retrieval
    def lookup_document(self, title: str, top_k: int = 6, focus: str = "") -> list[Evidence]:
        """Locate a *named* paper using its canonical SQLite title, not broad OR search.

        All reads stay on the same-version read-only `chunks` table. A title
        substring must have four English words (or >=8 CJK chars). Unknown
        titles return [] -- never hallucinate a publication or citation ID.
        """
        if self.fts is None or not getattr(self, "chunks_table", ""):
            return []
        needle = _normalized_title(title)
        if not needle or (len(re.findall(r"[a-z]{2,}", needle)) < 4 and len(needle) < 8):
            return []
        table = self._quoted(self.chunks_table)
        # A small list of distinct titles (922 papers in this snapshot), so
        # normalized punctuation/hyphen variants can be matched without FTS.
        if not hasattr(self, "_known_titles"):
            self._known_titles = [row[0] for row in self.fts.execute(
                f"SELECT DISTINCT title FROM {table} WHERE title IS NOT NULL AND title != ''"
            ).fetchall()]
        titles = [t for t in self._known_titles if needle in _normalized_title(t)]
        if not titles:
            return []
        # Ambiguous fragments should not be mistaken for a unique paper.
        if len(titles) > 3:
            return []
        placeholders = ",".join("?" for _ in titles)
        cursor = self.fts.execute(
            f"SELECT * FROM {table} WHERE title IN ({placeholders}) AND body_text IS NOT NULL AND length(trim(body_text)) > 0",
            titles,
        )
        keys = [c[0] for c in cursor.description]
        rows = [dict(zip(keys, values)) for values in cursor.fetchall()]
        terms = _focus_terms(focus)
        def relevance(row: dict) -> tuple[float, int]:
            text = str(row.get("body_text") or "").casefold()
            first = text[:260]
            score = 0.0
            for term in terms:
                if term in text:
                    score += 2.0
                    if term in first:
                        score += 6.0
            if "limitations" in terms and first.startswith(("several limitations", "limitations", "this study has")):
                score += 12.0
            # Prefer narrative text over a figure caption if equally relevant.
            if ":parent:body:" in str(row.get("chunk_id", "")):
                score += 1.0
            return (score, len(text))
        rows.sort(key=relevance, reverse=True)
        out = []
        for row in rows[:max(1, min(30, top_k))]:
            e = normalize(row, source="sqlite_title_lookup")
            if e:
                doc_id = _get(row, "document_id")
                if doc_id.upper().startswith("PMC"):
                    e.pmcid = doc_id
                out.append(e)
        return out

    @_serialized_retrieval
    def _sparse(self, query: str, limit: int) -> list[Evidence]:
        if self.fts is None:
            return []
        # FTS5 unicode61 indexes English terms; short CJK sequences may need
        # a different tokenizer, so dense remains the main CJK retrieval path.
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}|[\u3400-\u9fff]{2,}", query)[:12]
        if not tokens:
            return []
        expression = " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)
        name = self._quoted(self.fts_table)
        table = getattr(self, "chunks_table", "")
        try:
            if table:
                chunk_table = self._quoted(table)
                # External-content FTS5: rowid is the primary key of chunks.
                # JOIN ensures the result has the *same* chunk_id as dense.
                cursor = self.fts.execute(
                    f"SELECT c.* FROM {name} "
                    f"JOIN {chunk_table} AS c ON c.id = {name}.rowid "
                    f"WHERE {name} MATCH ? ORDER BY bm25({name}) LIMIT ?",
                    (expression, limit),
                )
            else:
                # Compatibility for self-contained FTS tables in older tests.
                cursor = self.fts.execute(
                    f"SELECT rowid, * FROM {name} WHERE {name} MATCH ? LIMIT ?",
                    (expression, limit),
                )
            keys = [col[0] for col in cursor.description]
            data = [dict(zip(keys, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise RetrievalError(f"BM25 FTS5 SQL 查询失败: {exc}；请核对 SQLite 表结构") from exc
        out = []
        for rank, row in enumerate(data, 1):
            if table:
                # Do not use an fts5:rowid synthetic citation for the real corpus.
                if not _get(row, "chunk_id"):
                    continue
            elif not _get(row, "chunk_id", "id", "doc_id", "point_id", "node_id"):
                row["id"] = f"fts5:{self.fts_table}:{row['rowid']}"
            e = normalize(row, 1.0 / rank, "sqlite_fts5")
            if e:
                if not e.pmcid:
                    doc_id = _get(row, "document_id")
                    if doc_id.upper().startswith("PMC"):
                        e.pmcid = doc_id
                out.append(e)
        return out

    def search(self, query: str, top_k: int) -> list[Evidence]:
        title = extract_explicit_title(query)
        if title:
            exact = self.lookup_document(title, top_k=top_k, focus=query)
            if exact:
                return exact
        dense, sparse = self._dense(query, top_k * 2), self._sparse(query, top_k * 2)
        scores: dict[str, float] = {}
        docs: dict[str, Evidence] = {}
        for group in (dense, sparse):
            for rank, e in enumerate(group, 1):
                scores[e.id] = scores.get(e.id, 0.0) + 1.0 / (60.0 + rank)
                if e.id not in docs or len(e.text) > len(docs[e.id].text):
                    docs[e.id] = e
        ordered = sorted(docs, key=lambda x: -scores[x])[:top_k]
        for doc_id in ordered:
            docs[doc_id].score = round(scores[doc_id], 7)
        return [docs[x] for x in ordered]


def make_retriever(cfg: Config):
    if cfg.retriever == "hook":
        return HookRetriever(cfg.hook)
    if cfg.retriever == "qdrant":
        return LocalQdrantRetriever(cfg)
    raise RetrievalError("MEDEVIDENCE_RETRIEVER 仅支持 qdrant 或 hook")
