#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为隔离语料构建与 medevidence_bm25_v1_1.py search 兼容的只读 SQLite FTS5 索引。

独立位置原子创建，不覆盖现有 BM25 文件。索引与旧版 BM25 的排序参数可兼容，
但文本与建库差异意味着不能把两版得分视为直接可比。
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path


def read_jsonl(p):
    with p.open(encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{p} 第{n}行损坏: {e}") from e
                if not isinstance(obj, dict):
                    raise ValueError(f"{p} 第{n}行非对象")
                yield obj


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--version", default="v3_2_trial")
    ap.add_argument("--db", default=None, help="新 SQLite 文件路径，不得指向已有文件")
    args = ap.parse_args()
    root = args.root.resolve()
    base = root / "data/processed/knowledge" / args.version
    report_path = base / "ingestion_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "ISOLATED_TRIAL_UNREVIEWED_NOT_PUBLISHED":
        raise ValueError("只对隔离试验语料建立索引")
    output = Path(args.db) if args.db else root / "data/processed/index" / args.version / "bm25_v1b.sqlite3"
    if not output.is_absolute():
        output = root / output
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有索引: {output}")
    if output.resolve() == (root / "data/processed/index/bm25_v1.sqlite3").resolve():
        raise ValueError("禁止写入旧 BM25 索引")
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp_" + uuid.uuid4().hex[:8])
    titles = {}
    for doc in read_jsonl(base / "documents.jsonl"):
        did = str(doc.get("document_id") or "")
        if not did or did in titles:
            raise ValueError("重复或空 document_id")
        titles[did] = doc
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL, parent_id TEXT, content_kind TEXT,
                section_path TEXT, title TEXT, doi TEXT, pmid TEXT, media_id TEXT,
                body_text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE fts_chunks USING fts5(
                title, section_path, body_text,
                content='chunks', content_rowid='id', tokenize='unicode61'
            );
            CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX idx_chunks_document ON chunks(document_id);
        """)
        inserted = 0
        for ch in read_jsonl(base / "retrieval_chunks.jsonl"):
            cid = str(ch.get("chunk_id") or "")
            did = str(ch.get("document_id") or "")
            text = str(ch.get("text") or "")
            if not cid or not did or not text or did not in titles:
                raise ValueError(f"Chunk 字段缺失或孤立文献: {cid}")
            doc = titles[did]
            sec = ch.get("section_path") or ch.get("section_heading_path") or ch.get("section") or ""
            if isinstance(sec, list):
                sec = " > ".join(map(str, sec))
            conn.execute("""INSERT INTO chunks(chunk_id,document_id,parent_id,content_kind,
                section_path,title,doi,pmid,media_id,body_text) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (cid, did, str(ch.get("parent_id") or ""), str(ch.get("content_kind") or ""),
                 str(sec), str(doc.get("title") or ""), str(doc.get("doi") or ""),
                 str(doc.get("pmid") or ""), str(ch.get("media_id") or ""), text))
            inserted += 1
        if inserted != report["counts"]["retrieval_chunks"]:
            raise ValueError("语料和入库报告的 Chunk 总数不一致")
        conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES ('rebuild')")
        meta = {"data_status": "LOCAL_RESEARCH_CANDIDATE", "corpus_version": args.version,
                "publication_status": "ISOLATED_TRIAL_UNREVIEWED_NOT_PUBLISHED",
                "chunk_count": str(inserted)}
        conn.executemany("INSERT INTO index_meta VALUES (?,?)", list(meta.items()))
        conn.commit()
        count = conn.execute("SELECT count(*) FROM fts_chunks").fetchone()[0]
        if count != inserted:
            raise RuntimeError("FTS5 条数与 Chunk 总数不一致")
        # Verify actual search path (including 3-column BM25 weights).
        try:
            conn.execute("SELECT bm25(fts_chunks, 2.0,1.3,1.0) FROM fts_chunks WHERE fts_chunks MATCH 'kidney' LIMIT 1").fetchall()
        except sqlite3.Error as e:
            raise RuntimeError("FTS5 bm25() 功能不可用") from e
    except BaseException:
        conn.close()
        if tmp.exists():
            tmp.unlink()
        raise
    conn.close()
    os.replace(tmp, output)
    print(f"新 BM25 索引：{output}\nChunk 行数：{inserted}\n状态：隔离试验库（未发布）")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
