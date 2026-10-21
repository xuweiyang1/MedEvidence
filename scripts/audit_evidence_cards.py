#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MedEvidence Pilot: read-only, offline evidence-card quality / coverage audit.

Run from MedEvidence root:
    python scripts/audit_evidence_cards.py

Does not call Qwen, modify SQLite, change human reviews, or regenerate cards.
The quote check establishes *literal occurrence*, NOT scientific validity.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evidence_review_core import ROOT, DB_FILE, audit_card, read_candidates

FIELDS = [
    "question_id", "document_id", "card_status", "attention_level",
    "study_design", "evidence_directness", "supports_answer_from_paper",
    "suggested_relevance_label", "source_count", "truncated_source_count",
    "missing_support_chunk_count", "quote_total", "quote_verified",
    "quote_partial", "quote_not_found", "uncertainty_count", "issues",
]

REQUIRED = (
    "study_design", "population", "intervention_or_exposure", "comparator",
    "outcome", "main_finding", "evidence_directness",
    "supports_answer_from_paper", "suggested_relevance_label",
)


def read_existing_cards(db: Path) -> dict[str, sqlite3.Row]:
    if not db.is_file():
        raise FileNotFoundError(f"不存在证据卡数据库：{db}")
    # Read-only connection: avoids creating databases or changing review state.
    url = db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(url, uri=True, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        return {r["key"]: dict(r) for r in conn.execute(
            "SELECT key, card_json, context_json, audit_json, model_name FROM cards"
        )}


def score_one(qid: str, did: str, saved: dict | None) -> dict:
    row = {k: "" for k in FIELDS}
    row.update(question_id=qid, document_id=did, card_status="MISSING", attention_level="HIGH")
    issues = []
    if saved is None:
        row["issues"] = "CARD_MISSING"
        return row
    row["card_status"] = "PRESENT"
    try:
        card = json.loads(saved["card_json"])
        context = json.loads(saved["context_json"])
        stored_audit = json.loads(saved["audit_json"])
        if not isinstance(card, dict) or not isinstance(context, dict) or not isinstance(stored_audit, dict):
            raise ValueError("Expected JSON objects")
    except (ValueError, TypeError, KeyError) as e:
        row.update(card_status="INVALID", attention_level="HIGH", issues=f"INVALID_SAVED_JSON:{type(e).__name__}")
        return row

    for col in ("study_design", "evidence_directness", "supports_answer_from_paper", "suggested_relevance_label"):
        row[col] = str(card.get(col) or "")

    absent = [field for field in REQUIRED if not str(card.get(field) or "").strip()]
    if absent:
        issues.append("MISSING_FIELDS:" + ",".join(absent))
    sources = context.get("sources", [])
    if not isinstance(sources, list):
        sources = []
        issues.append("INVALID_SOURCES")
    row["source_count"] = len(sources)
    row["truncated_source_count"] = sum(bool(s.get("truncated")) for s in sources if isinstance(s, dict))
    if not sources:
        issues.append("NO_SOURCE_TEXT")
    if any(isinstance(s, dict) and s.get("origin") == "CSV_PREVIEW_ONLY" for s in sources):
        issues.append("PREVIEW_ONLY_NOT_FULLTEXT")
    if row["truncated_source_count"]:
        issues.append("TRUNCATED_SOURCE_REVIEW")

    missing = context.get("missing_support_ids", [])
    if not isinstance(missing, list):
        missing = []
        issues.append("INVALID_SUPPORT_IDS")
    row["missing_support_chunk_count"] = len(missing)
    if missing:
        issues.append("MISSING_SUPPORT_CHUNKS")

    quotes = card.get("evidence_quotes", [])
    if not isinstance(quotes, list):
        quotes = []
        issues.append("INVALID_QUOTES")
    row["quote_total"] = len(quotes)
    if not quotes:
        issues.append("NO_QUOTE_REVIEW_EVIDENCE_GAP")
    if len(quotes) > 3:
        issues.append("MORE_THAN_THREE_QUOTES")

    try:
        # Recompute with the same literal-text verifier over the saved source snapshot.
        check = audit_card(card, context)
        counts = check["counts"]
        row["quote_verified"] = counts.get("QUOTE_VERIFIED", 0)
        row["quote_partial"] = counts.get("QUOTE_PARTIAL", 0)
        row["quote_not_found"] = counts.get("QUOTE_NOT_FOUND", 0)
        if row["quote_not_found"]:
            issues.append("QUOTE_NOT_FOUND")
        if row["quote_partial"]:
            issues.append("QUOTE_PARTIAL")
        # The saved audit should match this recomputation. Catch possible state drift.
        if stored_audit.get("counts") != counts:
            issues.append("STORED_QUOTE_AUDIT_MISMATCH")
    except (KeyError, TypeError, ValueError, AttributeError):
        issues.append("UNABLE_TO_VERIFY_QUOTES")

    unc = card.get("uncertainties", [])
    row["uncertainty_count"] = len(unc) if isinstance(unc, list) else 0
    if not unc:
        issues.append("UNCERTAINTIES_NOT_RECORDED")
    if row["study_design"] == "UNKNOWN":
        issues.append("UNKNOWN_STUDY_DESIGN")
    if row["evidence_directness"] == "UNCERTAIN" or row["supports_answer_from_paper"] == "UNCERTAIN":
        issues.append("UNCERTAIN_EVIDENCE_JUDGMENT")

    severe = ("MISSING_FIELDS", "NO_SOURCE_TEXT", "PREVIEW_ONLY", "QUOTE_NOT_FOUND", "INVALID_", "UNABLE_TO_VERIFY", "STORED_QUOTE_AUDIT_MISMATCH")
    row["attention_level"] = "HIGH" if any(x.startswith(severe) for x in issues) else ("REVIEW" if issues else "TEXT_CHECK_ONLY")
    row["issues"] = " | ".join(issues)
    return row


def write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线审核证据卡完整性/原文引用，非医学真实性判定")
    parser.add_argument("--input", type=Path, default=ROOT / "reports/pilot_annotation_24.csv")
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--out", type=Path, default=ROOT / "reports/pilot24_quality_report")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须大于 0")

    candidates = read_candidates(args.input)
    selected = list(candidates)[:args.limit]
    saved_cards = read_existing_cards(args.db)
    rows = [score_one(qid, did, saved_cards.get(f"{qid}|{did}")) for qid, did in selected]
    counts = Counter(row["card_status"] for row in rows)
    attention = Counter(row["attention_level"] for row in rows)
    quote_count = sum(int(row["quote_total"] or 0) for row in rows)
    verified = sum(int(row["quote_verified"] or 0) for row in rows)
    partial = sum(int(row["quote_partial"] or 0) for row in rows)
    not_found = sum(int(row["quote_not_found"] or 0) for row in rows)
    trunc = sum(int(row["truncated_source_count"] or 0) for row in rows)

    from io import StringIO
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    csv_path = args.out.with_suffix(".csv")
    md_path = args.out.with_suffix(".md")
    write_atomic(csv_path, buf.getvalue())

    summary = [
        "# MedEvidence · Pilot 证据卡离线质量报告",
        "",
        f"生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"候选文件：`{args.input}`  |  数据库：`{args.db}`",
        "",
        "## 自动检查结果",
        "",
        f"- 本次检查候选：**{len(selected)}** 对（问题–论文）；已存卡片：**{counts['PRESENT']}**；缺失：**{counts['MISSING']}**；格式无效：**{counts['INVALID']}**。",
        f"- 文本引用：**{quote_count}** 条；字面匹配：**{verified}**；部分匹配：**{partial}**；未找到：**{not_found}**。",
        f"- 带至少一项截断来源的卡片：**{sum(bool(row['truncated_source_count']) for row in rows)}**；截断来源合计：**{trunc}**。",
        f"- HIGH 注意：**{attention['HIGH']}**；REVIEW：**{attention['REVIEW']}**；无自动异常（TEXT_CHECK_ONLY）：**{attention['TEXT_CHECK_ONLY']}**。",
        "",
        "**重要：通过字面匹配 ≠ 医学结论得到支持；没有自动异常 ≠ 通过医学复核。AI 建议标签不属于 Gold。**",
        "",
        "## 逐条结果（仅用于安排复核）",
        "",
        "| 问题 | PMCID | 是否有卡 | 关注级别 | 未找到引用 | 截断段数 | 检查提醒 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        issues = str(row["issues"]).replace("|", "/").replace("\n", " ")
        if len(issues) > 92:
            issues = issues[:89] + "..."
        summary.append(
            f"| {row['question_id']} | {row['document_id']} | {row['card_status']} | "
            f"{row['attention_level']} | {row['quote_not_found'] or 0} | "
            f"{row['truncated_source_count'] or 0} | {issues or '无自动异常'} |"
        )
    summary.extend([
        "", "## 解读与后续处理", "",
        "- `CARD_MISSING`：先确认批量生成是否完成，不要假设 24/24 成功。",
        "- `QUOTE_NOT_FOUND` / `QUOTE_PARTIAL`：在 Streamlit 找到引用和原文；不能直接使用为已验证医学证据。",
        "- `TRUNCATED_SOURCE_REVIEW`：模型只看到了片段，必要时回溯 Parent 或全文核验，尤其是表格、公式。",
        "- `NO_QUOTE_REVIEW_EVIDENCE_GAP`：可能是合理保留不确定性，也可能是证据不足，不能自动判负例。",
        "- `TEXT_CHECK_ONLY`：只代表通过本工具的自动结构与字面检查；不对研究设计和疗效作结论。",
        "- 保持 `pilot v1` 数据版本冻结；人工复核/评测 Gold 另行管理。",
    ])
    write_atomic(md_path, "\n".join(summary) + "\n")
    print(f"检查 {len(selected)} 条；已存卡片 {counts['PRESENT']}，缺失 {counts['MISSING']}，无效 {counts['INVALID']}")
    print(f"引用：字面匹配 {verified}，部分匹配 {partial}，未找到 {not_found}；高优先级注意 {attention['HIGH']}")
    for r in rows:
        if r["attention_level"] == "HIGH":
            print(f"  需要优先检查：{r['question_id']}|{r['document_id']} -> {r['issues']}")
    print(f"详细 CSV：{csv_path}\nMarkdown 报告：{md_path}")
    print("说明：本工具不进行医学正确性判定，也不调用 API。")


if __name__ == "__main__":
    main()
