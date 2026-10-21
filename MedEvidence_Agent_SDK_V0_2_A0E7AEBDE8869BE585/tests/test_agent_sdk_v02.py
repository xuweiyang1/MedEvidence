"""Offline tests for new root detection, SDK guard contract and local metadata trace."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from agent.project_locator import find_project_root
from agent.sdk_runtime import validate_draft, LocalMetadataTraceProcessor, SDKState
from agent.models import Evidence
from agent.retrieval_tools import discover_indices


class TestAgentSDKV02(unittest.TestCase):
    def test_nested_extraction_detects_real_project_root(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "MedEvidence"
            nested = p / "MedEvidence_Agent_V0_1_badfolder"
            (p / "scripts").mkdir(parents=True)
            (p / "scripts" / "index_coordinator.py").write_text("", encoding="utf-8")
            nested.mkdir()
            self.assertTrue(find_project_root(nested).samefile(p))

    def test_project_root_override(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(find_project_root(Path("."), t), Path(t).resolve())

    def test_real_qdrant_meta_scanned(self):
        with tempfile.TemporaryDirectory() as t:
            q = Path(t) / "indexes" / "v3_2_trial_clean" / "qdrant"
            q.mkdir(parents=True)
            (q / "meta.json").write_text("{}", encoding="utf-8")
            # Current Qdrant storage layout may include collections folder.
            (q / "collections").mkdir()
            self.assertIn(str(q), discover_indices(Path(t))["qdrant_candidates"])

    def test_valid_draft_and_no_fabricated_id(self):
        data = {"status": "answered", "claims": [{"text": "statement", "citation_ids": ["E1"]}], "limitations": []}
        valid, why = validate_draft(json.dumps(data), {"E1"})
        self.assertEqual(why, "ok")
        self.assertEqual(valid["status"], "answered")
        self.assertIsNone(validate_draft(json.dumps(data), {"E2"})[0])

    def test_uncited_claim_denied(self):
        data = {"status": "answered", "claims": [{"text": "statement", "citation_ids": []}]}
        self.assertIsNone(validate_draft(json.dumps(data), {"E1"})[0])

    def test_no_claims_without_valid_status(self):
        self.assertIsNone(validate_draft('{"status":"other","claims":[]}', set())[0])
        self.assertIsNone(validate_draft('{"status":"insufficient_evidence","claims":[{"text":"bad","citation_ids":["E1"]}]}', {"E1"})[0])

    def test_trace_processor_never_stores_prompts(self):
        class FakeSpan:
            span_id = "span_1"
            parent_id = "parent"
            span_data = type("span_data", (), {"type": "generation", "input": "medical private details"})()
        c = LocalMetadataTraceProcessor()
        c.on_span_start(FakeSpan())
        c.on_span_end(FakeSpan())
        self.assertNotIn("medical private details", json.dumps(c.events))
        self.assertIn('"span_type": "generation"', json.dumps(c.events))

if __name__ == "__main__":
    unittest.main()
