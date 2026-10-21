#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MedEvidence 盲评候选合并器 V1

用途：
- 在 Dense 检索跑通后，把 BM25 和 Dense 候选按 (question_id, document_id) 合并。
- 去除同一论文重复候选。
- 输出一份“隐藏来源”的盲评表，供人工标注 2/1/0/UNCERTAIN。
- 另存 provenance 文件，保留候选来自 BM25、Dense 或两者的信息。
"""
from __future__ import annotations
import argparse, csv, hashlib
from pathlib import Path

def read_csv(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def qid(row):
    return (row.get("question_id") or row.get("id") or row.get("query_id") or "").strip()

def docid(row):
    return (row.get("document_id") or row.get("pmcid") or row.get("paper_id") or "").strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--bm25", type=Path, default=Path("reports/bm25_v1_1_review_candidates.csv"))
    ap.add_argument("--dense", type=Path, required=True)
    args = ap.parse_args()

    root = args.root.resolve()
    bm25 = args.bm25 if args.bm25.is_absolute() else root / args.bm25
    dense = args.dense if args.dense.is_absolute() else root / args.dense

    merged = {}
    for source, rows in [("BM25", read_csv(bm25)), ("DENSE", read_csv(dense))]:
        for r in rows:
            q, d = qid(r), docid(r)
            if not q or not d:
                continue
            key = (q, d)
            rec = merged.setdefault(key, {
                "question_id": q,
                "query": r.get("query",""),
                "document_id": d,
                "title": r.get("title",""),
                "best_chunk_id": r.get("chunk_id",""),
                "best_chunk_preview": r.get("text_preview") or r.get("preview") or r.get("text",""),
                "sources": set(),
            })
            rec["sources"].add(source)
            if not rec["title"] and r.get("title"):
                rec["title"] = r["title"]
            if not rec["best_chunk_preview"]:
                rec["best_chunk_preview"] = r.get("text_preview") or r.get("preview") or r.get("text","")

    rows = []
    for rec in merged.values():
        blind_key = f'{rec["question_id"]}|{rec["document_id"]}|20261014'
        rows.append({
            "question_id": rec["question_id"],
            "query": rec["query"],
            "document_id": rec["document_id"],
            "title": rec["title"],
            "best_chunk_id": rec["best_chunk_id"],
            "best_chunk_preview": rec["best_chunk_preview"][:1600],
            "relevance_label": "",
            "answerable": "",
            "review_notes": "",
            "_blind_order": hashlib.sha256(blind_key.encode("utf-8")).hexdigest(),
            "_sources": ";".join(sorted(rec["sources"])),
        })

    rows.sort(key=lambda r: (r["question_id"], r["_blind_order"]))
    out_blind = root / "reports/retrieval_union_blinded_v1.csv"
    out_key = root / "reports/retrieval_union_provenance_v1.csv"
    out_blind.parent.mkdir(parents=True, exist_ok=True)

    blind_fields = [
        "question_id","query","document_id","title","best_chunk_id",
        "best_chunk_preview","relevance_label","answerable","review_notes"
    ]
    with out_blind.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=blind_fields)
        w.writeheader()
        for r in rows:
            w.writerow({k:r[k] for k in blind_fields})

    key_fields = ["question_id","document_id","_sources","_blind_order"]
    with out_key.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=key_fields)
        w.writeheader()
        for r in rows:
            w.writerow({k:r[k] for k in key_fields})

    print(f"盲评表: {out_blind}")
    print(f"来源映射: {out_key}")
    print(f"候选论文数: {len(rows)}")

if __name__ == "__main__":
    main()
