"""Plain data objects and serialization; no heavyweight imports."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Evidence:
    id: str
    text: str
    title: str = ""
    pmcid: str = ""
    doi: str = ""
    year: str = ""
    section: str = ""
    score: float = 0.0
    source: str = ""

    def public(self, include_text: bool = False) -> dict[str, Any]:
        x = asdict(self)
        if not include_text:
            x.pop("text", None)
        return x


@dataclass
class Plan:
    aspects: list[str]
    queries: list[str]
    high_risk: bool = False


@dataclass
class Coverage:
    covered: dict[str, list[str]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    ratio: float = 0.0
    explanation: str = ""


@dataclass
class AgentResult:
    run_id: str
    status: str
    answer: str
    claims: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    limitations: list[str]
    verification: dict[str, Any]
    trace_path: str
    rounds: int
    coverage_ratio: float
    # Why the run ended the way it did; empty for answered/partial.
    reason_code: str = ""
    failure_stage: str = ""
    # Token/request accounting: {"sdk": {...}, "judge": {...}, "model": ...}
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
