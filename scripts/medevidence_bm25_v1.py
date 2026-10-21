#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence BM25 baseline: SQLite FTS5, document / section / chunk fields.

Windows / PyCharm / Python 3.11, no external dependencies.

Place under MedEvidence/scripts, then:
    python scripts/medevidence_bm25_v1.py build
    python scripts/medevidence_bm25_v1.py search --query "ABCC8 polymorphism type 2 diabetes fasting glucose"
    python scripts/medevidence_bm25_v1.py search --query "chronic kidney disease glomerular filtration rate" --top-k 10

This is a retrieval baseline, not a medical evidence-validity classifier.
The resulting database is for local research on CANDIDATE data, not redistribution.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

INPUT_FOLDER = Path('data/processed/knowledge/v3_1_all')
DEFAULT_DB = Path('data/processed/index/bm25_v1.sqlite3')
DEFAULT_REPORT = Path('reports/bm25_v1_build_summary.txt')
DEFAULT_SEARCH_REPORT = Path('reports/bm25_v1_search_results.json')
STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can', 'do', 'does', 'for',
    'from', 'how', 'in', 'is', 'it', 'of', 'on', 'or', 'the', 'this', 'to', 'was',
    'were', 'what', 'which', 'who', 'with', 'would', 'among', 'between', 'than',
    'that', 'these', 'those', 'patient', 'patients', 'study', 'studies'
}
TOKEN_RE = re.compile(r'[^\W_]+(?:_[^\W_]+)*', flags=re.UNICODE)
VALID_KINDS = {'abstract', 'body', 'table', 'figure_caption'}


def get_db(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_test USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_test")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError('你的Python内置SQLite缺少FTS5，请更换含FTS5的Python 3.11发行版。') from exc


def safe_json_lines(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f'缺少文件：{path}')
    with path.open('r', encoding='utf-8-sig') as stream:
        for lineno, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'{path} 第 {lineno} 行 JSON 无效') from exc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def section_text(chunk: dict) -> str:
    value = chunk.get('section_heading_path') or []
    if isinstance(value, list):
        return ' > '.join(str(x) for x in value)
    if value:
        return str(value)
    return str(chunk.get('section_id') or '')


def build(root: Path, out_db: Path, report: Path) -> None:
    docs_path = root / INPUT_FOLDER / 'documents.jsonl'
    chunks_path = root / INPUT_FOLDER / 'retrieval_chunks.jsonl'
    docs = {}
    for doc in safe_json_lines(docs_path):
        did = doc.get('document_id', '')
        if not did or did in docs:
            raise ValueError(f'文献 ID 缺失或重复：{did!r}')
        if doc.get('scope') != 'LOCAL_RESEARCH_CANDIDATE':
            raise ValueError(f'{did} 不属于本地候选文献，已停止建库')
        docs[did] = doc
    if not docs:
        raise ValueError('documents.jsonl 为空')

    out_db.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    temp_db = out_db.with_name(out_db.name + '.building')
    if temp_db.exists():
        temp_db.unlink()
    conn = sqlite3.connect(temp_db)
    cnt = collections.Counter()
    try:
        get_db(conn)
        conn.executescript('''
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY,
                chunk_id TEXT UNIQUE NOT NULL,
                document_id TEXT NOT NULL,
                parent_id TEXT NOT NULL,
                content_kind TEXT NOT NULL,
                section_path TEXT NOT NULL,
                title TEXT NOT NULL,
                doi TEXT,
                pmid TEXT,
                media_id TEXT,
                body_text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE fts_chunks USING fts5(
                title, section_path, body_text,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX idx_chunks_doc ON chunks(document_id);
            CREATE INDEX idx_chunks_parent ON chunks(parent_id);
        ''')
        # Chunk text is used exactly as emitted by V3.1. Do not add tags to it.
        with conn:
            for record in safe_json_lines(chunks_path):
                chunk_id = str(record.get('chunk_id') or '')
                did = str(record.get('document_id') or '')
                parent_id = str(record.get('parent_id') or '')
                kind = str(record.get('content_kind') or '')
                content = str(record.get('text') or '')
                if not chunk_id or not parent_id or did not in docs or kind not in VALID_KINDS or not content.strip():
                    raise ValueError(f'不合法的Chunk: {chunk_id!r} / {did!r} / {kind!r}')
                rights = record.get('rights_status')
                if rights != 'CANDIDATE_LOCAL_RESEARCH_ONLY':
                    raise ValueError(f'Chunk 状态不符，拒绝入库: {chunk_id}')
                doc = docs[did]
                heading = section_text(record)
                title = str(doc.get('title') or '')
                cursor = conn.execute('''
                    INSERT INTO chunks(chunk_id,document_id,parent_id,content_kind,section_path,title,doi,pmid,media_id,body_text)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (chunk_id, did, parent_id, kind, heading, title,
                     str(doc.get('doi') or ''), str(doc.get('pmid') or ''),
                     str(record.get('media_id') or ''), content))
                rowid = cursor.lastrowid
                conn.execute('INSERT INTO fts_chunks(rowid,title,section_path,body_text) VALUES(?,?,?,?)',
                             (rowid, title, heading, content))
                cnt[kind] += 1
                if sum(cnt.values()) % 5000 == 0:
                    print(f'  已索引 {sum(cnt.values()):,} 条 Chunk')
            if not cnt:
                raise ValueError('未找到任何可索引 Chunk')
            metadata = {
                'created_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'documents': str(len(docs)),
                'chunks': str(sum(cnt.values())),
                'source_chunks_sha256': sha256_file(chunks_path),
                'source_documents_sha256': sha256_file(docs_path),
                'bm25_fields': 'title:2.0;section_path:1.3;body_text:1.0',
                'data_status': 'LOCAL_RESEARCH_CANDIDATE',
            }
            conn.executemany('INSERT INTO index_meta(key,value) VALUES(?,?)', metadata.items())
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise RuntimeError(f'SQLite 完整性失败: {integrity}')
        num_c = conn.execute('SELECT count(*) FROM chunks').fetchone()[0]
        num_f = conn.execute('SELECT count(*) FROM fts_chunks').fetchone()[0]
        if num_c != num_f or num_c != sum(cnt.values()):
            raise RuntimeError('FTS5 索引条数与文档表不一致')
    except BaseException:
        conn.close()
        if temp_db.exists():
            temp_db.unlink()
        raise
    else:
        conn.close()
        temp_db.replace(out_db)
    summary = [
        '=== MedEvidence BM25 V1 构建摘要 ===',
        f'文献数量: {len(docs)}',
        f'Chunk 数量: {sum(cnt.values())}',
        f'Chunk 类型: {dict(cnt)}',
        '数据库与FTS索引数量一致: YES',
        'SQLite integrity_check: ok',
        f'索引位置: {out_db}',
        '索引字段: title ×2.0; section ×1.3; text ×1.0 (仅实验初值)',
        '数据范围: PMC CANDIDATE 本地实验；未索引 WHO/CDC',
        '提醒: 检索相关性不代表医学证据适用性或证据等级。'
    ]
    report.write_text('\n'.join(summary)+'\n', encoding='utf-8-sig')
    print('\n'.join(summary))
    print(f'报告: {report}')


def tokenize_query(query: str) -> list[str]:
    tokens = TOKEN_RE.findall(query.lower())
    filtered = [x for x in tokens if x not in STOPWORDS]
    if not filtered:
        filtered = tokens
    # Preserve first appearance, avoid accidentally huge FTS expressions.
    return list(dict.fromkeys(filtered))[:20]


def search(root: Path, db: Path, q: str, top_k: int, per_document: int,
           kind: str | None, out_file: Path) -> list[dict]:
    if not db.is_file():
        raise FileNotFoundError(f'请先运行 build 创建索引: {db}')
    terms = tokenize_query(q)
    if not terms:
        raise ValueError('查询必须含有可搜索的词')
    conn = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        get_db(conn)
        sql = '''SELECT c.chunk_id, c.document_id, c.parent_id, c.content_kind,
                        c.section_path, c.title, c.doi, c.pmid, c.media_id, c.body_text,
                        bm25(fts_chunks, 2.0, 1.3, 1.0) AS bm25_raw
                 FROM fts_chunks JOIN chunks AS c ON c.id = fts_chunks.rowid
                 WHERE fts_chunks MATCH ?'''
        tail = (' AND c.content_kind = ?' if kind else '') + ' ORDER BY bm25_raw ASC LIMIT ?'
        max_pool = min(10000, max(300, top_k * per_document * 15))
        def retrieve(expr):
            params = [expr] + ([kind] if kind else []) + [max_pool]
            return conn.execute(sql+tail, params).fetchall()
        # Try conjunction first for precision; fallback to disjunction for recall.
        safe = ['"' + x.replace('"', '""') + '"' for x in terms]
        candidates = retrieve(' AND '.join(safe)) if len(safe) > 1 else retrieve(safe[0])
        strategy = 'AND'
        if len(candidates) < top_k:
            candidates = retrieve(' OR '.join(safe))
            strategy = 'OR_FALLBACK'
        doc_counter = collections.Counter()
        found = []
        for row in candidates:
            did = row['document_id']
            if doc_counter[did] >= per_document:
                continue
            doc_counter[did] += 1
            record = dict(row)
            # SQLite FTS5 BM25 scores are ranking values; lower is better. Do not call them probabilities.
            record['text_preview'] = record.pop('body_text')[:1200]
            record['bm25_raw'] = float(record['bm25_raw'])
            found.append(record)
            if len(found) >= top_k:
                break
        output = {
            'query': q, 'match_strategy': strategy,
            'note': 'BM25 score is not a probability; relevance and medical claims need separate validation.',
            'result_count': len(found), 'results': found,
        }
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(f'检索模式: {strategy}, 搜索词: {terms}, 返回 {len(found)} 条')
        for i, item in enumerate(found, 1):
            print(f'\n[{i}] {item["document_id"]} · {item["content_kind"]} · BM25原始分数={item["bm25_raw"]:.5f}')
            print(f'  {item["title"][:105]}')
            print(f'  章节: {item["section_path"]}')
            print(f'  Chunk: {item["chunk_id"]}')
            print(f'  摘要: {item["text_preview"][:240].replace(chr(10), " ")}')
        print(f'\n保存检索明细: {out_file}')
        return found
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--db', type=Path, default=DEFAULT_DB,
                   help='数据库路径；相对路径相对于项目根目录')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('build', help='构建本地 BM25 FTS5 索引')
    s = sub.add_parser('search', help='查询 BM25 索引')
    s.add_argument('--query', required=True, help='优先用英文医学术语')
    s.add_argument('--top-k', type=int, default=10)
    s.add_argument('--per-document', type=int, default=3)
    s.add_argument('--kind', choices=sorted(VALID_KINDS))
    args = p.parse_args()
    if args.command == 'search' and (args.top_k < 1 or args.per_document < 1):
        p.error('top-k 和 per-document 必须 >= 1')
    root = args.root.expanduser().resolve()
    db = args.db if args.db.is_absolute() else root / args.db
    if args.command == 'build':
        build(root, db, root / DEFAULT_REPORT)
    else:
        search(root, db, args.query, args.top_k, args.per_document, args.kind, root / DEFAULT_SEARCH_REPORT)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(2)
