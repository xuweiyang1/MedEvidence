#!/usr/bin/env python3
"""MedEvidence V1c.2: hydrate smoke-test hits from read-only SQLite chunks table.

No embedding, no Qdrant access, no index modification. Existing results.json is never overwritten.

Usage:
  python scripts/hydrate_retrieval_results.py --results outputs/retrieval_smoke/v3_2_trial_clean/<run>/results.json
  python scripts/hydrate_retrieval_results.py --results ... --bm25-db data/processed/index_versions/v3_2_trial_clean/bm25.sqlite3
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only MedEvidence evidence-text recovery")
    p.add_argument("--results", required=True, type=Path, help="Original results.json")
    p.add_argument("--bm25-db", type=Path, default=None, help="SQLite index; defaults to results.json meta.bm25_db")
    p.add_argument("--out-dir", type=Path, default=None, help="Output directory; defaults to directory of results.json")
    p.add_argument("--top-k", type=int, default=5, help="Rows per method per question in Markdown report")
    p.add_argument("--excerpt-chars", type=int, default=360, help="Maximum evidence excerpt length per row")
    return p.parse_args()


def chunk_kind(chunk_id: str) -> str:
    for marker, kind in ((":table:", "table"), (":figure:", "figure"), (":abstract:", "abstract")):
        if marker in chunk_id:
            return kind
    return "body"


def md_escape(v: object, max_chars: int = 360) -> str:
    val = " ".join(str(v or "").split())
    if max_chars > 0 and len(val) > max_chars:
        val = val[:max_chars].rstrip() + "…"
    return val.replace("|", "\\|").replace("`", "'")


def main() -> int:
    args = parse_args()
    if args.top_k < 1 or args.excerpt_chars < 50:
        raise ValueError("--top-k must be positive; --excerpt-chars must be at least 50")
    original = args.results.resolve()
    if not original.is_file():
        raise FileNotFoundError(f"Missing results file: {original}")
    data = json.loads(original.read_text(encoding="utf-8-sig"))
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Expected a non-empty 'questions' array")
    bm25_db_value = args.bm25_db or data.get("meta", {}).get("bm25_db")
    if not bm25_db_value:
        raise ValueError("Missing --bm25-db and meta.bm25_db")
    db_path = Path(bm25_db_value).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Missing SQLite index: {db_path}")

    # URI read-only connection, and explicitly forbid writes on this connection.
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info('chunks')")}
        needed = {"chunk_id", "document_id", "body_text"}
        if not needed.issubset(columns):
            raise RuntimeError(f"SQLite 'chunks' columns missing {sorted(needed - columns)}; found {sorted(columns)}")
        optional = [col for col in ("title", "section_path", "content_kind", "doi", "pmid") if col in columns]
        selected = ["chunk_id", "document_id", "body_text", *optional]
        sql_columns = ", ".join('"' + c + '"' for c in selected)
        ids = sorted({h["chunk_id"] for q in questions for method in ("bm25", "dense", "hybrid") for h in q.get(method, [])})
        records = {}
        for start in range(0, len(ids), 400):
            part = ids[start:start + 400]
            placeholders = ",".join("?" * len(part))
            sql = f'SELECT {sql_columns} FROM "chunks" WHERE "chunk_id" IN ({placeholders})'
            for row in conn.execute(sql, part):
                key = row["chunk_id"]
                if key in records:
                    raise RuntimeError(f"Duplicate chunk_id in SQLite chunks table: {key}")
                records[key] = dict(row)
    finally:
        conn.close()

    enriched = json.loads(json.dumps(data, ensure_ascii=False))
    count_all = collections.Counter()
    doc_mismatches = []
    for q in enriched["questions"]:
        for method in ("bm25", "dense", "hybrid"):
            for hit in q.get(method, []):
                count_all[f"{method}_all"] += 1
                meta = records.get(hit["chunk_id"])
                if meta is None:
                    count_all[f"{method}_missing_chunk"] += 1
                    continue
                body_text = meta["body_text"] or ""
                hit["text"] = body_text
                for col in optional:
                    hit[col] = meta.get(col)
                hit["text_source"] = "sqlite.chunks.body_text"
                if body_text.strip():
                    count_all[f"{method}_nonempty"] += 1
                if hit.get("doc_id") != meta["document_id"]:
                    count_all[f"{method}_doc_mismatch"] += 1
                    doc_mismatches.append((q.get("id"), method, hit["chunk_id"], hit.get("doc_id"), meta["document_id"]))
    missing_ids = sorted(set(ids) - set(records))
    nonempty_unique_ids = sum(1 for row in records.values() if (row["body_text"] or "").strip())
    original_source = data.get("meta", {}).get("version", "unspecified")
    enriched.setdefault("hydration", {})
    enriched["hydration"] = {
        "source_results": str(original),
        "source_db": str(db_path),
        "source_column": "chunks.body_text",
        "source_version": original_source,
        "unique_chunk_ids": len(ids),
        "sqlite_chunks_matched": len(records),
        "sqlite_chunks_nonempty": nonempty_unique_ids,
        "missing_chunk_ids": missing_ids,
        "doc_id_mismatches": doc_mismatches,
        "hit_counts": dict(count_all),
    }

    output_dir = (args.out_dir or original.parent).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_json = output_dir / "results_enriched.json"
    out_md = output_dir / "report_enriched.md"
    if out_json == original or out_md == original:
        raise ValueError("Cannot overwrite original results file")

    out_json.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# MedEvidence 检索证据正文补全报告（只读 SQLite）", "",
        f"- 索引版本：`{md_escape(original_source, 90)}`",
        f"- 唯一 Chunk ID：{len(ids)}；SQLite 命中：{len(records)}；含非空正文：{nonempty_unique_ids}",
        f"- 未找到 Chunk ID：{len(missing_ids)}；文献 ID 不一致记录：{len(doc_mismatches)}", "",
        "> 此报告仅用于人工审核证据相关性；未标注金标准，不得视为 Recall/MRR/nDCG 或临床准确率。", "",
    ]
    if missing_ids:
        lines.extend(["## 未找到的 Chunk ID（前 30 条）", ""])
        lines.extend([f"- `{md_escape(cid, 150)}`" for cid in missing_ids[:30]])
        lines.append("")
    for q in enriched["questions"]:
        lines.extend([f"## {md_escape(q.get('id', ''), 50)} — {md_escape(q.get('question', ''), 250)}", ""])
        for method in ("bm25", "dense", "hybrid"):
            lines.extend([f"### {method.upper()} Top {args.top_k}", "",
                          "| 排名 | Chunk ID | Doc ID | 类型 | 分数 | 文本摘要 |", "|---:|---|---|---|---:|---|"])
            for rank, hit in enumerate(q.get(method, [])[:args.top_k], 1):
                score = hit.get("rrf_score", hit.get("score", 0))
                try:
                    score_str = f"{float(score):.6f}"
                except (ValueError, TypeError):
                    score_str = str(score)
                excerpt = md_escape(hit.get("text", ""), args.excerpt_chars) or "**[空文本]**"
                if hit["chunk_id"] in missing_ids:
                    excerpt = "**[SQLite 中未找到 Chunk ID]**"
                lines.append(f"| {rank} | `{md_escape(hit['chunk_id'], 170)}` | "
                             f"`{md_escape(hit.get('doc_id', ''), 70)}` | {chunk_kind(hit['chunk_id'])} | "
                             f"{score_str} | {excerpt} |")
            lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"Source: {original}")
    print(f"SQLite (READ-ONLY): {db_path}")
    print(f"Unique Chunk IDs: {len(ids)}; matched in chunks: {len(records)}; nonempty texts: {nonempty_unique_ids}")
    print(f"Missing IDs: {len(missing_ids)}; doc_id mismatches: {len(doc_mismatches)}")
    for method in ("bm25", "dense", "hybrid"):
        print(f"{method.upper()}: nonempty text {count_all[f'{method}_nonempty']}/{count_all[f'{method}_all']}")
    print(f"Enriched JSON: {out_json}")
    print(f"Enriched report: {out_md}")
    if missing_ids or doc_mismatches or nonempty_unique_ids == 0:
        print("[WARN] Missing/mismatched/empty evidence detected. Do not treat results as valid for evidence-based answer generation.")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, sqlite3.Error, RuntimeError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
