"""Build new, standalone BM25/FTS5 from snapshot/chunks.jsonl, never old knowledge tree."""
from __future__ import annotations
import argparse
import os
import sqlite3
import uuid
from pathlib import Path
from v1c2_common import snapshot, bm25_files, atomic_json, file_hash


def build(root: Path, version: str):
    folder, index, m, docs, chunks, hashes = snapshot(root, version)
    dest, mf = bm25_files(index)
    if dest.exists() or mf.exists():
        raise FileExistsError(f'拒绝覆盖已有索引或元数据: {dest}, {mf}')
    index.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name('.bm25.' + uuid.uuid4().hex + '.sqlite3')
    conn = None
    try:
        conn = sqlite3.connect(temp)
        conn.executescript('''
            PRAGMA journal_mode=DELETE;
            CREATE TABLE chunks(id INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL, parent_id TEXT, content_kind TEXT, section_path TEXT,
                title TEXT, doi TEXT, pmid TEXT, media_id TEXT, body_text TEXT NOT NULL);
            CREATE VIRTUAL TABLE fts_chunks USING fts5(title,section_path,body_text,
                content='chunks',content_rowid='id',tokenize='unicode61');
            CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX idx_chunks_doc ON chunks(document_id);
        ''')
        for cid in sorted(chunks):
            ch = chunks[cid]; did = ch['document_id']; doc = docs[did]
            section = ch.get('section_path') or ch.get('section_heading_path') or ch.get('section') or ''
            if isinstance(section, list):
                section = ' > '.join(str(x) for x in section)
            conn.execute('''INSERT INTO chunks(chunk_id,document_id,parent_id,content_kind,
                   section_path,title,doi,pmid,media_id,body_text) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                 (cid,did,str(ch.get('parent_id') or ''),str(ch.get('content_kind') or ''),str(section),
                  str(doc.get('title') or ''),str(doc.get('doi') or ''),str(doc.get('pmid') or ''),
                  str(ch.get('media_id') or ''),ch['text']))
        conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")
        metadata = {
            'data_status':'LOCAL_RESEARCH_CANDIDATE', 'publication_status':'CANDIDATE_NOT_PUBLISHED',
            'corpus_version':version,'chunks_sha256':m['chunks_sha256'],
            'documents_sha256':m['documents_sha256'],
            'chunk_id_set_sha256':m['chunk_id_set_sha256'],
            'chunk_count':str(len(chunks)),'schema_version':'MedEvidence_BM25_V1c2',
        }
        conn.executemany('INSERT INTO index_meta VALUES (?,?)', sorted(metadata.items()))
        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity_check 失败')
        if conn.execute('SELECT count(*) FROM fts_chunks').fetchone()[0] != len(chunks):
            raise RuntimeError('FTS 行数不匹配')
        conn.execute('SELECT bm25(fts_chunks,2.0,1.3,1.0) FROM fts_chunks WHERE fts_chunks MATCH ?',('medical',)).fetchall()
        conn.close(); conn = None
        if dest.exists():
            raise FileExistsError(dest)
        os.rename(temp, dest)
        atomic_json(mf, {'schema':'MedEvidence_BM25_IndexManifest_V1c2',
              'corpus_version':version,'chunk_count':len(chunks),
              'chunks_sha256':m['chunks_sha256'],'documents_sha256':m['documents_sha256'],
              'chunk_id_set_sha256':m['chunk_id_set_sha256'],
              'db_sha256':file_hash(dest), 'db_name':dest.name,
              'status':'BUILT_UNPUBLISHED'})
        return dest
    finally:
        if conn is not None: conn.close()
        temp.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--version',required=True)
    a=p.parse_args()
    print('BM25 新索引:',build(a.root,a.version))

if __name__ == '__main__':
    main()
