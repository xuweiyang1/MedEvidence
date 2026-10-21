#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence BM25 V1.1: staged retrieval + document diversity.

Read-only against the existing BM25 V1 SQLite index. Python 3.11 stdlib only.

Examples (from project root):
  python scripts/medevidence_bm25_v1_1.py search --query "ABCC8 polymorphism type 2 diabetes fasting glucose" --must ABCC8 --phrase "type 2 diabetes"
  python scripts/medevidence_bm25_v1_1.py search --query "chronic kidney disease glomerular filtration rate" --top-k 10
  python scripts/medevidence_bm25_v1_1.py search --query "creatinine estimated glomerular filtration rate" --kind table

Not a medical evidence classifier. CANDIDATE data for controlled local research only.
Do NOT directly sum bm25_raw with any embedding score. Rank positions can be used for RRF.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path('data/processed/index/bm25_v1.sqlite3')
DEFAULT_REPORT = Path('reports/bm25_v1_1_search_results.json')
STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can', 'do', 'does', 'for',
    'from', 'how', 'in', 'is', 'it', 'of', 'on', 'or', 'the', 'this', 'to', 'was',
    'were', 'what', 'which', 'who', 'with', 'would', 'among', 'between', 'than',
    'that', 'these', 'those', 'patient', 'patients', 'study', 'studies'
}
TOKEN_RE = re.compile(r'[^\W_]+(?:_[^\W_]+)*', flags=re.UNICODE)
VALID_KINDS = {'abstract', 'body', 'table', 'figure_caption'}


def normalize_terms(s: str) -> list[str]:
    raw = TOKEN_RE.findall(s.lower())
    keep = [t for t in raw if t not in STOPWORDS]
    return list(dict.fromkeys(keep or raw))[:20]


def qterm(word: str) -> str:
    """Quoted FTS token, never concatenate raw user syntax."""
    return '"' + word.replace('"', '""') + '"'


def phrase_expr(s: str) -> str:
    words = TOKEN_RE.findall(s.lower())
    if not words:
        raise ValueError('空短语不能用作 --phrase')
    return qterm(' '.join(words))


def automatic_anchors(query: str) -> list[str]:
    # Only unambiguous identifier-like forms: e.g. ABCC8, rs1799854, NCT12345678.
    raw = TOKEN_RE.findall(query)
    return list(dict.fromkeys(x.lower() for x in raw if
        re.fullmatch(r'rs\d{3,}', x, re.I)
        or re.fullmatch(r'NCT\d{8}', x, re.I)
        or re.fullmatch(r'(?=[A-Z0-9]{3,12}$)(?=.*[A-Z])(?=.*\d)[A-Z0-9]+', x)))


def open_index(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise FileNotFoundError(f'找不到 BM25 V1 索引，请先运行旧版 build: {db}')
    conn = sqlite3.connect(f'{db.resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    if not {'chunks','fts_chunks','index_meta'}.issubset(tables):
        conn.close()
        raise ValueError('数据库没有 BM25 V1 的预期表')
    meta = dict(conn.execute('SELECT key, value FROM index_meta'))
    if meta.get('data_status') != 'LOCAL_RESEARCH_CANDIDATE':
        conn.close()
        raise ValueError('索引来源状态不符合本地候选库要求')
    return conn


def sql_retrieve(conn: sqlite3.Connection, expr: str, kind: str | None,
                 pool: int) -> list[dict]:
    sql = '''SELECT c.chunk_id, c.document_id, c.parent_id, c.content_kind,
        c.section_path, c.title, c.doi, c.pmid, c.media_id, c.body_text,
        bm25(fts_chunks, 2.0, 1.3, 1.0) AS bm25_raw
        FROM fts_chunks JOIN chunks AS c ON c.id = fts_chunks.rowid
        WHERE fts_chunks MATCH ?'''
    params: list = [expr]
    if kind:
        sql += ' AND c.content_kind = ?'
        params.append(kind)
    sql += ' ORDER BY bm25_raw ASC LIMIT ?'
    params.append(pool)
    return [dict(row) for row in conn.execute(sql, params)]


def matched_terms(row: dict, terms: list[str]) -> list[str]:
    text = ' '.join(str(row.get(x) or '') for x in ('title', 'section_path', 'body_text')).lower()
    words = set(TOKEN_RE.findall(text))
    return [term for term in terms if term in words]


def search_index(conn: sqlite3.Connection, query: str, *, top_k: int = 10,
                 per_document: int = 2, per_section: int = 1,
                 kind: str | None = None, must: list[str] | None = None,
                 phrases: list[str] | None = None, allow_broad: bool = False,
                 no_auto_anchor: bool = False, pool: int = 4000) -> dict:
    if not (1 <= top_k <= 200) or per_document < 1 or per_section < 1:
        raise ValueError('top-k 必须在1-200，per-document/per-section必须>=1')
    terms = normalize_terms(query)
    if not terms:
        raise ValueError('查询无有效词项')
    explicit = [t for s in (must or []) for t in normalize_terms(s)]
    auto = [] if no_auto_anchor else automatic_anchors(query)
    explicit = list(dict.fromkeys(explicit))
    anchors = list(dict.fromkeys(explicit + auto))
    phrases = phrases or []
    strict_terms = list(dict.fromkeys(terms + anchors))
    strict_expr = ' AND '.join([qterm(t) for t in strict_terms] + [phrase_expr(p) for p in phrases])
    loose_expr = ' OR '.join(qterm(t) for t in terms)
    # Separate stages: don't discard strict results when relaxing. BM25 scores from
    # different MATCH expressions are not compared against one another.
    pool = max(200, min(12000, pool))
    tiers: list[tuple[str, str, bool]] = [('STRICT_AND', strict_expr, False)]
    if len(terms) > 1:
        # For the middle tier, the FTS query may be broad, but Python must
        # verify core identifiers and >=65% distinct query-term coverage.
        if anchors:
            tiers.append(('ANCHOR_PARTIAL',
                ' AND '.join([qterm(t) for t in anchors] + ['(' + loose_expr + ')']), True))
        else:
            tiers.append(('MAJORITY_PARTIAL', '(' + loose_expr + ')', True))
    if allow_broad:
        tiers.append(('EXPLICIT_BROAD_OR', '(' + loose_expr + ')', False))

    selected: list[dict] = []
    seen = set()
    docs = collections.Counter()
    sections = collections.Counter()
    tier_counts = {}
    thresholds = max(2, math.ceil(len(terms)*0.65)) if len(terms) >= 2 else 1
    for name, expr, filter_majority in tiers:
        if len(selected) >= top_k:
            break
        rows = sql_retrieve(conn, expr, kind, pool)
        accepted = 0
        for row in rows:
            cid = row['chunk_id']
            if cid in seen:
                continue
            found = matched_terms(row, terms)
            present = set(matched_terms(row, anchors))
            # --must stays mandatory in ALL tiers. Auto-detected identifiers may be
            # relaxed only at the user's explicit broad-OR request.
            if not set(explicit).issubset(present):
                continue
            if anchors and not set(anchors).issubset(present) and name != 'EXPLICIT_BROAD_OR':
                continue
            if filter_majority and len(found) < thresholds:
                continue
            did = row['document_id']
            # Same-section limitation applies to headings; for media chunk IDs,
            # prefer their own parent as the section key when no heading is present.
            section_key = (did, row['section_path'] or row['parent_id'])
            if docs[did] >= per_document or sections[section_key] >= per_section:
                continue
            seen.add(cid)
            docs[did] += 1
            sections[section_key] += 1
            accepted += 1
            row['bm25_raw'] = float(row['bm25_raw'])
            row['matched_terms'] = found
            row['matching_tier'] = name
            # Explicitly do not mislabel lexical match as medical relevance.
            row['text_preview'] = row.pop('body_text')[:1200]
            row['rank'] = len(selected)+1
            selected.append(row)
            if len(selected) >= top_k:
                break
        tier_counts[name] = {'pool_returned': len(rows), 'selected': accepted}
    paper_order = {}
    for item in selected:
        did = item['document_id']
        if did not in paper_order:
            paper_order[did] = len(paper_order)+1
        item['paper_rank'] = paper_order[did]
    return {
        'query': query,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'search_terms': terms, 'core_anchors': anchors, 'priority_phrases': phrases,
        'strategy': 'STRICT_AND_THEN_CONSTRAINED_PARTIAL',
        'broad_or_enabled': bool(allow_broad), 'tier_stats': tier_counts,
        'top_k': top_k, 'per_document': per_document, 'per_section': per_section,
        'result_count': len(selected), 'unique_papers': len(paper_order),
        'note': ('These are ranked lexical matches; not validated medical evidence. '
                 'Scores from different query tiers must not be compared or added to vector scores.'),
        'results': selected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--db', type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('search', help='只读查询已有 BM25 V1 索引')
    s.add_argument('--query', required=True)
    s.add_argument('--top-k', type=int, default=10)
    s.add_argument('--per-document', type=int, default=2)
    s.add_argument('--per-section', type=int, default=1)
    s.add_argument('--kind', choices=sorted(VALID_KINDS))
    s.add_argument('--must', action='append', default=[], help='可重复：核心实体（如 ABCC8）')
    s.add_argument('--phrase', action='append', default=[], help='可重复：第一层优先精确短语（如 type 2 diabetes）；后续层放宽')
    s.add_argument('--allow-broad', action='store_true', help='显式启用最后一级宽松 OR 召回')
    s.add_argument('--no-auto-anchor', action='store_true', help='关闭 rs编号/NCT编号/字母数字基因名检测')
    s.add_argument('--pool', type=int, default=4000)
    s.add_argument('--out', type=Path, default=DEFAULT_REPORT)
    args = p.parse_args()
    root = args.root.expanduser().resolve()
    db = args.db if args.db.is_absolute() else root / args.db
    out = args.out if args.out.is_absolute() else root / args.out
    conn = open_index(db)
    try:
        result = search_index(conn, args.query, top_k=args.top_k,
            per_document=args.per_document, per_section=args.per_section,
            kind=args.kind, must=args.must, phrases=args.phrase,
            allow_broad=args.allow_broad, no_auto_anchor=args.no_auto_anchor,
            pool=args.pool)
    finally:
        conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f'查询：{result["query"]}\n核心实体：{result["core_anchors"]}\n分层统计：{result["tier_stats"]}')
    print(f'共 {result["result_count"]} 条 / {result["unique_papers"]} 篇论文')
    for item in result['results']:
        print(f'\n[{item["rank"]}] {item["document_id"]} | {item["content_kind"]} | {item["matching_tier"]}')
        print(f'  章节: {item["section_path"]} | chunk: {item["chunk_id"]}')
        print(f'  {item["text_preview"][:210].replace(chr(10), " ")}')
    print(f'\n输出：{out}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)
