#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V1b 独立的隔离试验入库页；原来的 V1a.3 保持不动。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data/staging/medical_imports"
BASE = ROOT / "data/processed/knowledge/v3_1_all"

st.set_page_config(page_title="MedEvidence V1b · 隔离入库", layout="wide")
st.title("MedEvidence · 医学文献入库 V1b")
st.caption("只准备隔离试验语料和新索引，不发布临床证据，不改变已有 V3.1 / BM25 / Qdrant")
st.warning("PDF 目前只有文字与页码定位，未验证双栏、公式、表格、图片；XML 的部分嵌套结构可能被拒绝。未经独立质量与许可审核，不应让 Agent 作为正式临床证据使用。")

def staged():
    result = []
    for m in sorted(STAGING.glob("*/manifest.json")):
        try:
            row = json.loads(m.read_text(encoding="utf-8"))
            if row.get("publication_status") == "NOT_INDEXED":
                result.append((m, row))
        except (OSError, ValueError):
            pass
    return result

items = staged()
col1, col2, col3 = st.columns(3)
col1.metric("当前基础论文", "921（预期）")
col2.metric("基础 Chunk", "42,526（预期）")
col3.metric("待入库文件", len(items))

if not BASE.exists():
    st.error("找不到原始语料：" + str(BASE))
    st.stop()
if not items:
    st.info("请先在 V1a.3（8502）上传并暂存一篇 XML/PDF，然后返回此页。")
    st.stop()

names = [f"{r.get('original_filename')} ｜ {m.parent.name[:12]}" for m, r in items]
chosen = st.multiselect("选择要放进同一试验版本的待入库文献", names, default=names[:1])
by_label = dict(zip(names, items))
selected = [by_label[n] for n in chosen]
for m, row in selected:
    ins = row.get("inspection") or {}
    with st.expander(row.get("original_filename") or "待入库文件", expanded=len(selected) == 1):
        st.write("**标题**：", ins.get("title", ""))
        st.write("**元数据已核对**：", "是" if ins.get("metadata_confirmed") else "否")
        st.write("**主 DOI / PMID / PMCID**：", ins.get("doi", ""), ins.get("pmid", ""), ins.get("pmcid", ""))
        st.write("**arXiv / 版本类型**：", (ins.get("version_identity") or {}).get("arxiv_id", ""), (ins.get("version_identity") or {}).get("uploaded_version_type", "UNKNOWN"))
        st.write("**许可状态**：", row.get("source_license", "NOT_REVIEWED"))
        st.write("**重复警示**：", ins.get("duplicates") or "暂无记录")

version = st.text_input("新版本名称（不能与已存在版本相同）", value="v3_2_trial")
valid_version = bool(re.fullmatch(r"[a-zA-Z0-9_-]{3,64}", version)) and version != "v3_1_all"
new_dir = ROOT / "data/processed/knowledge" / version if valid_version else None
st.caption("这个版本不会改变原有 921 篇文献。成功后会生成完整语料快照 + 1 篇或更多新论文。")
ack1 = st.checkbox("我明白这是未验证的隔离试验库，不是正式医学知识库发布")
ack2 = st.checkbox("我已核对待入库文件的标题与重复状态；未通过的文件需返回 8502 处理")
can_prepare = bool(selected) and valid_version and not new_dir.exists() and ack1 and ack2
bad = [r.get("original_filename", "") for _, r in selected if not (r.get("inspection") or {}).get("metadata_confirmed")]
if bad:
    st.error("以下文件尚未确认标题，无法从本页面准备试验版：" + "、".join(bad))
    can_prepare = False
if any((r.get("inspection") or {}).get("duplicates") for _, r in selected):
    st.error("部分文件有重复警示；请先在旧管理页核对，避免重复收录。")
    can_prepare = False

def run_python(script, params, timeout=300):
    cmd = [sys.executable, str(ROOT / "scripts" / script), "--root", str(ROOT), *params]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)

if st.button("构建新的隔离试验语料（不修改旧版）", type="primary", disabled=not can_prepare):
    args = ["--version", version, "--allow-unreviewed-trial"]
    for m, _ in selected:
        args += ["--manifest", str(m)]
    try:
        with st.spinner("正在解析新论文并验证 Chunk / Parent / 关系完整性……"):
            done = run_python("prepare_corpus_v1b.py", args)
        st.code((done.stdout + "\n" + done.stderr).strip(), language="text")
        if done.returncode == 0:
            st.success("新试验语料已生成。继续建立单独的 BM25 索引。")
        else:
            st.error("准备失败，旧知识库未改动。请根据日志检查 PDF/XML 或重复状态。")
    except subprocess.TimeoutExpired:
        st.error("准备任务超过5分钟；请在 PyCharm Terminal 单独运行，先检查是否已有对应版本目录。")

if valid_version and new_dir.exists():
    report = new_dir / "ingestion_report.json"
    st.divider()
    st.subheader("已创建的试验版本：" + version)
    if report.is_file():
        r = json.loads(report.read_text(encoding="utf-8"))
        st.json({"status": r.get("status"), "counts": r.get("counts"), "new_documents": r.get("new_document_count")})
    db = ROOT / "data/processed/index" / version / "bm25_v1b.sqlite3"
    if not db.exists():
        if st.button("为该试验版本建立独立 BM25 索引"):
            with st.spinner("正在建立 SQLite FTS5 索引……"):
                done = run_python("build_bm25_v1b.py", ["--version", version], timeout=300)
            st.code((done.stdout + "\n" + done.stderr).strip(), language="text")
            if done.returncode == 0:
                st.success("新 BM25 索引已建立；刷新页面可查看。")
            else:
                st.error("新 BM25 索引构建失败；原 BM25 文件保持不变。")
    else:
        st.success("隔离 BM25 索引已存在：" + str(db))
    st.subheader("BGE-M3 / Qdrant 独立建库（在 PyCharm Terminal 执行）")
    st.caption("GPU 向量化可能耗时较长，故不在 Streamlit 请求中运行。不要使用 --reset 或旧版 Qdrant 路径。")
    n = (json.loads(report.read_text(encoding="utf-8")).get("counts") or {}).get("retrieval_chunks", 0) if report.is_file() else 0
    cmd = (f"python scripts\\medevidence_dense_v1b.py build --corpus data/processed/knowledge/{version}/retrieval_chunks.jsonl "
           f"--docs data/processed/knowledge/{version}/documents.jsonl "
           f"--index data/processed/index/{version}/qdrant_bge_m3 "
           f"--collection bge_m3_dense_{version} --expected-count {n}")
    st.code(cmd, language="powershell")
    st.caption("成功后应核对 Qdrant point_count 与 Chunk 数一致，并开展 BM25 / Dense 检索测试。本试验版本不会自动切换到 Agent。")
