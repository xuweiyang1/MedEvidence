"""Outcome composition for a finished agent run.

The distinction that matters to users is *why* no medical conclusion was
produced:

- ``error``               the run itself failed (network, SDK, guardrail, contract)
- ``insufficient_evidence`` the run completed but verified evidence was lacking

Keeping these separate prevents an API outage from being reported as
"the literature does not support an answer". This module is deliberately pure
so the rules can be unit-tested without an API key, SDK, or index.
"""
from __future__ import annotations

from dataclasses import dataclass

ERROR_ANSWER = (
    "本次运行未完成（{code}），因此未生成医学结论。"
    "这通常与网络、模型 API 或输出校验有关，不代表文献中没有证据。"
)
REFUSAL_ANSWER = "未获得足够且通过引用核验的医学证据，因此不生成医学结论。"


@dataclass(frozen=True)
class Outcome:
    status: str          # answered | partial | insufficient_evidence | error
    reason_code: str
    failure_stage: str   # empty unless status == "error"
    answer: str          # only used when no verified claim survives


def compose_outcome(
    *,
    accepted: list[dict],
    drafted: dict | None,
    coverage,
    policy,
    error_type: str,
    verification_method: str,
    coverage_threshold: float,
    retrieval_successes: int = 1,
    retrieval_errors: int = 0,
    coverage_errors: int = 0,
    online_errors: int = 0,
    online_successes: int = 0,
) -> Outcome:
    """Decide the public status. ``accepted`` claims are already verified."""
    claims = (drafted or {}).get("claims") or []

    if accepted:
        answered = (
            (drafted or {}).get("status") == "answered"
            and policy is not None
            and policy.action == "answer"
            and coverage is not None
            and coverage.ratio >= coverage_threshold
            and not coverage.conflicts
            and len(accepted) == len(claims)
        )
        if answered:
            return Outcome("answered", "policy_answer", "", "")
        reason = "policy_not_answer"
        if len(accepted) != len(claims):
            reason = "claims_partially_verified"
        elif coverage is not None and coverage.conflicts:
            reason = "conflict_reported"
        elif coverage is None or coverage.ratio < coverage_threshold:
            reason = "coverage_below_threshold"
        return Outcome("partial", reason, "", "")

    if error_type:
        if "Guardrail" in error_type or "Tripwire" in error_type:
            return Outcome("error", "guardrail_rejected", "output_guardrail",
                           ERROR_ANSWER.format(code=error_type))
        return Outcome("error", "agent_runner_error", "agent_runner",
                       ERROR_ANSWER.format(code=error_type))
    if verification_method == "failed_closed":
        return Outcome("error", "verification_failed", "citation_verification",
                       ERROR_ANSWER.format(code="citation_verification_failed"))
    if drafted is None:
        return Outcome("error", "invalid_output", "output_contract",
                       ERROR_ANSWER.format(code="invalid_output"))
    if coverage_errors and coverage is None:
        return Outcome("error", "coverage_failed", "coverage",
                       ERROR_ANSWER.format(code="coverage_failed"))
    if retrieval_errors and retrieval_successes == 0:
        return Outcome("error", "retrieval_failed", "retrieval",
                       ERROR_ANSWER.format(code="retrieval_failed"))
    if online_errors and online_successes == 0:
        return Outcome("error", "online_retrieval_failed", "online_search",
                       ERROR_ANSWER.format(code="online_retrieval_failed"))
    return Outcome("insufficient_evidence", "no_verified_claims", "", REFUSAL_ANSWER)
