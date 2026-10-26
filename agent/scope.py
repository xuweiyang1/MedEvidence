"""Detect overly broad questions before they burn retrieval rounds.

The local corpus is research-paper oriented (very few guidelines), so generic
questions such as "common treatments for heart disease" retrieve tangential
trial fragments. A deterministic hint is cheaper and more honest than three
rounds of low-precision retrieval.
"""
from __future__ import annotations

from dataclasses import dataclass

# Conditions specific enough that local retrieval has a chance.
SPECIFIC_TERMS = (
    "心衰", "心力衰竭", "heart failure",
    "冠心病", "冠状动脉", "coronary artery", "coronary heart disease",
    "高血压", "hypertension",
    "房颤", "心房颤动", "atrial fibrillation",
    "心肌病", "cardiomyopathy",
    "心肌梗死", "心梗", "myocardial infarction",
    "瓣膜", "valve disease",
    "心律失常", "arrhythmia",
    "肺动脉高压", "pulmonary hypertension",
    "糖尿病", "diabetes",
    "慢阻肺", "copd", "chronic obstructive",
    "哮喘", "asthma",
    "慢性肾病", "chronic kidney", "ckd",
    "肺炎", "pneumonia",
    "脓毒症", "sepsis",
    "静脉血栓", "thromboembolism",
    "卒中", "stroke",
    "甲状腺", "thyroid",
    "贫血", "anemia",
    "痛风", "gout",
    "类风湿", "rheumatoid",
    "先心病", "先天性心脏病", "congenital heart",
    "血脂异常", "高血脂", "dyslipidemia",
)

GENERIC_TERMS = (
    "常见", "治疗方式", "治疗方法", "怎么治", "如何治疗", "有哪些", "一般治疗",
    "常用药", "包括哪些", "治疗手段", "处理方式",
    "common treatment", "treatment options", "how to treat", "management options",
    "what are the treatments", "treatment modalities",
)

SUGGESTIONS = (
    "心力衰竭的一线药物治疗有哪些？",
    "冠心病的抗血小板与降脂治疗证据？",
    "房颤患者的抗凝治疗如何选择？",
    "慢性肾病患者的血压控制目标？",
    "2 型糖尿病的降糖药物心血管结局证据？",
)


@dataclass(frozen=True)
class BroadScope:
    message: str
    suggestions: tuple[str, ...] = SUGGESTIONS

    def as_text(self) -> str:
        return self.message + "可尝试更具体的问题：" + "；".join(self.suggestions)


def detect_broad_question(question: str) -> BroadScope | None:
    """Return a scope hint when the question is generic and names no condition."""
    text = (question or "").casefold()
    if not text.strip():
        return None
    generic = any(term in text for term in GENERIC_TERMS)
    specific = any(term in text for term in SPECIFIC_TERMS)
    if not generic or specific:
        return None
    return BroadScope(
        message=("这是一个较宽泛的问题。本地库以具体研究为主、指南很少，"
                 "宽泛问题容易检索到不相关片段。建议缩小到具体疾病，或使用在线指南检索。"),
    )
