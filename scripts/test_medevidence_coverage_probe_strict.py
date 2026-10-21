import sqlite3
import tempfile
import unittest
from pathlib import Path

from medevidence_coverage_probe_strict import compile_term, context, main


class StrictCoverageProbeTests(unittest.TestCase):
    def test_token_boundary_excludes_stem_false_positives(self):
        dapt = compile_term("DAPT")
        self.assertIsNotNone(dapt.search("Dual antiplatelet therapy (DAPT)"))
        self.assertIsNone(dapt.search("adapted treatment protocol"))

        pylori = compile_term("pylori")
        self.assertIsNotNone(pylori.search("H. pylori infection"))
        self.assertIsNone(pylori.search("pyloric stenosis"))

        phrase = compile_term("test of cure")
        self.assertIsNotNone(phrase.search("test-of-cure was documented"))

    def test_context_is_centred_on_match(self):
        value = "prefix " + ("x" * 20) + "DAPT" + ("y" * 20) + " suffix"
        start = value.index("DAPT")
        snippet = context(value, start, start + 4, 5)
        self.assertIn("DAPT", snippet)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))

    def test_sqlite_probe_reports_strict_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "bm25.sqlite3"
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE chunks (chunk_id TEXT, document_id TEXT, body_text TEXT)"
            )
            connection.executemany(
                "INSERT INTO chunks VALUES (?, ?, ?)",
                [
                    ("c1", "d1", "DAPT is discussed."),
                    ("c2", "d2", "The patient was adapted to treatment."),
                    ("c3", "d3", "H. pylori eradication was studied."),
                    ("c4", "d4", "Pyloric stenosis is a different condition."),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(main(["--db", str(db), "--case", "Q15"]), 0)
            # The CLI output is intentionally not parsed here; direct regex
            # assertions above protect the false-positive failure mode.


if __name__ == "__main__":
    unittest.main()
