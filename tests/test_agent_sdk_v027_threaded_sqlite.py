"""Regression tests: SDK tools run in worker threads, unlike smoke CLI.

Uses a temporary on-disk SQLite FTS5 DB and a fake local Qdrant client.
No model downloads, no API requests, and no data/index modifications.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from agent.retrieval_tools import LocalQdrantRetriever

TITLE = "Real-world effectiveness of dupilumab in COPD: a German multicenter analysis"
CID = "PMC13640210:parent:body:s0010:child:0010"


class QdrantStub:
    def __init__(self, *, path):
        self.path = path

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name="bge_m3_v3_2_trial_clean")])

    def query_points(self, *, collection_name, query, limit, with_payload):
        assert collection_name == "bge_m3_v3_2_trial_clean"
        assert with_payload is True
        return SimpleNamespace(points=[SimpleNamespace(
            id=1, score=0.91, payload={"chunk_id": CID, "document_id": "PMC13640210"})][:limit])

    def close(self):
        pass


class TestSDKThreadedSQLiteV027(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "qdrant").mkdir()
        dbpath = root / "bm25.sqlite3"
        con = sqlite3.connect(dbpath)
        con.executescript('''
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            chunk_id TEXT UNIQUE,
            document_id TEXT,
            title TEXT,
            section_path TEXT,
            body_text TEXT
        );
        CREATE VIRTUAL TABLE fts_chunks USING fts5(
            title,section_path,body_text,
            content='chunks',content_rowid='id',tokenize='unicode61'
        );
        ''')
        con.execute('INSERT INTO chunks(chunk_id,document_id,title,section_path,body_text) VALUES(?,?,?,?,?)',
                    (CID,"PMC13640210", TITLE, "Discussion",
                     "Several limitations should be acknowledged. This was a retrospective real-world "
                     "analysis without a control group. Corticosteroids were background therapy."))
        con.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")
        con.commit()
        con.close()
        cfg = SimpleNamespace(root=root, qdrant_path=str(root / "qdrant"),
                              bm25_db=str(dbpath), collection="bge_m3_v3_2_trial_clean",
                              embed_model="unused", device="cpu")
        fake_module = ModuleType("qdrant_client")
        fake_module.QdrantClient = QdrantStub
        with patch.dict(sys.modules, {"qdrant_client": fake_module}):
            self.r = LocalQdrantRetriever(cfg)
        self.r._encode = lambda query: [0.1] * 1024
        self.addCleanup(self.r.fts.close)

    def test_document_lookup_from_sdk_worker_thread(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            got = pool.submit(self.r.lookup_document,
                              "Real-world effectiveness of dupilumab in COPD", 8,
                              "study limitations").result(timeout=10)
        self.assertGreater(len(got), 0)
        self.assertEqual(got[0].id, CID)
        self.assertTrue(got[0].text.startswith("Several limitations"))

    def test_dense_and_bm25_combined_in_worker_thread(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            got = pool.submit(self.r.search, "COPD corticosteroids", 5).result(timeout=10)
        self.assertEqual(got[0].id, CID)
        self.assertIn("without a control group", got[0].text)

    def test_parallel_sdk_search_and_lookup_do_not_share_unsafe_cursor(self):
        def operation(i):
            if i % 2:
                return self.r.search("COPD corticosteroids", 5)[0].id
            return self.r.lookup_document("Real-world effectiveness of dupilumab in COPD", 6)[0].id
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(operation, range(16)))
        self.assertEqual(ids, [CID] * 16)

    def test_read_only_sqlite_mode_not_weakened(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.r.fts.execute("DELETE FROM chunks")

    def test_asyncio_to_thread_same_as_sdk_tool_path(self):
        async def run():
            return await asyncio.to_thread(self.r.lookup_document, "Real-world effectiveness of dupilumab in COPD", 4, "limitations")
        got = asyncio.run(run())
        self.assertEqual(got[0].id, CID)


if __name__ == '__main__':
    unittest.main()
