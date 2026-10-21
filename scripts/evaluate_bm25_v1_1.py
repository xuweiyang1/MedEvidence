#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence: evaluate BM25 V1.1 on HUMAN-VERIFIED paper relevance labels.

Runs against the existing SQLite BM25 V1 index (read-only). Does not create
synthetic gold labels. Output is JSON with paper-level metrics and separate
chunk-level diversity diagnostics.

Place in MedEvidence/scripts alongside medevidence_bm25_v1_1.py:
    python scripts/evaluate_bm25_v1_1.py --gold reports/retrieval_eval_template.csv

Gold file: query_id,query_type,query,gold_document_ids,annotation_status,reviewer_notes
Use | to separate multiple PMCIDs, e.g. PMC111111|PMC222222.
Only VERIFIED questions with manually checked gold PMCID are scored.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

import medevidence_bm25_v1_1 as retriever


def split_ids(raw: str) -> set[str]:
    return {part.strip().upper() for part in (raw or '').split('|') if part.strip()}


def unique_in_order(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def paper_metrics(papers: list[str], gold: set[str], k: int) -> dict:
    """Rank of UNIQUE papers; each relevant paper counted at most once."""
    ranking = unique_in_order(papers)[:k]
    hits = [int(p in gold) for p in ranking]
    first_hit = next((i + 1 for i, x in enumerate(hits) if x), None)
    dcg = sum(v / math.log2(i + 2) for i, v in enumerate(hits))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(gold))))
    count = sum(hits)
    return {
        'paper_hit_at_5': int(any(p in gold for p in ranking[:5])),
        'paper_hit_at_k': int(count > 0),
        'paper_recall_at_k_annotated': round(count / len(gold), 6),
        'paper_mrr_at_k': round(1 / first_hit, 6) if first_hit else 0.0,
        'paper_ndcg_at_k_binary': round(dcg / ideal, 6) if ideal else 0.0,
        'relevant_unique_papers_at_k': count,
        'returned_unique_papers_at_k': len(ranking),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--db', type=Path, default=retriever.DEFAULT_DB)
    parser.add_argument('--gold', type=Path, default=Path('reports/retrieval_eval_template.csv'))
    parser.add_argument('--out', type=Path, default=Path('reports/bm25_v1_1_evaluation.json'))
    parser.add_argument('--top-k', type=int, default=10, help='排名前K篇不同论文，默认10')
    parser.add_argument('--per-document', type=int, default=2, help='检索阶段每篇论文最多几个Chunk')
    args = parser.parse_args()
    if not 1 <= args.top_k <= 50:
        parser.error('--top-k 必须在1至50之间')
    if args.per_document < 1:
        parser.error('--per-document 必须>=1')
    root = args.root.expanduser().resolve()
    gold_path = args.gold if args.gold.is_absolute() else root / args.gold
    out_path = args.out if args.out.is_absolute() else root / args.out
    db_path = args.db if args.db.is_absolute() else root / args.db
    if not gold_path.is_file():
        parser.error(f'找不到评测集: {gold_path}')
    with gold_path.open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        needed = {'query_id', 'query_type', 'query', 'gold_document_ids', 'annotation_status'}
        if reader.fieldnames is None or not needed.issubset(reader.fieldnames):
            parser.error(f'评测集字段不完整，要求: {sorted(needed)}')
        rows = list(reader)
    if not rows:
        parser.error('评测集没有问题行')

    seen_ids = set()
    labeled, skipped = [], []
    for row in rows:
        qid = (row.get('query_id') or '').strip()
        query = (row.get('query') or '').strip()
        if not qid or qid in seen_ids or not query:
            parser.error(f'空/重复 query_id 或空查询：{qid!r}')
        seen_ids.add(qid)
        gold = split_ids(row.get('gold_document_ids') or '')
        status = (row.get('annotation_status') or '').strip().upper()
        if status == 'VERIFIED' and not gold:
            parser.error(f'{qid}: VERIFIED 必须有人工核实的 gold_document_ids')
        if status == 'VERIFIED':
            labeled.append((row, gold))
        else:
            skipped.append(qid)

    with gold_path.open('rb') as f:
        gold_digest = hashlib.sha256(f.read()).hexdigest()
    summary = {
        'engine': 'BM25_V1.1',
        'gold_file_sha256': gold_digest,
        'retrieval_parameters': {'per_document': args.per_document, 'per_section': 2,
                                 'auto_anchor': True, 'broad_or': False},
        'evaluation_unit': 'unique_paper',
        'top_k_unique_papers': args.top_k,
        'total_questions': len(rows),
        'verified_scored_questions': len(labeled),
        'unlabeled_skipped': len(skipped),
        'macro_average': None,
        'results': [],
        'warning': ('仅评估人工核验的PMC候选文献。若未穷举相关论文，'
                    'Annotated Recall不是真实全量Recall；不代表临床适用性、证据等级或声明级引用支持。'),
    }
    if labeled:
        conn = retriever.open_index(db_path)
        try:
            summary['index_meta'] = dict(conn.execute('SELECT key,value FROM index_meta'))
            # Stop typos or references outside this index from silently lowering scores.
            available = {r[0].upper() for r in conn.execute('SELECT DISTINCT document_id FROM chunks')}
            for row, gold in labeled:
                absent = sorted(gold - available)
                if absent:
                    raise ValueError(f'{row["query_id"]} 的金标准PMCID不在当前921篇索引中: {absent}')
                # We retrieve extra chunks because several chunks may belong to one paper.
                retrieval_k = min(200, max(40, args.top_k * args.per_document * 3))
                response = retriever.search_index(
                    conn, row['query'], top_k=retrieval_k,
                    per_document=args.per_document, per_section=2)
                chunks = response['results']
                chunk_papers = [c['document_id'].upper() for c in chunks]
                paper_ranking = unique_in_order(chunk_papers)[:args.top_k]
                metrics = paper_metrics(paper_ranking, gold, args.top_k)
                top_chunk_window = chunk_papers[:args.top_k]
                chunk_duplicate = (1 - len(set(top_chunk_window)) / len(top_chunk_window)) if top_chunk_window else 0.0
                summary['results'].append({
                    'query_id': row['query_id'], 'query_type': row.get('query_type', ''),
                    'query': row['query'], 'gold_document_ids': sorted(gold),
                    'retrieved_unique_papers': paper_ranking,
                    'first_chunk_papers': top_chunk_window,
                    'chunk_duplicate_fraction_at_k': round(chunk_duplicate, 6),
                    'metrics': metrics, 'tier_stats': response['tier_stats'],
                })
        finally:
            conn.close()
        metric_keys = list(summary['results'][0]['metrics'])
        macro = {
            metric: round(statistics.mean(row['metrics'][metric] for row in summary['results']), 6)
            for metric in metric_keys
        }
        macro['chunk_duplicate_fraction_at_k'] = round(statistics.mean(
            row['chunk_duplicate_fraction_at_k'] for row in summary['results']), 6)
        summary['macro_average'] = macro

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not labeled:
        print(f'读取了 {len(rows)} 道问题，但仍无 VERIFIED 且填写了人工标准PMCID的问题。')
        print('已生成待标注报告；不得把UNLABELED问题当成真实检索评测成绩。')
    else:
        print(f'已评估 {len(labeled)} 个人工核实问题，未核实跳过 {len(skipped)} 个。')
        print('论文级宏平均（只针对已标注相关文献）:', summary['macro_average'])
    print('报告:', out_path)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, sqlite3.Error, ValueError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(2)
