#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence Pilot evidence-review core. Standard library only.

Input text is untrusted source data, never instructions. Preserve exact source
segments for quotation audit. Human labels are stored separately from AI cards.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DB_FILE = ROOT / "data/review/evidence_review_v1.sqlite3"
CORPUS = ROOT / "data/processed/knowledge/v3_1_all/retrieval_chunks.jsonl"
PARENTS = ROOT / "data/processed/knowledge/v3_1_all/parent_units.jsonl"
DOCS = ROOT / "data/processed/knowledge/v3_1_all/documents.jsonl"
INPUT_CANDIDATES = (
    REPORTS / "pilot_annotation_24_AI预标注_非Gold.csv",
    REPORTS / "pilot_annotation_24.csv",
)
PILOT_KEYS = (
    ("C001", "PMC13627872"),
    ("R001", "PMC13643946"),
    ("M001", "PMC13637635"),
)
LABELS = ("2", "1", "0", "UNCERTAIN")
DIRECTNESS = ("DIRECT", "INDIRECT", "BACKGROUND", "UNCERTAIN")
DESIGNS = ("RCT", "OBSERVATIONAL", "SECONDARY_ANALYSIS", "REVIEW", "GUIDELINE", "PROTOCOL", "CASE_SERIES", "CASE_REPORT", "ECONOMIC_MODEL", "OTHER", "UNKNOWN")
MATCHES = ("YES", "PARTIAL", "NO", "UNCERTAIN")
PAPER_SUPPORT = ("YES", "PARTIAL", "NO", "UNCERTAIN")
QUOTE_STATES = ("QUOTE_VERIFIED", "QUOTE_PARTIAL", "QUOTE_NOT_FOUND")
ID_KEYS = ("chunk_id", "retrieval_chunk_id", "evidence_chunk_id", "id", "parent_id", "parent_unit_id")
DOC_KEYS = ("document_id", "pmcid", "paper_id", "article_id", "document_pmcid", "doc_id")
TEXT_KEYS = ("text", "content", "chunk_text", "retrieval_text", "text_for_embedding", "embedding_text", "child_text", "passage", "plain_text", "body_text", "text_content", "evidence_text", "retrieval_content", "chunk_content", "embedding_input", "passage_text", "content_text", "normalized_text", "parent_text")
SEC_KEYS = ("section", "section_path", "section_title", "heading")


def pick(obj: dict, keys: tuple[str, ...]) -> str:
    for name in keys:
        value = obj.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, dict):
            for inner in ("text", "content", "value", "id", "name"):
                v = value.get(inner)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def read_jsonl(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"未找到医学语料文件：{path}")
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 格式错误：{path}:{line_no}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"JSONL 行不是对象：{path}:{line_no}")
            yield obj


def read_candidates(path: Path) -> dict[tuple[str, str], dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for row in rows:
        qid = (row.get("question_id") or row.get("query_id") or "").strip()
        did = (row.get("document_id") or "").strip()
        if not qid or not did:
            raise ValueError("候选 CSV 缺少 question_id 或 document_id")
        if (qid, did) in out:
            raise ValueError(f"重复问题–论文对 {qid}/{did}")
        out[(qid, did)] = row
    return out


def candidate_file() -> Path:
    for p in INPUT_CANDIDATES:
        if p.is_file():
            return p
    raise FileNotFoundError("未发现试点 CSV，请将 pilot_annotation_24.csv 放到 reports 下")


def doc_id(raw: dict, id_value: str = "") -> str:
    did = pick(raw, DOC_KEYS)
    if did:
        return did
    head = id_value.split(":", 1)[0]
    return head if re.fullmatch(r"PMC\d+", head, re.I) else ""


def parent_identifier(cid: str, obj: dict) -> str:
    direct = pick(obj, ("parent_id", "parent_unit_id", "parent_chunk_id", "parent_ref"))
    return direct or cid.split(":child:", 1)[0]


def scan_corpus(doc_ids: set[str]) -> tuple[dict, dict]:
    chunks: dict[str, list] = {d: [] for d in doc_ids}
    parent_refs: dict[str, set] = {d: set() for d in doc_ids}
    for raw in read_jsonl(CORPUS):
        cid = pick(raw, ID_KEYS)
        did = doc_id(raw, cid)
        if did not in doc_ids:
            continue
        txt = pick(raw, TEXT_KEYS)
        if not cid or not txt:
            continue
        item = {
            "raw_id": cid,
            "text": txt,
            "kind": pick(raw, ("content_kind", "kind", "chunk_kind", "type")) or "unknown",
            "section": pick(raw, SEC_KEYS),
            "origin": "retrieval_chunk",
        }
        chunks[did].append(item)
        parent_refs[did].add(parent_identifier(cid, raw))
    return chunks, parent_refs


def scan_parents(doc_ids: set[str], parent_refs: dict[str, set]) -> dict:
    parents = {d: [] for d in doc_ids}
    if not PARENTS.exists():
        return parents
    for raw in read_jsonl(PARENTS):
        pid = pick(raw, ("parent_id", "parent_unit_id", "parent_chunk_id", "id"))
        did = doc_id(raw, pid)
        if did not in doc_ids or not pid or pid not in parent_refs.get(did, set()):
            continue
        txt = pick(raw, TEXT_KEYS)
        if txt:
            parents[did].append({
                "raw_id": pid, "text": txt, "kind": pick(raw, ("content_kind", "kind", "type")) or "parent",
                "section": pick(raw, SEC_KEYS), "origin": "parent_unit",
            })
    return parents


def scan_metadata(doc_ids: set[str]) -> dict:
    out = {d: {} for d in doc_ids}
    if not DOCS.exists():
        return out
    for raw in read_jsonl(DOCS):
        did = doc_id(raw, pick(raw, ("id",)))
        if did in doc_ids:
            out[did] = {
                "title": pick(raw, ("title", "article_title", "document_title")),
                "abstract": pick(raw, ("abstract", "abstract_text", "summary")),
                "doi": pick(raw, ("doi", "DOI")),
                "pmid": pick(raw, ("pmid", "PMID")),
            }
    return out


def score_context(item: dict, query: str) -> float:
    text = item["text"]
    section = (item.get("section", "") + " " + item.get("kind", "")).lower()
    s = 0.0
    if "abstract" in section:
        s += 4
    if "method" in section or "design" in section:
        s += 5
    if "result" in section or "outcome" in section:
        s += 5
    if "table" in section:
        s += 3
    qwords = {w for w in re.findall(r"[a-z0-9]+", query.casefold()) if len(w) > 2}
    # Bounded to prevent long paragraphs dominating selection.
    overlap = sum(1 for w in qwords if w in text.casefold())
    return s + min(overlap, 8) * 1.5


def select_context(row: dict, chunks: list[dict], parents: list[dict], meta: dict, max_sources: int = 12, max_chars: int = 19000) -> dict:
    qid, did = row["question_id"], row["document_id"]
    identifiers = [x.strip() for x in (row.get("support_chunk_ids") or "").split(";") if x.strip()]
    best_id = (row.get("best_chunk_id") or "").strip()
    if best_id and best_id not in identifiers:
        identifiers.insert(0, best_id)
    priorities = []
    for ch in chunks:
        if ch["raw_id"] in identifiers:
            priorities.append(ch)
    parent_keys = {p.split(":child:", 1)[0] for p in identifiers}
    priorities.extend(p for p in parents if p["raw_id"] in parent_keys)
    remaining = [c for c in chunks if c not in priorities]
    # Parent chunks already chosen take priority; remaining children are ranked
    # by query/section relevance and not by method output rank.
    remaining.sort(key=lambda c: score_context(c, row["query"]), reverse=True)
    ordered = priorities + remaining
    if meta.get("abstract"):
        abstract_id = f"{did}:metadata:abstract"
        if not any(c["raw_id"] == abstract_id for c in ordered):
            ordered.insert(len(priorities), {"raw_id": abstract_id, "text": meta["abstract"], "kind": "abstract", "section": "Abstract", "origin": "document_metadata"})

    sources = []
    remaining_chars = max_chars
    seen_ids: set[str] = set()
    for c in ordered:
        if len(sources) >= max_sources or remaining_chars < 120:
            break
        if c["raw_id"] in seen_ids:
            continue
        seen_ids.add(c["raw_id"])
        allotted = min(2400 if c["origin"] == "parent_unit" else 2000, remaining_chars)
        # Only send source text actually present. Never invent suffixes or locations.
        excerpt = c["text"][:allotted]
        if not excerpt.strip():
            continue
        sources.append({
            "source_id": f"S{len(sources)+1}", "origin": c["origin"],
            "raw_id": c["raw_id"], "section": c.get("section", "") or "UNKNOWN",
            "kind": c["kind"], "text": excerpt,
            "truncated": len(excerpt) < len(c["text"]),
            "text_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        })
        remaining_chars -= len(excerpt)

    # Preview is not authoritative. If none of the target corpus IDs are found,
    # allow a visibly flagged preview-only trial, never imply fulltext access.
    if not sources and row.get("best_chunk_preview"):
        txt = row["best_chunk_preview"][:2000]
        sources.append({"source_id": "S1", "origin": "CSV_PREVIEW_ONLY", "raw_id": best_id,
                        "section": "UNKNOWN", "kind": "unknown", "text": txt,
                        "truncated": True, "text_sha256": hashlib.sha256(txt.encode("utf-8")).hexdigest()})
    return {
        "question_id": qid, "document_id": did, "query": row["query"],
        "title": meta.get("title") or row.get("title") or "",
        "metadata": {"doi": meta.get("doi", ""), "pmid": meta.get("pmid", "")},
        "sources": sources, "missing_support_ids": [i for i in identifiers if i not in {c["raw_id"] for c in chunks}],
        "has_corpus_text": bool(sources) and not any(s["origin"] == "CSV_PREVIEW_ONLY" for s in sources),
        "context_scope": "CURATED_EXCERPTS_NOT_FULLTEXT",
        "disclaimer": "Context is a curated subset of corpus text, not necessarily the full article. No page numbers unless supplied by the source.",
    }


def normspace(s: str) -> str:
    return re.sub(r"\s+", " ", s).casefold().strip()


def quote_state(quote: str, source: dict | None) -> str:
    if not source or not quote or not isinstance(quote, str):
        return "QUOTE_NOT_FOUND"
    q, s = normspace(quote), normspace(source.get("text", ""))
    if len(q) < 12:
        return "QUOTE_NOT_FOUND"  # Too short to verify meaningfully.
    if q in s:
        return "QUOTE_VERIFIED"
    # PARTIAL means some continuous source wording matches; it does NOT
    # authorize claiming the full quote was copied verbatim.
    words = q.split()
    if len(words) >= 12:
        window = max(6, round(len(words) * 0.6))
        for i in range(len(words) - window + 1):
            if len(" ".join(words[i:i+window])) >= 55 and " ".join(words[i:i+window]) in s:
                return "QUOTE_PARTIAL"
    return "QUOTE_NOT_FOUND"


def audit_card(card: dict, context: dict) -> dict:
    by_id = {s["source_id"]: s for s in context["sources"]}
    audits = []
    for q in card.get("evidence_quotes", []):
        if not isinstance(q, dict):
            continue
        source_id = str(q.get("source_id", ""))
        source = by_id.get(source_id)
        state = quote_state(str(q.get("quote") or ""), source)
        audits.append({
            "quote": str(q.get("quote") or ""), "source_id": source_id,
            "section_claimed": str(q.get("section") or ""),
            "section_actual": source["section"] if source else None,
            "raw_id": source["raw_id"] if source else None,
            "source_sha256": source["text_sha256"] if source else None,
            "state": state,
        })
    counts = {k: sum(x["state"] == k for x in audits) for k in QUOTE_STATES}
    return {"quotes": audits, "counts": counts,
            "all_quotes_verified": bool(audits) and counts["QUOTE_VERIFIED"] == len(audits),
            "note": "字面存在核验不等于语义支持核验，也不保证论文研究设计被正确识别。"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(path: Path = DB_FILE) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS cards (
        key TEXT PRIMARY KEY, question_id TEXT NOT NULL, document_id TEXT NOT NULL,
        card_json TEXT NOT NULL, context_json TEXT NOT NULL, audit_json TEXT NOT NULL,
        model_name TEXT NOT NULL, generated_utc TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
        key TEXT NOT NULL, reviewer_id TEXT NOT NULL, review_json TEXT NOT NULL,
        saved_utc TEXT NOT NULL, PRIMARY KEY(key, reviewer_id)
    )""")
    conn.commit()
    return conn


def key_for(qid: str, did: str) -> str:
    return f"{qid}|{did}"


def store_card(conn: sqlite3.Connection, context: dict, card: dict, audit: dict, model: str) -> None:
    key = key_for(context["question_id"], context["document_id"])
    conn.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)", (
        key, context["question_id"], context["document_id"],
        json.dumps(card, ensure_ascii=False), json.dumps(context, ensure_ascii=False),
        json.dumps(audit, ensure_ascii=False), model, utc_now(),
    ))
    conn.commit()


def export_cards(conn: sqlite3.Connection, dest: Path) -> int:
    rows = conn.execute("SELECT * FROM cards ORDER BY question_id,document_id").fetchall()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            obj = {"question_id": row["question_id"], "document_id": row["document_id"],
                   "card": json.loads(row["card_json"]), "audit": json.loads(row["audit_json"]),
                   "model": row["model_name"], "generated_utc": row["generated_utc"]}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    tmp.replace(dest)
    return len(rows)


def export_reviews(conn: sqlite3.Connection, candidates: dict, dest: Path) -> int:
    # Keep original rows, but do not carry AI pre-labels into human-review output.
    approved = {"relevance_label", "evidence_directness", "study_design", "population_match",
                "outcome_match", "supports_answer_from_paper", "review_notes", "reviewer_id"}
    fields = ["question_id", "query", "document_id", "title", "best_chunk_id", "support_chunk_ids",
              "relevance_label", "evidence_directness", "study_design", "population_match",
              "outcome_match", "supports_answer_from_paper", "review_notes", "reviewer_id",
              "review_status", "saved_utc"]
    results = []
    for dbrow in conn.execute("SELECT * FROM reviews ORDER BY key, reviewer_id").fetchall():
        qid, did = dbrow["key"].split("|", 1)
        raw = candidates.get((qid, did), {})
        review = json.loads(dbrow["review_json"])
        obj = {k: raw.get(k, "") for k in fields if k not in approved}
        for k in approved:
            obj[k] = review.get(k, "")
        obj["question_id"], obj["document_id"] = qid, did
        obj["saved_utc"] = dbrow["saved_utc"]
        obj["review_status"] = review.get("review_status", "REVIEWED_PROVISIONAL")
        results.append(obj)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    tmp.replace(dest)
    return len(results)
