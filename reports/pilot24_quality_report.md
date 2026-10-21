# MedEvidence · Pilot 证据卡离线质量报告

生成时间（UTC）：2026-10-14T10:09:59+00:00
候选文件：`E:\Download\Agent\MedEvidence\reports\pilot_annotation_24.csv`  |  数据库：`E:\Download\Agent\MedEvidence\data\review\evidence_review_v1.sqlite3`

## 自动检查结果

- 本次检查候选：**24** 对（问题–论文）；已存卡片：**24**；缺失：**0**；格式无效：**0**。
- 文本引用：**65** 条；字面匹配：**64**；部分匹配：**0**；未找到：**1**。
- 带至少一项截断来源的卡片：**10**；截断来源合计：**17**。
- HIGH 注意：**1**；REVIEW：**12**；无自动异常（TEXT_CHECK_ONLY）：**11**。

**重要：通过字面匹配 ≠ 医学结论得到支持；没有自动异常 ≠ 通过医学复核。AI 建议标签不属于 Gold。**

## 逐条结果（仅用于安排复核）

| 问题 | PMCID | 是否有卡 | 关注级别 | 未找到引用 | 截断段数 | 检查提醒 |
|---|---|---|---|---:|---:|---|
| C001 | PMC13627872 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW |
| C001 | PMC13641454 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| C001 | PMC13607204 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| D001 | PMC13601971 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| D001 | PMC13612674 | PRESENT | REVIEW | 0 | 1 | TRUNCATED_SOURCE_REVIEW |
| D001 | PMC13625882 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| G001 | PMC13627992 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| G001 | PMC13630088 | PRESENT | REVIEW | 0 | 0 | UNCERTAIN_EVIDENCE_JUDGMENT |
| G001 | PMC13630686 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW / UNCERTAIN_EVIDENCE_JUDGMENT |
| M001 | PMC13610795 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| M001 | PMC13637635 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| M001 | PMC13604364 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW |
| P001 | PMC13627872 | PRESENT | REVIEW | 0 | 1 | TRUNCATED_SOURCE_REVIEW |
| P001 | PMC13640178 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW |
| P001 | PMC13627207 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW |
| R001 | PMC13643946 | PRESENT | REVIEW | 0 | 0 | UNKNOWN_STUDY_DESIGN |
| R001 | PMC13629947 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| R001 | PMC13619518 | PRESENT | HIGH | 1 | 0 | QUOTE_NOT_FOUND / UNCERTAIN_EVIDENCE_JUDGMENT |
| S001 | PMC13646093 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| S001 | PMC13640178 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW |
| S001 | PMC13634386 | PRESENT | REVIEW | 0 | 2 | TRUNCATED_SOURCE_REVIEW |
| T001 | PMC13637892 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |
| T001 | PMC13645658 | PRESENT | REVIEW | 0 | 1 | TRUNCATED_SOURCE_REVIEW |
| T001 | PMC13649160 | PRESENT | TEXT_CHECK_ONLY | 0 | 0 | 无自动异常 |

## 解读与后续处理

- `CARD_MISSING`：先确认批量生成是否完成，不要假设 24/24 成功。
- `QUOTE_NOT_FOUND` / `QUOTE_PARTIAL`：在 Streamlit 找到引用和原文；不能直接使用为已验证医学证据。
- `TRUNCATED_SOURCE_REVIEW`：模型只看到了片段，必要时回溯 Parent 或全文核验，尤其是表格、公式。
- `NO_QUOTE_REVIEW_EVIDENCE_GAP`：可能是合理保留不确定性，也可能是证据不足，不能自动判负例。
- `TEXT_CHECK_ONLY`：只代表通过本工具的自动结构与字面检查；不对研究设计和疗效作结论。
- 保持 `pilot v1` 数据版本冻结；人工复核/评测 Gold 另行管理。
