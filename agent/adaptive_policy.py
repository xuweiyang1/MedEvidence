"""MedEvidence V0.3.0: auditable evidence sufficiency control (research prototype).

Not clinical validity / not an independent verifier. Structural citation grounding,
coverage heuristics, novelty and conflicting-source flags only. Semantic support
verification remains the CitationVerifier's *separate* responsibility.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Sequence
from .models import Coverage, Evidence

Action = Literal["search_again", "answer", "partial", "abstain"]


@dataclass(frozen=True)
class PolicyConfig:
    coverage_goal: float = 1.0
    score_goal: float = 0.70  # pilot starting point; tune only on Dev set
    min_body_chars: int = 40
    stop_on_zero_novelty_after: int = 2


@dataclass(frozen=True)
class PolicyDecision:
    action: Action
    reason_code: str
    sufficiency_proxy: float
    source_utilization: float
    aspect_coverage: float
    anchor_quality_proxy: float
    conflict_penalty: float
    evidence_count: int
    independent_source_count: int
    new_chunks_last_search: int
    remaining_searches: int
    unresolved_aspects: tuple[str, ...]
    # not kept in logs to avoid leaking user question: only the count is logged

    def public(self, include_gap_text: bool = False) -> dict:
        data = asdict(self)
        if not include_gap_text:
            data["unresolved_aspects"] = len(self.unresolved_aspects)
        return data


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, x))


def decide_evidence_action(*, aspects: Sequence[str], coverage: Coverage | None,
                           evidence: Sequence[Evidence], searches_used: int,
                           max_searches: int, new_chunks_last_search: int = 0,
                           no_novelty_streak: int = 0,
                           settings: PolicyConfig = PolicyConfig()) -> PolicyDecision:
    """Decide from CURRENT evidence only, never the hidden future search rounds.

    R: fraction of retrieved source chunks used by a cited coverage decision.
    C: fraction of question aspects with a whitelisted cited anchor.
    V: fraction of aspects anchored to a substantial, non-empty text snippet.
    K: conflict mentions per aspect (bounded).
    Score is an unvalidated proxy; hard gates take precedence over the score.
    """
    aspects = list(dict.fromkeys(x.strip() for x in aspects if x.strip()))
    edict = {e.id: e for e in evidence if e.id}
    covered: dict[str, list[str]] = {}
    conflict_count = len(coverage.conflicts) if coverage else 0
    for a in aspects:
        refs = (coverage.covered.get(a, []) if coverage else [])
        valid = list(dict.fromkeys(r for r in refs if r in edict))
        if valid:
            covered[a] = valid
    used_ids = {r for refs in covered.values() for r in refs}
    unresolved = [a for a in aspects if a not in covered]
    # Extra gap statements returned by the evaluator are not silently ignored.
    if coverage and coverage.gaps:
        for gap in coverage.gaps:
            if gap not in unresolved:
                unresolved.append(gap)
    c = len(covered) / len(aspects) if aspects else 0.0
    v = (sum(any(len(edict[r].text.strip()) >= settings.min_body_chars for r in refs)
             for refs in covered.values()) / len(aspects)) if aspects else 0.0
    r = len(used_ids) / len(edict) if edict else 0.0
    k = min(1.0, conflict_count / max(len(aspects), 1))
    score = _clamp(.20 * r + .45 * c + .35 * v - .20 * k)
    left = max(0, max_searches - searches_used)
    source_ids = {e.pmcid or e.id.split(":", 1)[0] for e in edict.values()}
    # no evidence, no aspects, conflicts, missing anchors, or exhausted budget:
    # fail closed instead of pretending an exact clinical sufficiency threshold.
    if not aspects:
        action, reason = "abstain", "no_evidence_aspects"
    elif (not edict or not covered) and not left:
        action, reason = "abstain", "budget_exhausted_without_usable_evidence"
    elif c >= settings.coverage_goal and v >= settings.coverage_goal and not unresolved and not conflict_count and score >= settings.score_goal:
        action, reason = "answer", "proxy_coverage_met"
    elif left and searches_used >= settings.stop_on_zero_novelty_after and no_novelty_streak >= settings.stop_on_zero_novelty_after:
        action, reason = ("partial" if covered else "abstain"), "retrieval_saturated"
    elif left:
        action, reason = "search_again", "missing_or_conflicting_evidence"
    else:
        action, reason = ("partial" if covered else "abstain"), "search_budget_exhausted"
    return PolicyDecision(
        action=action, reason_code=reason, sufficiency_proxy=round(score, 4),
        source_utilization=round(r, 4), aspect_coverage=round(c, 4),
        anchor_quality_proxy=round(v, 4), conflict_penalty=round(k, 4),
        evidence_count=len(edict), independent_source_count=len(source_ids),
        new_chunks_last_search=max(0, new_chunks_last_search),
        remaining_searches=left, unresolved_aspects=tuple(unresolved))
