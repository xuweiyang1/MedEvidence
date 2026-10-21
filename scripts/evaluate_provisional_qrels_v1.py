#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Evaluate MedEvidence chunk retrieval against a provisional, incompletely judged Qrels pool.

Important:
- This is an exploratory evaluation, NOT a human-validated gold benchmark.
- Unjudged chunks are excluded from condensed nDCG rather than assigned grade 0.
- Recall is explicitly pool-relative: denominator = judged chunks with grade >= --relevance-threshold.
- Recall@K is only emitted when the retrieval run actually contains at least K hits for every evaluated query.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = defaultdict(dict)
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 4:
                raise ValueError(
                    f"Qrels line {line_no} should have 4 whitespace-separated columns; got {len(parts)}"
                )
            qid, _iteration, docid, grade_text = parts
            grade = int(grade_text)
            if grade not in (0, 1, 2, 3):
                raise ValueError(f"Invalid grade {grade} at Qrels line {line_no}")
            if docid in qrels[qid] and qrels[qid][docid] != grade:
                raise ValueError(f"Conflicting duplicate qrels for {qid}/{docid}")
            qrels[qid][docid] = grade
    return dict(qrels)


def qid_of(q: dict[str, Any]) -> str:
    for key in ("query_id", "id", "question_id", "qid"):
        if q.get(key) is not None:
            return str(q[key])
    raise KeyError(f"Cannot find query ID in result row; keys={list(q)}")


def get_ranked_ids(q: dict[str, Any], method: str) -> list[str]:
    hits = q.get(method, [])
    if not isinstance(hits, list):
        raise TypeError(f"Result field {method!r} for {qid_of(q)} is not a list")
    result: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("chunk_id") is None:
            continue
        chunk_id = str(hit["chunk_id"])
        if chunk_id not in seen:
            result.append(chunk_id)
            seen.add(chunk_id)
    return result


def dcg(grades: list[int], k: int) -> float:
    return sum((2 ** grade - 1) / math.log2(rank + 1)
               for rank, grade in enumerate(grades[:k], 1))


def condensed_ndcg_at_10(ranked: list[str], qrels_for_q: dict[str, int]) -> float | None:
    # Condense the ranking by removing unjudged chunks. This avoids silently
    # treating unjudged documents as nonrelevant, but must be reported as condensed nDCG.
    judged_grades = [qrels_for_q[cid] for cid in ranked if cid in qrels_for_q]
    ideal = sorted(qrels_for_q.values(), reverse=True)
    ideal_dcg = dcg(ideal, 10)
    if ideal_dcg <= 0:
        return None
    return dcg(judged_grades, 10) / ideal_dcg


def pool_recall_at_k(
    ranked: list[str], qrels_for_q: dict[str, int], k: int, threshold: int
) -> float | None:
    relevant = {cid for cid, grade in qrels_for_q.items() if grade >= threshold}
    if not relevant:
        return None
    found = set(ranked[:k]) & relevant
    return len(found) / len(relevant)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, help="retrieval_smoke_test.py results.json")
    parser.add_argument("--qrels", required=True, help="Four-column provisional Qrels TSV")
    parser.add_argument("--out-dir", required=True, help="New output directory")
    parser.add_argument("--relevance-threshold", type=int, default=2,
                         help="Grade threshold for pool-relative recall (default: 2)")
    args = parser.parse_args()

    results_path = Path(args.results)
    qrels_path = Path(args.qrels)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(results_path.read_text(encoding="utf-8-sig"))
    result_rows = data.get("questions")
    if not isinstance(result_rows, list):
        raise ValueError("results.json must contain a 'questions' list")
    qrels = load_qrels(qrels_path)

    result_by_qid: dict[str, dict[str, Any]] = {}
    for row in result_rows:
        qid = qid_of(row)
        if qid in result_by_qid:
            raise ValueError(f"Duplicate query ID in results.json: {qid}")
        result_by_qid[qid] = row

    result_ids = set(result_by_qid)
    qrels_ids = set(qrels)
    if result_ids != qrels_ids:
        print("WARNING: query ID sets do not match.")
        print("In results only:", sorted(result_ids - qrels_ids))
        print("In Qrels only:", sorted(qrels_ids - result_ids))
    common_ids = sorted(result_ids & qrels_ids)
    if not common_ids:
        raise ValueError("No overlapping query IDs between retrieval results and Qrels")

    methods = ("bm25", "dense", "hybrid")
    cutoffs = (10, 20, 50, 100)
    per_query_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "schema": "MedEvidence_ProvisionalChunkRetrievalEval_v1",
        "results_path": str(results_path),
        "qrels_path": str(qrels_path),
        "qrels_queries": len(qrels_ids),
        "result_queries": len(result_ids),
        "evaluated_queries": len(common_ids),
        "relevance_threshold_for_pool_recall": args.relevance_threshold,
        "metrics_notes": [
            "Exploratory only; Qrels are AI-assisted and not independently human-validated.",
            "condensed_nDCG@10 removes unjudged retrieved chunks before calculating ranks/gains.",
            "pool_recall@K denominator is judged Qrels chunks with grade >= threshold, not all relevant chunks in the corpus.",
            "Recall@K is omitted when the method returned fewer than K hits for any evaluated query.",
        ],
        "methods": {},
    }

    for method in methods:
        metric_values: dict[str, list[float]] = defaultdict(list)
        coverage_values: dict[str, list[float]] = defaultdict(list)
        returned_depths: list[int] = []
        for qid in common_ids:
            qresult = result_by_qid[qid]
            ranked = get_ranked_ids(qresult, method)
            judged = qrels[qid]
            returned_depths.append(len(ranked))
            judged_in_top10 = sum(1 for cid in ranked[:10] if cid in judged)
            judged_in_top100 = sum(1 for cid in ranked[:100] if cid in judged)
            positive_judged = sum(1 for grade in judged.values() if grade >= args.relevance_threshold)
            ndcg10 = condensed_ndcg_at_10(ranked, judged)
            if ndcg10 is not None:
                metric_values["condensed_nDCG@10"].append(ndcg10)
            for k in cutoffs:
                if len(ranked) >= k:
                    recall = pool_recall_at_k(ranked, judged, k, args.relevance_threshold)
                    if recall is not None:
                        metric_values[f"pool_recall@{k}"].append(recall)
            coverage_values["judged_coverage@10"].append(judged_in_top10 / max(1, min(10, len(ranked))))
            coverage_values["judged_coverage@100"].append(judged_in_top100 / max(1, min(100, len(ranked))))

            row: dict[str, Any] = {
                "query_id": qid,
                "method": method,
                "returned_hits": len(ranked),
                "judged_qrels_chunks": len(judged),
                "judged_relevant_chunks_grade_ge_threshold": positive_judged,
                "judged_hits_in_top10": judged_in_top10,
                "judged_hits_in_top100": judged_in_top100,
                "condensed_nDCG@10": "" if ndcg10 is None else round(ndcg10, 6),
            }
            for k in cutoffs:
                row[f"pool_recall@{k}"] = (
                    round(pool_recall_at_k(ranked, judged, k, args.relevance_threshold), 6)
                    if len(ranked) >= k and pool_recall_at_k(ranked, judged, k, args.relevance_threshold) is not None
                    else ""
                )
            per_query_rows.append(row)

        method_summary: dict[str, Any] = {
            "queries_with_results": len(returned_depths),
            "min_returned_hits": min(returned_depths) if returned_depths else 0,
            "max_returned_hits": max(returned_depths) if returned_depths else 0,
            "mean_returned_hits": round(mean(returned_depths), 3) if returned_depths else 0,
            "macro_means": {
                metric: round(mean(values), 6) if values else None
                for metric, values in metric_values.items()
            },
            "mean_judged_coverage": {
                metric: round(mean(values), 6) if values else None
                for metric, values in coverage_values.items()
            },
        }
        for k in cutoffs:
            key = f"pool_recall@{k}"
            if key not in method_summary["macro_means"]:
                method_summary["macro_means"][key] = None
        summary["methods"][method] = method_summary

    csv_path = out_dir / "per_query_metrics.csv"
    fieldnames = list(per_query_rows[0].keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(per_query_rows)

    json_path = out_dir / "summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Evaluated queries: {len(common_ids)}")
    print(f"Per-query CSV: {csv_path}")
    print(f"Summary JSON: {json_path}")
    print("\nMacro metrics (exploratory; see notes in summary.json):")
    for method, values in summary["methods"].items():
        print(f"\n[{method}]")
        print(json.dumps(values["macro_means"], ensure_ascii=False, indent=2))
        print("Mean judged coverage:", json.dumps(values["mean_judged_coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
