"""Outcome contract: execution failures must not masquerade as evidence gaps."""
from __future__ import annotations
import unittest
from types import SimpleNamespace

from agent.outcome import compose_outcome


def coverage(ratio=1.0, covered=None, conflicts=None):
    return SimpleNamespace(ratio=ratio, covered=covered or {}, conflicts=conflicts or [])


def policy(action="answer"):
    return SimpleNamespace(action=action, reason_code="test")


class TestOutcome(unittest.TestCase):
    def test_answered_requires_full_policy_and_coverage(self):
        accepted = [{"text": "t", "citation_ids": ["E1"]}]
        drafted = {"status": "answered", "claims": accepted}
        out = compose_outcome(accepted=accepted, drafted=drafted, coverage=coverage(1.0),
                              policy=policy("answer"), error_type="", verification_method="llm",
                              coverage_threshold=1.0)
        self.assertEqual(out.status, "answered")

    def test_policy_gap_yields_partial(self):
        accepted = [{"text": "t", "citation_ids": ["E1"]}]
        drafted = {"status": "answered", "claims": accepted}
        out = compose_outcome(accepted=accepted, drafted=drafted, coverage=coverage(0.5),
                              policy=policy("partial"), error_type="", verification_method="llm",
                              coverage_threshold=1.0)
        self.assertEqual(out.status, "partial")

    def test_dropped_claim_is_reported(self):
        accepted = [{"text": "a", "citation_ids": ["E1"]}]
        drafted = {"status": "answered", "claims": accepted + [{"text": "b", "citation_ids": ["E2"]}]}
        out = compose_outcome(accepted=accepted, drafted=drafted, coverage=coverage(1.0),
                              policy=policy("answer"), error_type="", verification_method="llm",
                              coverage_threshold=1.0)
        self.assertEqual(out.status, "partial")
        self.assertEqual(out.reason_code, "claims_partially_verified")

    def test_runner_error_is_error_not_insufficient(self):
        out = compose_outcome(accepted=[], drafted=None, coverage=None, policy=policy("abstain"),
                              error_type="APIConnectionError", verification_method="not_run",
                              coverage_threshold=1.0)
        self.assertEqual(out.status, "error")
        self.assertEqual(out.reason_code, "agent_runner_error")
        self.assertEqual(out.failure_stage, "agent_runner")
        self.assertIn("不代表文献中没有证据", out.answer)

    def test_guardrail_tripwire_is_error(self):
        out = compose_outcome(accepted=[], drafted=None, coverage=coverage(0.0), policy=policy("abstain"),
                              error_type="OutputGuardrailTripwireTriggered",
                              verification_method="not_run", coverage_threshold=1.0)
        self.assertEqual(out.status, "error")
        self.assertEqual(out.reason_code, "guardrail_rejected")

    def test_verification_crash_is_error(self):
        out = compose_outcome(accepted=[], drafted={"status": "answered", "claims": [{"text": "t", "citation_ids": ["E1"]}]},
                              coverage=coverage(1.0), policy=policy("answer"),
                              error_type="", verification_method="failed_closed",
                              coverage_threshold=1.0)
        self.assertEqual(out.status, "error")
        self.assertEqual(out.reason_code, "verification_failed")

    def test_missing_draft_is_error(self):
        out = compose_outcome(accepted=[], drafted=None, coverage=coverage(1.0), policy=policy("answer"),
                              error_type="", verification_method="not_run", coverage_threshold=1.0)
        self.assertEqual(out.status, "error")
        self.assertEqual(out.reason_code, "invalid_output")

    def test_honest_refusal_stays_insufficient(self):
        out = compose_outcome(accepted=[], drafted={"status": "insufficient_evidence", "claims": []},
                              coverage=coverage(0.0), policy=policy("abstain"),
                              error_type="", verification_method="not_run", coverage_threshold=1.0)
        self.assertEqual(out.status, "insufficient_evidence")
        self.assertEqual(out.reason_code, "no_verified_claims")


if __name__ == "__main__":
    unittest.main()
