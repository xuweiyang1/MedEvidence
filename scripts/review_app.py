#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Streamlit front end. Start via scripts/run_review_app.py from PyCharm."""
from __future__ import annotations
import json
import re
from pathlib import Path

import streamlit as st
from evidence_review_core import (
    DB_FILE, REPORTS, LABELS, DESIGNS, DIRECTNESS, MATCHES, PAPER_SUPPORT,
    candidate_file, connect_db, export_reviews, key_for, read_candidates, utc_now,
)

st.set_page_config(page_title="MedEvidence 医学证据复核", layout="wide", page_icon="📚")
st.title("MedEvidence · 证据卡人工复核（Pilot）")
st.caption("Qwen 证据卡是 AI 草稿；QUOTE_VERIFIED 只代表文字确实出现，不代表结论得到支持。未经医学专业人员复核，不应当作 Gold。")

try:
    candidate_path = candidate_file()
    candidates = read_candidates(candidate_path)
    db = connect_db(DB_FILE)
    records = db.execute("SELECT * FROM cards ORDER BY question_id, document_id").fetchall()
except Exception as e:
    st.error(f"加载数据失败：{e}。先在 PyCharm 运行 generate_evidence_cards.py。")
    st.stop()

if not records:
    st.warning("当前没有证据卡。先运行 generate_evidence_cards.py 生成3条试点卡片。")
    st.stop()

st.sidebar.header("审核设置")
reviewer_id = st.sidebar.text_input("你的审核者编号", value="R1").strip()
if not reviewer_id or len(reviewer_id) > 48 or not re.fullmatch(r"[\w-]+", reviewer_id):
    st.sidebar.error("审核编号必须是1~48位字母、数字、下划线或连接号。")
    st.stop()

keys = [r["key"] for r in records]
if "idx" not in st.session_state:
    st.session_state.idx = 0
st.session_state.idx = min(st.session_state.idx, len(keys)-1)
index = st.sidebar.selectbox("选择问题–论文对", options=list(range(len(keys))),
                             format_func=lambda x: f"{x+1}/{len(keys)} · {keys[x]}", index=st.session_state.idx)
st.session_state.idx = index
r = records[index]
context = json.loads(r["context_json"])
card = json.loads(r["card_json"])
audit = json.loads(r["audit_json"])
existing = db.execute("SELECT * FROM reviews WHERE key=? AND reviewer_id=?", (r["key"], reviewer_id)).fetchone()
previous = json.loads(existing["review_json"]) if existing else {}

n_reviewed = db.execute("SELECT COUNT(DISTINCT key) FROM reviews").fetchone()[0]
st.sidebar.metric("已保存过复核的候选", f"{n_reviewed}/{len(keys)}")
st.sidebar.caption("保存记录会写入本地SQLite；导出CSV仅包含已保存的人工复核，不会混入AI预标注。")

left, middle, right = st.columns([1.1, 1.8, 1.15], gap="large")
with left:
    st.subheader("问题与论文")
    st.markdown(f"**问题 {context['question_id']}**")
    st.write(context["query"])
    st.markdown(f"**论文 {context['document_id']}**")
    st.write(context["title"])
    did = context["document_id"]
    if re.fullmatch(r"PMC\d+", did):
        st.markdown(f"[在 PubMed Central 打开原始论文](https://pmc.ncbi.nlm.nih.gov/articles/{did}/)")
    for name, value in (context.get("metadata") or {}).items():
        if value:
            st.text(f"{name.upper()}: {value}")
    if context.get("missing_support_ids"):
        st.warning("部分命中 Chunk ID 未从检索语料匹配：" + ", ".join(context["missing_support_ids"][:4]))
    if not context.get("has_corpus_text"):
        st.warning("仅有候选预览或原文上下文不足。不能直接判断论文结论。")
    st.info("来源是截取的部分论文内容，可能遗漏Methods/Results；必要时请打开原论文。")

with middle:
    st.subheader("Qwen 证据卡（供核对）")
    for label, k in (("研究设计", "study_design"), ("人群", "population"),
                     ("干预/暴露", "intervention_or_exposure"), ("对照", "comparator"),
                     ("结局", "outcome"), ("主要发现", "main_finding"),
                     ("证据直接性建议", "evidence_directness"),
                     ("论文回答支持建议", "supports_answer_from_paper"),
                     ("相关性建议（非Gold）", "suggested_relevance_label")):
        st.markdown(f"**{label}**：{card.get(k, 'UNCERTAIN')}")
    if card.get("uncertainties"):
        st.markdown("**尚不确定**")
        for v in card["uncertainties"]:
            st.write("•", v)
    st.divider()
    st.subheader("原文引用字面核对")
    if not audit["quotes"]:
        st.warning("没有原文引用。建议标记 UNCERTAIN，并回到全文核对。")
    for item in audit["quotes"]:
        state = item["state"]
        if state == "QUOTE_VERIFIED":
            st.success("QUOTE_VERIFIED · 逐字核验通过（不代表结论被证实）")
        elif state == "QUOTE_PARTIAL":
            st.warning("QUOTE_PARTIAL · 仅部分字面匹配，不能直接引用")
        else:
            st.error("QUOTE_NOT_FOUND · 指定原文未找到，请勿当作可信引用")
        st.code(item["quote"], language=None)
        st.caption(f"来源 {item['source_id']} | Chunk/Parent: {item['raw_id']} | 真实节名: {item['section_actual']}")
    with st.expander("查看送给 Qwen 的全部原文（按来源 ID）", expanded=False):
        for s in context["sources"]:
            st.markdown(f"**{s['source_id']}** · {s['origin']} · {s['section']} · `{s['raw_id']}`")
            st.text(s["text"])
            if s["truncated"]:
                st.caption("⚠ 此段已截断；请打开论文查看全文。")

with right:
    st.subheader("人工复核")
    st.caption("不懂、看不准就选 UNCERTAIN。请根据可见证据核对，不能照抄 AI 推荐。")

    def select_field(label: str, options: tuple[str, ...], field: str):
        initial = previous.get(field)
        default = options.index(initial) if initial in options else options.index("UNCERTAIN") if "UNCERTAIN" in options else options.index("UNKNOWN") if "UNKNOWN" in options else 0
        return st.selectbox(label, options, index=default, key=f"{r['key']}|{reviewer_id}|{field}")

    label = select_field("相关性（2/1/0/UNCERTAIN）", LABELS, "relevance_label")
    design = select_field("研究设计", DESIGNS, "study_design")
    direct = select_field("证据直接性", DIRECTNESS, "evidence_directness")
    population = select_field("人群匹配", MATCHES, "population_match")
    outcome = select_field("结局匹配", MATCHES, "outcome_match")
    supports = select_field("单篇论文能否支持答案", PAPER_SUPPORT, "supports_answer_from_paper")
    notes = st.text_area("复核依据 / 不确定点", value=previous.get("review_notes", ""), height=110,
                         key=f"{r['key']}|{reviewer_id}|notes")
    verified_count = audit["counts"]["QUOTE_VERIFIED"]
    if label == "2" and verified_count == 0:
        st.error("没有完全核验通过的原文引用，禁止标为2。请先补充论文上下文或选UNCERTAIN。")
    if any(q["state"] != "QUOTE_VERIFIED" for q in audit["quotes"]):
        st.warning("存在不完整/找不到的引用，请额外检查。")
    checked = st.checkbox("我已查看引用与对应原文，理解这不是 Gold 标注", value=False,
                          key=f"{r['key']}|{reviewer_id}|checked")
    status = "NEEDS_FULLTEXT" if label == "UNCERTAIN" or verified_count == 0 else "REVIEWED_PROVISIONAL"
    disabled = not checked or (label == "2" and verified_count == 0)
    if st.button("保存并下一条", type="primary", disabled=disabled, use_container_width=True):
        review = {
            "relevance_label": label, "evidence_directness": direct,
            "study_design": design, "population_match": population,
            "outcome_match": outcome, "supports_answer_from_paper": supports,
            "review_notes": notes, "reviewer_id": reviewer_id,
            "review_status": status,
        }
        db.execute("""INSERT INTO reviews(key,reviewer_id,review_json,saved_utc)
                      VALUES(?,?,?,?) ON CONFLICT(key,reviewer_id)
                      DO UPDATE SET review_json=excluded.review_json,saved_utc=excluded.saved_utc""",
                   (r["key"], reviewer_id, json.dumps(review, ensure_ascii=False), utc_now()))
        db.commit()
        count = export_reviews(db, candidates, REPORTS / "pilot_annotation_24_human_review.csv")
        st.session_state.idx = min(index + 1, len(keys)-1)
        st.toast(f"已保存 {r['key']}，人工记录 {count} 行")
        st.rerun()
    st.caption(f"当前状态: {previous.get('review_status', '尚未保存')}；当前模型: {r['model_name']}")

# Re-open all review records via DB: the CSV is merely an exported copy.
db.close()
