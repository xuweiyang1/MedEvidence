"""A small, deterministic control policy; separated for easy ablation."""
from __future__ import annotations
from dataclasses import dataclass
from .models import Coverage


@dataclass(frozen=True)
class Decision:
    action: str  # answer | search_again | abstain
    reason: str


def decide(coverage: Coverage, round_index: int, max_rounds: int, threshold: float,
           can_search: bool = True) -> Decision:
    if not coverage.covered and round_index >= max_rounds:
        return Decision("abstain", "轮次耗尽且关键子问题均缺少证据")
    if coverage.ratio >= threshold and coverage.covered:
        return Decision("answer", "证据覆盖率达到配置阈值（仅为启发式代理分数）")
    if round_index < max_rounds and can_search:
        return Decision("search_again", "关键子问题仍缺少证据")
    if coverage.covered:
        return Decision("answer", "已达到检索限制，将尝试部分回答并说明缺失证据")
    return Decision("abstain", "找不到可引用的支持性证据")
