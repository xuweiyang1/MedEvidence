"""Local v3_2_trial_clean schema regression; uses ONLY in-memory SQLite and fake Qdrant."""
from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from agent.retrieval_tools import LocalQdrantRetriever, RetrievalError


class FakeQdrant:
    def __init__(self, hits):
        self.hits = hits

    def query_points(self, **kw):
        assert kw["with_payload"] is True
        return SimpleNamespace(points=self.hits[:kw["limit"]])


def point(cid, score=0.9):
    return SimpleNamespace(id=100, score=score, payload={
        "chunk_id": cid, "document_id": cid.split(":")[0],
        "corpus_version": "v3_2_trial_clean", "content_hash": "hexhash",
    })


def fixture():
    connection = sqlite3.connect(":memory:")
    connection.execute('''CREATE TABLE chunks (id INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE NOT NULL,
                       document_id TEXT, parent_id TEXT, content_kind TEXT, section_path TEXT,
                       title TEXT, doi TEXT, pmid TEXT, media_id TEXT, body_text TEXT)''')
    rows = [
        ("PMC111:parent:body:s0001:child:0001", "PMC111", "Results", "COPD treatment trial", "corticosteroids given for five days"),
        ("PMC222:parent:body:s0002:child:0002", "PMC222", "Methods", "COPD exacerbation", "randomized study comparator"),
    ]
    for cid, docid, section, title, body in rows:
        connection.execute("INSERT INTO chunks (chunk_id, document_id, section_path, title, body_text) VALUES (?, ?, ?, ?, ?)", (cid, docid, section, title, body))
    connection.execute('''CREATE VIRTUAL TABLE fts_chunks USING fts5(
        title, section_path, body_text, content='chunks', content_rowid='id', tokenize='unicode61')''')
    connection.execute("INSERT INTO fts_chunks(fts_chunks) VALUES ('rebuild')")
    return connection, rows


def retriever(db, hits):
    r = LocalQdrantRetriever.__new__(LocalQdrantRetriever)
    r.fts = db
    r.fts_table = "fts_chunks"
    r.chunks_table = r._find_chunks_table()
    r.client = FakeQdrant(hits)
    r.collection = "bge_m3_v3_2_trial_clean"
    r._encode = lambda query: [0.2] * 2
    return r


class TestV023ChunkJoin(unittest.TestCase):
    def test_dense_hydrates_text_by_stable_chunk_id(self):
        db, rows = fixture()
        try:
            r = retriever(db, [point(rows[0][0]), point(rows[1][0], .3)])
            got = r._dense("COPD", 5)
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0].id, rows[0][0])
            self.assertEqual(got[0].title, "COPD treatment trial")
            self.assertEqual(got[0].section, "Results")
            self.assertIn("five days", got[0].text)
            self.assertEqual(got[0].pmcid, "PMC111")
            self.assertNotIn("hexhash", got[0].text)
        finally:
            db.close()

    def test_external_content_bm25_join_real_chunk_id(self):
        db, rows = fixture()
        try:
            r = retriever(db, [])
            got = r._sparse("corticosteroids", 5)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].id, rows[0][0])
            self.assertEqual(got[0].source, "sqlite_fts5")
            self.assertTrue(got[0].text)
            self.assertNotIn("fts5:", got[0].id)
        finally:
            db.close()

    def test_rrf_merges_same_chunk_from_dense_and_fts(self):
        db, rows = fixture()
        try:
            r = retriever(db, [point(rows[0][0]), point(rows[1][0], .3)])
            got = r.search("corticosteroids", 5)
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0].id, rows[0][0])
            self.assertGreater(got[0].score, got[1].score)
        finally:
            db.close()

    def test_missing_id_is_not_replaced_by_fake_citation(self):
        db, rows = fixture()
        try:
            r = retriever(db, [point("PMC999:parent:body:s0000:child:0000")])
            with self.assertRaisesRegex(RetrievalError, "无法读取正文"):
                r._dense("COPD", 5)
        finally:
            db.close()

    def test_previous_in_payload_text_still_works_without_sqlite(self):
        r = LocalQdrantRetriever.__new__(LocalQdrantRetriever)
        r.fts = None
        r.chunks_table = ""
        r.collection = "test"
        r._encode = lambda q: [0.1]
        r.client = FakeQdrant([SimpleNamespace(id=7, score=0.9, payload={
            "chunk_id": "C1", "text": "published evidence"})])
        got = r._dense("trial", 1)
        self.assertEqual((got[0].id, got[0].text), ("C1", "published evidence"))

    def test_read_only_queries_do_not_modify_records(self):
        db, rows = fixture()
        try:
            r = retriever(db, [point(rows[0][0])])
            before = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            r.search("corticosteroids", 3)
            after = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            self.assertEqual(before, after)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
