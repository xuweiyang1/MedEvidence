"""Clear separation of general medical knowledge vs strict evidence workflow."""
from __future__ import annotations
from typing import Any, Callable

from bridge import AgentFrontendError, run_question
from general_qa import GeneralQAError, answer_general

VALID_MODES = {'general', 'strict'}


def run_web_question(
    question: str, *, mode: str = 'general', prefer_evidence: bool = False,
    agent_runner: Callable[[str], dict[str, Any]] = run_question,
    general_runner: Callable[[str], dict[str, Any]] = answer_general,
) -> dict[str, Any]:
    """Never present an unverified model answer as if its citations were checked.

    In strict mode, *only* the original evidence Agent is run. In general mode,
    the quick path only calls Qwen. Optional evidence-first is explicitly opt-in.
    """
    if mode not in VALID_MODES:
        raise ValueError('不支持的问答模式。')
    if mode == 'strict':
        result = dict(agent_runner(question))
        result['mode'] = 'strict'
        result['answer_origin'] = 'evidence_agent'
        return result
    if not prefer_evidence:
        result = dict(general_runner(question))
        result['mode'] = 'general'
        result['evidence_first'] = False
        return result

    agent_result = None
    error_occurred = False
    try:
        agent_result = dict(agent_runner(question))
    except AgentFrontendError:
        # This fallback is intentionally visible, never a hidden 'verified' answer.
        error_occurred = True
    if agent_result and agent_result.get('status') == 'answered' \
            and agent_result.get('evidence') and agent_result.get('claims'):
        agent_result['mode'] = 'general'
        agent_result['answer_origin'] = 'evidence_agent'
        agent_result['evidence_first'] = True
        return agent_result

    result = dict(general_runner(question))
    result['mode'] = 'general'
    result['evidence_first'] = True
    result['local_attempt'] = {
        'outcome': 'error' if error_occurred else str((agent_result or {}).get('status', 'unavailable')),
        'source_count': len((agent_result or {}).get('evidence') or []),
    }
    if error_occurred:
        result['limitations'].append('本地检索运行失败；已切换成未核验的通用模型回答。')
    else:
        result['limitations'].append('本地证据未满足完整回答条件；下列模型回答没有沿用本地引文。')
    return result
