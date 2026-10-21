#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export BM25 paper candidates for MANUAL medical relevance review.

Use from project root:
  python scripts/prepare_retrieval_review_v1.py --questions reports/retrieval_eval_template.csv

Output: reports/bm25_v1_1_review_candidates.csv
It is NOT a gold-standard dataset. Do not label candidates as relevant just
because a retriever returned them. Read original paper and record judgement.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import medevidence_bm25_v1_1 as retriever


FIELDS = [
    'query_id', 'query_type', 'query', 'paper_rank', 'document_id', 'title',
    'best_chunk_id', 'content_kind', 'section', 'retrieval_tier', 'bm25_raw',
    'chunk_excerpt', 'relevance_label', 'verified_evidence_chunk_id',
    'reviewer_notes',
]


def safe_cell(value):
    """Avoid spreadsheet formula execution in arbitrary source titles/text."""
    s = str(value if value is not None else '')
    return "'" + s if s.lstrip().startswith(('=', '+', '-', '@')) else s


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--db', type=Path, default=retriever.DEFAULT_DB)
    parser.add_argument('--questions', type=Path, default=Path('reports/retrieval_eval_template.csv'))
    parser.add_argument('--out', type=Path, default=Path('reports/bm25_v1_1_review_candidates.csv'))
    parser.add_argument('--papers-per-query', type=int, default=20)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    question_path = args.questions if args.questions.is_absolute() else root / args.questions
    out_path = args.out if args.out.is_absolute() else root / args.out
    db_path = args.db if args.db.is_absolute() else root / args.db
    if not 1 <= args.papers_per_query <= 50:
        parser.error('--papers-per-query 取值1-50')
    with question_path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {'query_id', 'query'}.issubset(reader.fieldnames):
            parser.error('问题文件需要query_id与query列')
        questions = list(reader)
    if not questions:
        parser.error('没有问题')
    conn = retriever.open_index(db_path)
    records = []
    try:
        for row in questions:
            query = (row.get('query') or '').strip()
            if not query:
                continue
            out = retriever.search_index(
                conn, query, top_k=min(200, max(60, args.papers_per_query * 3)),
                per_document=2, per_section=2)
            seen = set()
            for candidate in out['results']:
                doc_id = candidate['document_id']
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                records.append(dict(
                    query_id=row.get('query_id', ''), query_type=row.get('query_type', ''),
                    query=query, paper_rank=len(seen), document_id=doc_id,
                    title=candidate.get('title', ''),
                    best_chunk_id=candidate.get('chunk_id', ''),
                    content_kind=candidate.get('content_kind', ''),
                    section=candidate.get('section_path', ''),
                    retrieval_tier=candidate.get('matching_tier', ''),
                    bm25_raw=candidate.get('bm25_raw', ''),
                    chunk_excerpt=candidate.get('text_preview', '')[:500].replace('\n', ' '),
                    relevance_label='', verified_evidence_chunk_id='', reviewer_notes='',
                ))
                if len(seen) >= args.papers_per_query:
                    break
            print(f'{row.get("query_id", "?")}: 候选论文 {len(seen)} 篇')
    finally:
        conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in records:
            writer.writerow({k: safe_cell(row.get(k, '')) for k in FIELDS})
    print(f'输出：{out_path}（候选 ≠ 标准答案；仍需人工阅读原文）')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as err:
        print(f'ERROR: {err}', file=sys.stderr)
        sys.exit(2)
