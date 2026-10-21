"""Offline regression tests for final-coverage refresh and strict refusal policy."""
import asyncio
import json
import unittest
from types import SimpleNamespace

from agent.evidence_evaluator import EvidenceEvaluator
from agent.models import Coverage, Evidence
from agent.sdk_runtime import SDKState, guardrail_decision, recheck_stale_coverage


class TestAgentSDKV022Patch(unittest.TestCase):
    def test_safe_refusal_without_coverage(self):
        raw = json.dumps({"status": "insufficient_evidence", "claims": [], "limitations": ["no evidence"]})
        valid, why, draft = guardrail_decision(raw, set(), coverage_present=False)
        self.assertTrue(valid)
        self.assertEqual(why, "safe_refusal")
        self.assertEqual(draft["claims"], [])

    def test_cited_claim_without_coverage_rejected(self):
        raw = json.dumps({"status": "answered", "claims": [{"text": "assertion", "citation_ids": ["E1"]}]})
        valid, why, _ = guardrail_decision(raw, {"E1"}, coverage_present=False)
        self.assertFalse(valid)
        self.assertEqual(why, "coverage_missing_after_latest_search")

    def test_invalid_citation_cannot_skip_guardrail(self):
        raw = json.dumps({"status": "answered", "claims": [{"text": "assertion", "citation_ids": ["E9"]}]})
        valid, why, _ = guardrail_decision(raw, {"E1"}, coverage_present=True)
        self.assertFalse(valid)
        self.assertIn("invalid_draft", why)

    def test_coverage_index_mapped_and_whitelisted(self):
        class FakeLLM:
            def json(self, sys, user, limit):
                body = json.loads(user)
                self_evidence = body["evidence"]
                assert self_evidence[0]["id"] == "E1"
                return {"items": [{"index": 0, "aspect": "paraphrase", "supported": True, "ids": ["E1", "E99"]}],
                        "gaps": [], "conflicts": []}
        evaluator = EvidenceEvaluator(FakeLLM())
        cov = evaluator.evaluate(["5 day duration"], [Evidence("E1", "5 days of treatment")])
        self.assertEqual(cov.covered, {"5 day duration": ["E1"]})
        self.assertEqual(cov.ratio, 1.0)

    def test_bad_index_and_fabricated_id_dont_earn_coverage(self):
        class FakeLLM:
            def json(self, sys, user, limit):
                return {"items": [{"index": 99, "aspect": "paraphrase", "supported": True, "ids": ["E1"]},
                                  {"index": 0, "supported": True, "ids": ["E999"]}]}
        cov = EvidenceEvaluator(FakeLLM()).evaluate(["5 day duration"], [Evidence("E1", "5 days")])
        self.assertEqual(cov.ratio, 0.0)

    def test_recheck_without_aspects_does_not_hallucinate_coverage(self):
        state = SDKState(config=SimpleNamespace(), retriever=None, evaluator=None)
        self.assertFalse(asyncio.run(recheck_stale_coverage(state)))
        self.assertIsNone(state.coverage)
        self.assertEqual(state.events[-1]["reason"], "no_aspects")

    def test_stale_coverage_is_refreshed(self):
        class Evaluator:
            def evaluate(self, aspects, ev):
                return Coverage(covered={aspects[0]: [ev[0].id]}, ratio=1.0)
        state = SDKState(config=SimpleNamespace(), retriever=None, evaluator=Evaluator())
        state.aspects = ["duration"]
        state.evidence = {"E1": Evidence("E1", "5 days")}
        self.assertTrue(asyncio.run(recheck_stale_coverage(state)))
        self.assertEqual(state.coverage.ratio, 1.0)
        self.assertEqual(state.events[-1]["kind"], "coverage_auto_recheck")

if __name__ == "__main__":
    unittest.main()
