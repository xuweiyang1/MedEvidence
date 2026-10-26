"""Offline regression tests: python scripts/run_agent.py --self-test."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from agent.models import Evidence, Coverage
from agent.llm_client import parse_json, LLMError
from agent.retrieval_tools import normalize, discover_indices, LocalQdrantRetriever
from agent.evidence_evaluator import EvidenceEvaluator
from agent.decision_policy import decide
from agent.citation_verifier import CitationVerifier
from agent.orchestrator import MedEvidenceAgent


class FakeLLM:
    def __init__(self):
        self.calls = []

    def json(self, system, user, max_tokens=1000):
        req = json.loads(user)
        self.calls.append((system, req))
        if "task" in req and "question" in req and "already_searched" not in req:
            return {"aspects": ["疗效", "适用限制"], "queries": ["initial query"], "high_risk": False}
        if "already_searched" in req:
            return {"queries": ["followup query"]}
        if "aspects" in req and "evidence" in req:
            ids = [x["id"] for x in req["evidence"]]
            if len(ids) == 1:
                return {"items": [{"aspect": "疗效", "supported": True, "ids": ids}],
                        "gaps": ["适用限制"], "conflicts": []}
            return {"items": [{"aspect": "疗效", "supported": True, "ids": [ids[0]]},
                              {"aspect": "适用限制", "supported": True, "ids": [ids[-1]]}],
                    "gaps": [], "conflicts": []}
        if "question" in req and "evidence" in req:
            return {"status": "answered", "claims": [
                {"text": "某文献描述了目标研究结局。", "citation_ids": ["E1"]},
                {"text": "没有来源的错误主张", "citation_ids": ["BOGUS"]}],
                "limitations": ["示例测试，不代表医学结论"]}
        if "claims" in req:
            return {"checks": [{"index": 0, "verdict": "supported", "reason": "引用文字匹配"}]}
        raise AssertionError(f"unexpected fake LLM request: {req}")


class FakeRetriever:
    def search(self, query, top_k):
        if query == "initial query":
            return [Evidence(id="PMC-1-C1", text="模拟文献片段 A，仅用于程序单元测试", title="Test A")]
        if query == "followup query":
            return [Evidence(id="PMC-2-C2", text="模拟文献片段 B，仅用于程序单元测试", title="Test B")]
        return []


def cfg(root):
    return SimpleNamespace(root=root, log_dir=root / "outputs" / "agent_runs", model="fake",
                           retriever="hook", collection="unit_test", max_rounds=3,
                           top_k=5, max_evidence=10, coverage_threshold=1.0,
                           trace_content=False, semantic_verify=True)


class TestAgent(unittest.TestCase):
    def test_json_parsing(self):
        self.assertEqual(parse_json('```json\n{"a":1}\n```'), {"a": 1})
        with self.assertRaises(LLMError):
            parse_json("no json here")

    def test_normalize_metadata(self):
        x = normalize({"metadata": {"chunk_id": "C1", "pmcid": "PMC1", "text": "evidence"}, "score": .2})
        self.assertEqual((x.id, x.text, x.pmcid), ("C1", "evidence", "PMC1"))
        self.assertIsNone(normalize({"id": "x"}))

    def test_decision_coverage(self):
        self.assertEqual(decide(Coverage(ratio=0, gaps=["x"]), 1, 3, 1.0).action, "search_again")
        self.assertEqual(decide(Coverage(ratio=0, gaps=["x"]), 3, 3, 1.0).action, "abstain")
        self.assertEqual(decide(Coverage(ratio=1, covered={"x": ["E1"]}), 1, 3, 1.0).action, "answer")

    def test_verifier_rejects_unlisted_and_unsupported(self):
        class VerifierLLM:
            def json(self, system, user, max_tokens=1900):
                return {"checks": [{"index": 0, "verdict": "unsupported"}]}
        verifier = CitationVerifier(VerifierLLM(), True)
        got, details = verifier.verify([
            {"text": "unproven", "citation_ids": ["E1"]},
            {"text": "unknown cite", "citation_ids": ["F999"]},
        ], [Evidence("E1", "original")])
        self.assertFalse(got)
        self.assertEqual(details["valid"], 0)
        self.assertEqual([x["verdict"] for x in details["checks"]], ["unsupported", "invalid_citation"])

    def test_coverage_ignores_fake_ids(self):
        class Bad:
            def json(self, *args):
                return {"items": [{"aspect": "a", "supported": True, "ids": ["E900"]}], "gaps": []}
        res = EvidenceEvaluator(Bad()).evaluate(["a"], [Evidence("E1", "sample")])
        self.assertEqual(res.ratio, 0)
        self.assertIn("a", res.gaps)

    def test_end_to_end_two_rounds_and_private_trace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = MedEvidenceAgent(FakeLLM(), FakeRetriever(), cfg(root)).run("测试医学问题私有文本")
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.coverage_ratio, 1.0)
            self.assertEqual(len(result.claims), 1)
            self.assertIn("[E1]", result.answer)
            self.assertNotIn("BOGUS", result.answer)
            self.assertEqual(result.evidence[0]["source_chunk_id"], "PMC-1-C1")
            log = Path(result.trace_path).read_text(encoding="utf-8")
            self.assertNotIn("测试医学问题私有文本", log)
            self.assertIn('"phase": "decision"', log)
            self.assertIn("llm_assisted_not_independent", log)

    def test_no_docs_no_made_up_answer(self):
        class Empty:
            def search(self, query, top_k):
                return []
        with tempfile.TemporaryDirectory() as d:
            c = cfg(Path(d))
            c.max_rounds = 1
            result = MedEvidenceAgent(FakeLLM(), Empty(), c).run("医学示例")
            self.assertEqual(result.status, "insufficient_evidence")
            self.assertEqual(result.claims, [])
            self.assertEqual(result.evidence, [])

    def test_sqlite_fts5_query_and_rowid(self):
        obj = LocalQdrantRetriever.__new__(LocalQdrantRetriever)
        obj.fts = sqlite3.connect(":memory:")
        obj.fts.execute("CREATE VIRTUAL TABLE evidence_fts USING fts5(text)")
        obj.fts.execute("INSERT INTO evidence_fts(text) VALUES (?)", ("hypertension treatment trial",))
        obj.fts_table = "evidence_fts"
        found = obj._sparse("hypertension", 3)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].id, "fts5:evidence_fts:1")
        self.assertIn("treatment", found[0].text)

    def test_probe_empty_root(self):
        with tempfile.TemporaryDirectory() as d:
            found = discover_indices(Path(d))
            self.assertEqual(found["qdrant_candidates"], [])
            self.assertEqual(found["sqlite_candidates"], [])


if __name__ == "__main__":
    unittest.main()
