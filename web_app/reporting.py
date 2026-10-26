"""Export plain-text Markdown with accurate evidence provenance."""
from __future__ import annotations
from typing import Any
from bridge import render_markdown_report


def export_question_report(question: str, result: dict[str, Any]) -> str:
    if result.get('answer_origin') != 'model_knowledge':
        return render_markdown_report(question, result)
    lines = [
        '# MedEvidence 通用医学知识回答', '',
        '> **来源：大语言模型生成；未经本地医学文献检索或引用核验。**', '',
        '## 问题', question.strip(), '', '## 回答',
        str(result.get('answer') or ''), '',
        '## 证据与限制',
        '- 本次内容没有可核验的本地论文引用；不得把它当成循证临床结论。',
    ]
    for x in result.get('limitations') or []:
        lines.append('- ' + str(x))
    lines.extend(['', '---', '仅供医学科研与科普参考，不用于个人诊疗或处方。'])
    return '\n'.join(lines)
