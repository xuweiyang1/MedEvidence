#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence BM25 + Dense 盲评合并器 V1.1.

- 接收 BM25 当前真实列：query_id / best_chunk_id / chunk_excerpt。
- 按 (问题 ID, 论文 ID) 去重，保留每个检索器的代表片段。
- 当 BM25/Dense 同论文不同 chunk 时，盲评预览包含两个去来源标记的片段。
- 溯源 CSV 单独保留 BM25/Dense 排名、证据片段 ID。
- 默认不覆盖已有标注文件；明确传 --overwrite 才允许覆盖。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"候选 CSV 不存在：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def first(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def sort_rank(row: dict) -> int:
    try:
        return int(first(row, "paper_rank", "rank", "doc_rank") or "999999")
    except (ValueError, TypeError):
        return 999999


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description="合并 BM25 + Dense 论文候选，输出去来源盲评表和单独的溯源表")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--bm25", type=Path, default=Path("reports/bm25_v1_1_review_candidates.csv"))
    p.add_argument("--dense", type=Path, required=True)
    p.add_argument("--overwrite", action="store_true", help="明确允许覆盖现有盲评 CSV（可能丢失人工标注）")
    args = p.parse_args()
    root = args.root.resolve()
    bm25_path = args.bm25 if args.bm25.is_absolute() else root / args.bm25
    dense_path = args.dense if args.dense.is_absolute() else root / args.dense
    out_blind = root / "reports/retrieval_union_blinded_v1.csv"
    out_key = root / "reports/retrieval_union_provenance_v1.csv"
    if not args.overwrite and (out_blind.exists() or out_key.exists()):
        raise FileExistsError("盲评输出已存在；为防止覆盖人工标注，默认停止。确认备份后传 --overwrite。")

    grouped: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: {"BM25": [], "DENSE": []})
    query_by_id: dict[str, str] = {}
    for source, path in (("BM25", bm25_path), ("DENSE", dense_path)):
        rows = read_csv(path)
        if not rows:
            raise ValueError(f"{source} CSV 没有候选行: {path}")
        retained = 0
        for row in rows:
            qid = first(row, "question_id", "query_id", "id")
            did = first(row, "document_id", "pmcid", "paper_id")
            query = first(row, "query", "question", "question_text")
            if not qid or not did:
                raise ValueError(f"{source} 有行缺少问题 ID 或论文 ID，拒绝静默跳过：{row}")
            if query and qid in query_by_id and query_by_id[qid] != query:
                raise ValueError(f"同一 query_id={qid} 在 BM25/Dense 中对应不同问题文本")
            if query:
                query_by_id[qid] = query
            grouped[(qid, did)][source].append(row)
            retained += 1
        print(f"读入 {source}: {retained} 行")

    blinded = []
    provenance = []
    for (qid, did), buckets in grouped.items():
        reps = {src: min(rows, key=sort_rank) for src, rows in buckets.items() if rows}
        candidate_chunks: dict[str, dict] = {}
        for source, row in reps.items():
            chunk_id = first(row, "best_chunk_id", "chunk_id")
            preview = first(row, "chunk_excerpt", "text_preview", "preview", "text", "best_chunk_preview")
            title = first(row, "title")
            key = chunk_id or stable_hash(qid, did, preview)[:24]
            if preview and key not in candidate_chunks:
                candidate_chunks[key] = {"chunk_id": chunk_id, "preview": preview}

        chosen = sorted(candidate_chunks.values(), key=lambda ch: stable_hash(qid, did, ch["chunk_id"], "20261014"))[:2]
        if not chosen:
            raise ValueError(f"问题 {qid} / 论文 {did} 没有可展示的证据文本，请检查候选 CSV 的 chunk_excerpt 列")
        # Chunk pieces are source-blind and deterministically reordered; preserve both unique pieces.
        preview = "\n\n".join(f"片段 {i+1}: {ch['preview'][:720]}" for i, ch in enumerate(chosen))[:1600]
        representative = chosen[0]["chunk_id"] if chosen else ""
        any_row = reps.get("BM25") or reps.get("DENSE")
        title = first(any_row, "title") or next((first(r, "title") for r in reps.values() if first(r, "title")), "")
        blind_order = stable_hash(qid, did, "20261014")
        blinded.append({
            "question_id": qid,
            "query": query_by_id.get(qid, ""),
            "document_id": did,
            "title": title,
            "best_chunk_id": representative,
            "best_chunk_preview": preview,
            "support_chunk_ids": ";".join(ch["chunk_id"] for ch in chosen if ch["chunk_id"]),
            "relevance_label": "", "answerable": "", "review_notes": "",
            "_blind_order": blind_order,
        })
        provenance.append({
            "question_id": qid, "document_id": did,
            "_sources": ";".join(sorted(reps.keys())),
            "bm25_rank": first(reps["BM25"], "paper_rank", "rank") if "BM25" in reps else "",
            "dense_rank": first(reps["DENSE"], "paper_rank", "rank") if "DENSE" in reps else "",
            "bm25_chunk_id": first(reps["BM25"], "best_chunk_id", "chunk_id") if "BM25" in reps else "",
            "dense_chunk_id": first(reps["DENSE"], "best_chunk_id", "chunk_id") if "DENSE" in reps else "",
            "_blind_order": blind_order,
        })

    blinded.sort(key=lambda r: (r["question_id"], r["_blind_order"]))
    provenance.sort(key=lambda r: (r["question_id"], r["_blind_order"]))
    out_blind.parent.mkdir(parents=True, exist_ok=True)
    blind_fields = ("question_id", "query", "document_id", "title", "best_chunk_id", "best_chunk_preview",
                    "support_chunk_ids", "relevance_label", "answerable", "review_notes")
    prov_fields = ("question_id", "document_id", "_sources", "bm25_rank", "dense_rank", "bm25_chunk_id", "dense_chunk_id", "_blind_order")
    for output, cols, rows in ((out_blind, blind_fields, blinded), (out_key, prov_fields, provenance)):
        with output.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for row in rows:
                w.writerow({col: row.get(col, "") for col in cols})

    overlap = sum(1 for value in grouped.values() if value["BM25"] and value["DENSE"])
    print(f"盲评表: {out_blind}\n来源映射: {out_key}\n候选论文数: {len(blinded)}；双方法共同检出: {overlap}")
    print("注意：该表可用于候选论文人工判定；未标注的论文不能直接当作负例用于全库 Recall 评估。")


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
