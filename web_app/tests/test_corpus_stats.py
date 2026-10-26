"""Corpus metrics are read from the configured index, not hardcoded in the UI."""
from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import corpus_stats


class TestCorpusStats(unittest.TestCase):
    def tearDown(self):
        corpus_stats.cache_clear()

    def test_reads_manifest_and_documents(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            version = root / "index_versions" / "v9_test"
            (version / "qdrant").mkdir(parents=True)
            (version / "dense_manifest.json").write_text('{"chunk_count": 42}', encoding="utf-8")
            knowledge = root / "knowledge" / "v9_test"
            knowledge.mkdir(parents=True)
            (knowledge / "documents.jsonl").write_text("{}\n{}\n{}\n", encoding="utf-8")
            (root / ".env.agent").write_text(
                "MEDEVIDENCE_QDRANT_PATH=" + str(version / "qdrant") + "\n"
                "MEDEVIDENCE_COLLECTION=bge_test\n", encoding="utf-8")
            stats = corpus_stats(root)
            self.assertEqual(stats["papers"], 3)
            self.assertEqual(stats["chunks"], 42)
            self.assertEqual(stats["version"], "v9_test")
            self.assertEqual(stats["collection"], "bge_test")

    def test_missing_config_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            stats = corpus_stats(Path(td))
            self.assertIsNone(stats["papers"])
            self.assertIsNone(stats["chunks"])


if __name__ == "__main__":
    unittest.main()
