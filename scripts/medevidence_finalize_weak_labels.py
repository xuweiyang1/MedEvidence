#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Finalize MedEvidence LLM weak labels without making more API calls.

Reads the original review pool, the existing judgments.jsonl cache, and the
local SQLite chunks table. It validates quotes using conservative Unicode,
case, dash and whitespace normalization. If a grade >= 2 still has no exact
normalized quote in the exact text excerpt sent to the model, it downgrades
that item to grade 1, marks it uncertain, and records the downgrade explicitly.

This creates complete *weak labels*, NOT human gold labels or clinical truth.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = "MedEvidence_AutoJudge_FinalizedWeakLabels_V1"
DEFAULT_MAX_CHARS = 7000


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize_quote(text: str) -> str:
    """Normalize presentation differences while preserving words/punctuation."""
    text = unicodedata.normalize("NFKC", text or "")
    translation = str.maketrans({
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2014": "-", "\u2212": "-", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"', "\u00a0": " ",
    })
    text = text.translate(translation)
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_matches(quote: str, evidence: str) -> bool:
    q = normalize_quote(quote)
    e = normalize_quote(evidence)
    return bool(q) and q in e


def load_chunks(db_path: Path, chunk_ids: set[str]) -> dict[str, dict[str, str]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        cols = {str(row[1]) for row in con.execute('PRAGMA table_info("chunks")')}
        required = {"chunk_id", "document_id", "body_text"}
        if not required.issubset(cols):
            raise RuntimeError(f"SQLite chunks 表缺少字段：{sorted(required - cols)}")
        found: dict[str, dict[str, str]] = {}
        ids = sorted(chunk_ids)
        for start in range(0, len(ids), 250):
            batch = ids[start:start + 250]
            if not batch:
                continue
            marks = ",".join("?" for _ in batch)
            sql = f'SELECT chunk_id, document_id, body_text FROM chunks WHERE chunk_id IN ({marks})'
            for cid, did, body in con.execute(sql, batch):
                found[str(cid)] = {"doc_id": str(did), "text": str(body or "")}
        return found
    finally:
        con.close()


def load_cache(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Use the last cache entry per (query_id, chunk_id), including retries."""
    result: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        raise FileNotFoundError(f"缓存不存在：{path}")
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"缓存第 {line_no} 行不是有效 JSON：{exc}") from exc
            key = (str(item.get("query_id", "")), str(item.get("chunk_id", "")))
            if key != ("", ""):
                result[key] = item
    return result


def to_grade(value: Any) -> int | None:
    try:
        grade = int(value)
    except (TypeError, ValueError):
        return None
    return grade if 0 <= grade <= 3 else None


def rank_is_top5(value: Any) -> bool:
    try:
        return int(str(value).strip()) <= 5 and int(str(value).strip()) >= 1
    except (TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, help="原始 review_pool.csv")
    parser.add_argument("--db", required=True, help="版本化 BM25 SQLite，例如 .../bm25.sqlite3")
    parser.add_argument("--out-dir", required=True, help="已有 qrels_auto_v1 输出目录，含 judgments.jsonl")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help="必须与最初自动评审使用的 --max-chars 一致，默认 7000")
    args = parser.parse_args()

    pool_path = Path(args.pool)
    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    cache_path = out_dir / "judgments.jsonl"
    for path in (pool_path, db_path, cache_path):
        if not path.is_file():
            parser.error(f"文件不存在：{path}")
    if args.max_chars < 400:
        parser.error("--max-chars 至少为 400")

    pool_cols, pool_rows = read_csv(pool_path)
    required = {"query_id", "question", "chunk_id", "doc_id", "content_kind",
                "bm25_rank", "dense_rank", "hybrid_rank"}
    if not required.issubset(set(pool_cols)):
        parser.error(f"review_pool.csv 缺少字段：{sorted(required - set(pool_cols))}")
    keys = [(r["query_id"], r["chunk_id"]) for r in pool_rows]
    if len(keys) != len(set(keys)):
        parser.error("review_pool.csv 中存在重复 (query_id, chunk_id)")

    cache = load_cache(cache_path)
    chunk_ids = {r["chunk_id"] for r in pool_rows}
    chunks = load_chunks(db_path, chunk_ids)
    missing_chunks = chunk_ids - set(chunks)
    if missing_chunks:
        raise RuntimeError(f"SQLite 找不到 {len(missing_chunks)} 个 Chunk，例如：{sorted(missing_chunks)[:5]}")

    output_rows: list[dict[str, Any]] = []
    final_judgments: list[dict[str, Any]] = []
    resolutions: Counter[str] = Counter()
    for row in pool_rows:
        key = (row["query_id"], row["chunk_id"])
        entry = cache.get(key)
        out = dict(row)
        if not entry:
            entry = {}
            original_status = "missing_cache"
        else:
            original_status = str(entry.get("status", "unknown"))

        chunk = chunks[row["chunk_id"]]
        if chunk["doc_id"] != row["doc_id"]:
            raise RuntimeError(f"doc_id 不一致：{row['query_id']} / {row['chunk_id']}")
        evidence = chunk["text"][:args.max_chars]
        if not evidence.strip():
            raise RuntimeError(f"正文为空：{row['chunk_id']}")

        expected_hash = str(entry.get("evidence_sha256", ""))
        hash_matches = (not expected_hash) or (sha256(evidence) == expected_hash)
        if entry and not hash_matches:
            raise RuntimeError(
                f"证据文本哈希不一致：{row['query_id']} / {row['chunk_id']}。"
                f"请确认 --max-chars 与原自动评审一致（当前 {args.max_chars}）。"
            )

        raw_grade = to_grade(entry.get("grade"))
        existing_grade = to_grade(entry.get("auto_grade_0_3"))
        grade = existing_grade if existing_grade is not None else raw_grade
        quote = str(entry.get("supporting_quote", "") or "").strip()
        quote_verified = quote_matches(quote, evidence)
        facets = entry.get("covered_facets", [])
        if not isinstance(facets, list):
            facets = []
        facets = [str(x) for x in facets if isinstance(x, str)]
        reason = str(entry.get("mismatch_reason", "") or "")
        rationale = str(entry.get("rationale", "") or "")
        uncertain = bool(entry.get("uncertain", False))
        status = original_status
        resolution = "unchanged"

        if not entry:
            grade = None
            uncertain = True
            resolution = "missing_cache"
            status = "missing_cache"
        elif grade is None:
            uncertain = True
            resolution = "no_valid_grade_in_cache"
            status = "no_valid_grade_in_cache"
        elif grade >= 2 and not quote_verified:
            # Do not present a high grade as evidence-backed if the quote cannot
            # be verified. Grade 1 is an explicitly conservative weak label.
            # The original model grade remains in auto_original_grade_0_3.
            grade = 1
            uncertain = True
            resolution = "downgraded_to_1_quote_unverified"
            status = "conservative_downgrade_quote_unverified"
            reason = reason or "quote_unverified_conservative_downgrade"
        elif grade == 3 and not facets:
            # A grade-3 label without any covered target facet fails the rubric.
            grade = 2 if quote_verified else 1
            uncertain = True
            resolution = "downgraded_missing_facet_coverage"
            status = "conservative_downgrade_missing_facets"
        elif status == "quote_validation_failed" and quote_verified:
            # A normalized exact match may resolve formatting-only differences.
            resolution = "normalized_exact_quote_match"
            status = "model_generated_quote_normalized_match"
        elif status == "facet_validation_failed":
            resolution = "conservative_facet_repair"
            status = "conservative_downgrade_missing_facets"
            uncertain = True

        out.update({
            "auto_grade_0_3": "" if grade is None else str(grade),
            "auto_covered_facets": "; ".join(facets),
            "auto_mismatch_reason": reason,
            "auto_supporting_quote": quote,
            "auto_rationale": rationale,
            "auto_uncertain": str(uncertain),
            "auto_status": status,
            "auto_model": str(entry.get("model", "") or ""),
            "auto_evidence_sha256": expected_hash,
            "auto_quote_verified_in_input": str(quote_verified),
            "auto_original_status": original_status,
            "auto_original_grade_0_3": "" if raw_grade is None else str(raw_grade),
            "auto_resolution": resolution,
        })
        output_rows.append(out)
        resolutions[resolution] += 1
        final_judgments.append({
            "query_id": row["query_id"], "chunk_id": row["chunk_id"],
            "doc_id": row["doc_id"], "model": entry.get("model", ""),
            "original_status": original_status,
            "final_status": status,
            "original_grade": raw_grade,
            "final_grade": grade,
            "quote_verified_normalized_exact": quote_verified,
            "resolution": resolution,
            "uncertain": uncertain,
            "supporting_quote": quote,
            "covered_facets": facets,
            "rationale": rationale,
            "evidence_sha256": expected_hash,
        })

    extra_cols = [
        "auto_grade_0_3", "auto_covered_facets", "auto_mismatch_reason",
        "auto_supporting_quote", "auto_rationale", "auto_uncertain", "auto_status",
        "auto_model", "auto_evidence_sha256", "auto_quote_verified_in_input",
        "auto_original_status", "auto_original_grade_0_3", "auto_resolution",
    ]
    final_csv = out_dir / "auto_review_pool_final.csv"
    write_csv(final_csv, list(dict.fromkeys(pool_cols + extra_cols)), output_rows)

    final_jsonl = out_dir / "judgments_final.jsonl"
    with final_jsonl.open("w", encoding="utf-8") as fh:
        for item in final_judgments:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    graded = [r for r in output_rows if r["auto_grade_0_3"] != ""]
    statuses = Counter(r["auto_status"] for r in output_rows)
    grades = Counter(r["auto_grade_0_3"] for r in graded)
    quote_unverified_high = sum(
        1 for r in output_rows
        if r["auto_grade_0_3"] in {"2", "3"} and r["auto_quote_verified_in_input"] != "True"
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "warning": (
            "LLM pseudo-labels only; NOT human gold labels, clinical truth, accuracy, "
            "or full-corpus Recall. Any quote-unverified grade >=2 is conservatively downgraded to 1."
        ),
        "pool_rows": len(output_rows),
        "auto_grade_available": len(graded),
        "missing_or_invalid": len(output_rows) - len(graded),
        "grade_counts": dict(grades),
        "statuses": dict(statuses),
        "resolutions": dict(resolutions),
        "quote_unverified_final_grade_2_or_3": quote_unverified_high,
        "issues_by_question": {},
    }
    for qid in sorted({r["query_id"] for r in output_rows}):
        matches = [r for r in output_rows if r["query_id"] == qid]
        report["issues_by_question"][qid] = {
            "n": len(matches),
            "graded": sum(r["auto_grade_0_3"] != "" for r in matches),
            "top5_per_method_grade3_count_weak": {
                method: sum(
                    r["auto_grade_0_3"] == "3" and rank_is_top5(r.get(f"{method}_rank", ""))
                    for r in matches
                ) for method in ("bm25", "dense", "hybrid")
            },
            "top5_facets_seen_weak": {
                method: sorted({
                    facet.strip()
                    for r in matches
                    if r["auto_grade_0_3"] != "" and rank_is_top5(r.get(f"{method}_rank", ""))
                    for facet in str(r.get("auto_covered_facets", "")).split(";")
                    if facet.strip()
                }) for method in ("bm25", "dense", "hybrid")
            },
        }
    final_report = out_dir / "weak_diagnostics_final.json"
    final_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] {len(graded)}/{len(output_rows)} finalized weak labels")
    print(f"[STATUS] {dict(statuses)}")
    print(f"[RESOLUTION] {dict(resolutions)}")
    print(f"[GRADE COUNTS] {dict(grades)}")
    print(f"[QUOTE CHECK] grade 2/3 without verified quote: {quote_unverified_high}")
    print(f"CSV: {final_csv}")
    print(f"JSON: {final_report}")
    print(f"Final judgments: {final_jsonl}")
    print("Original judgments.jsonl was not modified; no API calls were made.")
    return 0 if len(graded) == len(output_rows) and quote_unverified_high == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
