"""General medical knowledge answers through configured DashScope/Qwen API.

Unlike the evidence Agent, this module NEVER opens embedded Qdrant, never
attaches invented citations, and clearly marks responses as unverified.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4
from typing import Any

CODE_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = (
    "你是 MedEvidence 的中文一般医学知识科普助手。你的回答基于语言模型训练知识，"
    "本次未核验本地医学文献。请用清晰、准确、通俗的中文回答用户的常见医学知识问题。"
    "不要声称查阅了某篇文献、指南或数据库；不要捏造论文引用、PMID、DOI、年份、疗效数据。"
    "如果问题超出可靠认知或依赖最新指南，请说明需要核对最新专业资料。"
    "不要对具体患者作诊断，不要给出处方、个体药物剂量或擅自调整用药方案。"
    "如果用户描述了明显紧急危险症状，建议及时寻求急诊医疗帮助。"
    "如果问题是通常的医疗科普，应尽量直接回答，而不是机械拒答。"
    "篇幅适中，结构清楚，结尾用一句话提示：具体治疗请咨询医生。"
)

class GeneralQAError(RuntimeError):
    """Safe, concise API/config error; no API keys or raw response bodies."""


def _load_config(code_root: Path):
    """Reuse the Agent's .env.agent resolution but NOT its retriever."""
    root = Path(code_root).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from agent.config import Config
    cfg = Config.from_env(root.parent, code_root=root)
    if not cfg.api_key or cfg.api_key == 'your_key_here':
        raise GeneralQAError('没有读取到有效的百炼 API Key，请检查 .env.agent。')
    if not cfg.api_base.startswith('https://'):
        raise GeneralQAError('模型 API 地址必须使用 HTTPS。')
    return cfg


def answer_general(question: str, *, code_root: Path = CODE_ROOT, timeout: int = 100) -> dict[str, Any]:
    question = question.strip()
    if not question or len(question) > 1800:
        raise GeneralQAError('问题不能为空，且不能超过 1800 字。')
    cfg = _load_config(code_root)
    url = cfg.api_base.rstrip('/') + '/chat/completions'
    payload = {
        'model': cfg.model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': question},
        ],
        'temperature': 0.2,
        'max_tokens': 1600,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + cfg.api_key, 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GeneralQAError('百炼 API 权限验证失败，请检查 API Key。') from None
        if exc.code == 429:
            raise GeneralQAError('百炼 API 请求受限或配额不足，请检查模型额度。') from None
        raise GeneralQAError(f'百炼 API 请求失败（HTTP {exc.code}）。') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GeneralQAError('访问百炼 API 超时或网络连接失败，请检查网络。') from None
    except (ValueError, TypeError):
        raise GeneralQAError('百炼 API 返回的响应不是合法 JSON。') from None
    try:
        choice = data['choices'][0]
        answer = choice['message']['content']
        finish_reason = choice.get('finish_reason', '')
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('empty')
    except (TypeError, KeyError, IndexError, ValueError):
        raise GeneralQAError('百炼 API 未返回有效的文字回答。') from None
    if finish_reason == 'length':
        answer += '\n\n（回答可能因输出长度限制而截断。）'
    return {
        'run_id': 'general-' + uuid4().hex[:12],
        'mode': 'general', 'answer_origin': 'model_knowledge',
        'status': 'model_knowledge', 'model': cfg.model,
        'answer': answer.strip(), 'rounds': 0, 'coverage_ratio': None,
        'evidence': [], 'claims': [],
        'limitations': [
            '本次回答来自语言模型已有知识，未经过本地医学文献检索或引用核验。',
            '知识可能不完整或过时，不能用于个人诊断、开药、调药或急症决策。',
        ],
    }
