#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only corpus overview + local XML/PDF preview and isolated staging."""
from __future__ import annotations

import hashlib
import os
import streamlit as st
from metadata_lookup import lookup_by_title, title_similarity
from arxiv_lookup import lookup_arxiv_by_id
from knowledge_import_core import (
    PROJECT_ROOT, DOCUMENTS_JSONL, CHUNKS_JSONL, STAGING_ROOT,
    analyze_upload, existing_corpus_info, list_staged, stage_upload,
    check_duplicates, normalize_doi, normalize_pmcid, normalize_pmid,
    validate_version_identifiers,
)

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass


def adopt_arxiv_record(digest: str, record: dict) -> None:
    # Official arXiv metadata describes the PREPRINT; it does not prove the
    # uploaded bytes equal a specific preprint revision.
    for k in ("title", "authors", "year", "journal"):
        st.session_state[f"kb_{k}_{digest}"] = str(record.get(k) or "")
    st.session_state[f"kb_checked_{digest}"] = False
    st.session_state[f"kb_adopted_arxiv_{digest}"] = record
    st.session_state.pop(f"kb_selected_metadata_{digest}", None)


def link_published_version(digest: str, candidate: dict, query_title: str, searched_utc: str) -> None:
    # NOT the primary DOI/PMID/PMCID of this PREPRINT PDF.
    st.session_state[f"kb_related_publication_{digest}"] = {
        "relation_type": "POTENTIAL_PREPRINT_OF",
        "verification_status": "USER_LINKED_NOT_CONTENT_VERIFIED",
        "source": candidate.get("source"),
        "search_title": query_title,
        "searched_utc": searched_utc,
        "candidate": {k: candidate.get(k) for k in
                      ("title", "doi", "pmid", "pmcid", "year", "authors", "journal", "similarity")},
    }
    st.session_state[f"kb_checked_{digest}"] = False


def adopt_candidate(digest: str, candidate: dict, query_title: str, searched_utc: str) -> None:
    # Button on_click fires before widgets are constructed in the next rerun.
    for field in ("title", "doi", "pmid", "pmcid", "authors", "year", "journal"):
        st.session_state[f"kb_{field}_{digest}"] = str(candidate.get(field) or "")
    st.session_state[f"kb_checked_{digest}"] = False
    st.session_state[f"kb_selected_metadata_{digest}"] = {
        "source": candidate.get("source"), "query_title": query_title,
        "searched_utc": searched_utc, "similarity": candidate.get("similarity"),
        "candidate": {k: candidate.get(k) for k in ("title", "doi", "pmid", "pmcid", "year", "authors", "journal")},
        "selection_method": "EXPLICIT_USER_BUTTON",
    }


st.set_page_config(page_title="MedEvidence · 医学知识库管理", layout="wide")
st.title("MedEvidence · 医学知识库管理（V1a.3）")
st.caption("现有知识库只读 · PDF 标题建议与 arXiv 版本关联 · 联网查书目需主动点击 · 不修改 Qdrant")

@st.cache_data(show_spinner=False)
def corpus_snapshot(docs_mtime: float, chunks_mtime: float):
    return existing_corpus_info()

mt_doc = DOCUMENTS_JSONL.stat().st_mtime if DOCUMENTS_JSONL.exists() else -1.0
mt_chunk = CHUNKS_JSONL.stat().st_mtime if CHUNKS_JSONL.exists() else -1.0
info = corpus_snapshot(mt_doc, mt_chunk)

left, mid, right = st.columns(3)
left.metric("现有文献元数据记录", f"{info['documents']:,}")
mid.metric("现有检索 Chunk 行数", f"{info['chunks']:,}")
right.metric("待入库文件数", len(list_staged()))
for warning in info["warnings"]:
    st.warning(warning)

st.divider()
st.subheader("1. 添加新文献（先预览，不直接入库）")
st.info("支持 PMC/JATS XML 与文字型 PDF；优先识别 arXiv 文件名并排除作者单位伪标题；书目查询需手动点击，不上传 PDF；正式全文入库与索引发布尚未实现。")
files = st.file_uploader("选择医学论文（可多选）", type=["xml", "pdf"], accept_multiple_files=True,
                         help="文件只在本地浏览器会话中解析；只有点击‘保存到待入库区’才会写入项目目录。")

if st.button("解析并预览", type="primary", disabled=not files):
    st.session_state["kb_previews"] = {}
    for uploaded in files or []:
        try:
            content = uploaded.getvalue()
            result = analyze_upload(uploaded.name, content, info, list_staged())
            st.session_state["kb_previews"][result["sha256"]] = result
        except Exception as exc:
            st.error(f"{uploaded.name}：{type(exc).__name__}: {exc}")

previews = st.session_state.get("kb_previews", {})
selected_hashes = {hashlib.sha256(u.getvalue()).hexdigest() for u in files or []}
valid_previews = [x for key, x in previews.items() if key in selected_hashes]
if valid_previews:
    st.subheader("解析结果（不是正式入库结果）")
    for p in valid_previews:
        with st.expander(f"{p['filename']} · {p['format']} · {p['title'][:70]}", expanded=len(valid_previews)==1):
            digest = p["sha256"]
            st.caption("书目标题相同也可能是不同版本。预印本的期刊版 DOI/PMID/PMCID 只应保存为关联记录；不能直接写进预印本本身。")
            version_choices = ["UNKNOWN", "PREPRINT", "PUBLISHED"]
            suggested = "PREPRINT" if p.get("arxiv_id") else "UNKNOWN"
            version_type = st.selectbox(
                "当前上传 PDF 的版本类型（必须根据首页人工核对）", version_choices,
                index=version_choices.index(suggested), key="kb_version_type_" + digest,
                format_func=lambda v: {
                    "UNKNOWN": "尚未确认版本",
                    "PREPRINT": "预印本 / arXiv 版本",
                    "PUBLISHED": "期刊正式发表版本",
                }[v],
            )
            if p.get("arxiv_id"):
                st.info(f"检测到 arXiv 编号候选：{p['arxiv_id']}{p.get('arxiv_version') or ''} "
                        f"（来源：{p.get('arxiv_detected_from', '未知')}）。文件名或文字中的编号需要核验，并不自动证明 PDF 对应的具体版本。")
                if st.button("按 arXiv 编号查询预印本原始书目", key="kb_arxiv_find_" + digest):
                    with st.spinner("查询 arXiv API，不上传 PDF 文件……"):
                        try:
                            record = lookup_arxiv_by_id(p["arxiv_id"])
                            st.session_state["kb_arxiv_record_" + digest] = record
                            st.session_state.pop("kb_arxiv_error_" + digest, None)
                        except Exception as exc:
                            st.session_state["kb_arxiv_error_" + digest] = f"{type(exc).__name__}: {exc}"
                if st.session_state.get("kb_arxiv_error_" + digest):
                    st.warning("arXiv 查询失败，仍可手动核对：" + st.session_state["kb_arxiv_error_" + digest])
                original = st.session_state.get("kb_arxiv_record_" + digest)
                if original and original.get("arxiv_id") == p["arxiv_id"]:
                    st.write("**arXiv 官方记录候选：**", original["title"])
                    st.caption(f"作者：{original.get('authors') or '未知'} ｜ 年份：{original.get('year') or '未知'} "
                               f"｜ arXiv：{original['arxiv_id']}{original.get('arxiv_version') or ''}")
                    if original.get("published_doi_hint"):
                        st.caption("记录所带期刊 DOI 提示：" + original["published_doi_hint"] + "（不是当前预印本的 DOI）")
                    st.button("采用这条 arXiv 预印本标题/作者/年份", key="kb_arxiv_choose_" + digest,
                              disabled=version_type != "PREPRINT", on_click=adopt_arxiv_record,
                              args=(digest, original))
            if p.get("other_title_candidate"):
                st.caption("PDF 首页另外提取到的标题候选（可能不准确）：" + p["other_title_candidate"][:150])
            title = st.text_input("论文标题（核对首页后可修改）", value=p["title"], key="kb_title_" + digest)
            id_left, id_mid, id_right = st.columns(3)
            if version_type == "PREPRINT" and any(p.get(x) for x in ("doi", "pmid", "pmcid")):
                st.warning("PDF 文字中识别的编号可能属于正式发表版本或参考文献。预印本默认不自动填写这些编号；" 
                           "原文匹配候选：DOI=" + str(p.get("doi") or "无") +
                           "，PMID=" + str(p.get("pmid") or "无") +
                           "，PMCID=" + str(p.get("pmcid") or "无"))
            with id_left:
                pmcid = st.text_input("PMCID（没有可留空）", value=p.get("pmcid", "") if version_type != "PREPRINT" else "", key="kb_pmcid_" + digest)
            with id_mid:
                pmid = st.text_input("PMID（没有可留空）", value=p.get("pmid", "") if version_type != "PREPRINT" else "", key="kb_pmid_" + digest)
            with id_right:
                doi = st.text_input("DOI（没有可留空）", value=p.get("doi", "") if version_type != "PREPRINT" else "", key="kb_doi_" + digest)

            st.caption("可选字段（联网采用候选后自动填充）：")
            meta_left, meta_mid, meta_right = st.columns(3)
            with meta_left:
                authors = st.text_input("作者", value=p.get("authors", ""), key="kb_authors_" + digest)
            with meta_mid:
                year = st.text_input("发表年份", value=p.get("year", ""), key="kb_year_" + digest)
            with meta_right:
                journal = st.text_input("期刊 / 预印本平台", value=p.get("journal", ""), key="kb_journal_" + digest)

            st.markdown("**书目元数据联网补全（由你主动发起）**")
            st.caption("只在点击按钮后发送当前标题；另外可按 arXiv ID 单独查询。不会上传 PDF、正文、哈希或个人文献库。")
            if st.button("按当前标题联网查询 DOI / PMID / PMCID", key="kb_search_" + digest):
                with st.spinner("正在联网检索公开书目记录……"):
                    try:
                        st.session_state["kb_lookup_" + digest] = lookup_by_title(
                            title, email=os.getenv("CROSSREF_EMAIL", ""),
                            ncbi_email=os.getenv("NCBI_EMAIL", ""),
                            ncbi_api_key=os.getenv("NCBI_API_KEY", ""))
                    except Exception as exc:
                        st.session_state["kb_lookup_" + digest] = {
                            "query_title": title, "candidates": [], "warnings": [f"联网查询失败：{type(exc).__name__}: {exc}"],
                            "searched_utc": "",
                        }
            found = st.session_state.get("kb_lookup_" + digest)
            if found:
                for warning in found.get("warnings", []):
                    st.warning(warning)
                if found.get("query_title") != title:
                    st.info("当前标题与上次查询的标题不一致；请修改后重新联网查询。")
                elif not found.get("candidates"):
                    st.info("没有找到符合最低标题相似度要求的候选记录。可暂时留空编号，不要猜测。")
                else:
                    st.caption(f"找到 {len(found['candidates'])} 条候选；以下匹配分数仅反映标题字符串相似度，不是论文身份认证。")
                    st.caption("标题匹配度低于 88% 的候选不可一键采用；需要独立核查后手动填写。")
                    for n, item in enumerate(found["candidates"], 1):
                        st.markdown(f"**候选 {n} · {item['source']} · {item['match_label']} · 标题相似度 {item['similarity']:.1%}**")
                        st.write(item["title"])
                        st.caption(f"年份：{item['year'] or '未知'} ｜ 作者：{item['authors'] or '未知'} ｜ 期刊：{item['journal'] or '未知'}")
                        st.caption(f"DOI：{item['doi'] or '无'} ｜ PMID：{item['pmid'] or '无'} ｜ PMCID：{item['pmcid'] or '无'}")
                        if version_type == "PREPRINT":
                            st.button("关联为可能的正式发表版本（不覆盖预印本编号）", key=f"kb_link_{digest}_{n}",
                                      disabled=item["similarity"] < 0.88,
                                      on_click=link_published_version,
                                      args=(digest, item, found["query_title"], found.get("searched_utc", "")))
                        else:
                            st.button("作为当前 PDF 的书目元数据（仅适用于期刊正式版）", key=f"kb_choose_{digest}_{n}",
                                      disabled=(item["similarity"] < 0.88 or version_type != "PUBLISHED"),
                                      on_click=adopt_candidate,
                                      args=(digest, item, found["query_title"], found.get("searched_utc", "")))
            related = st.session_state.get("kb_related_publication_" + digest)
            if related:
                rc = related.get("candidate", {})
                st.info("已建立**待核验的版本关联**：疑似正式发表版本「" +
                        str(rc.get("title") or "") + "」，DOI=" + str(rc.get("doi") or "无") +
                        "；它的 PMID / PMCID 不会写入预印本的编号字段。")
                published_duplicate = check_duplicates({
                    "doi": rc.get("doi"), "pmid": rc.get("pmid"), "pmcid": rc.get("pmcid")
                }, info, list_staged())
                if published_duplicate:
                    st.warning("疑似关联发表版本已有收录记录：" + "；".join(published_duplicate) +
                               "。请不要把关联发表版本重复当作一篇全新论文入库。")
            chosen = st.session_state.get("kb_selected_metadata_" + digest)
            if chosen:
                st.info(f"已选择来自 {chosen['source']} 的候选；请重新核对标题、年份、作者与 DOI/PMID/PMCID，并在下方勾选人工核对。")
            st.write("**标题来源：**", "含 arXiv ID 的文件名（候选）" if p.get("title_source") == "PDF_FILENAME_ARXIV_CANDIDATE" else ("PDF 首页字体与位置推测" if p.get("title_source") == "PDF_FONT_HEURISTIC" else "原文件解析或未识别"))
            st.write("**文件 SHA256：**", digest)
            st.write("**状态：** 仅解析预览，未进入检索索引")
            if pmcid.strip() and not normalize_pmcid(pmcid):
                st.error("PMCID 格式不正确，示例：PMC1234567")
            if doi.strip() and not normalize_doi(doi):
                st.error("DOI 格式不正确，示例：10.1000/example")
            if pmid.strip() and not normalize_pmid(pmid):
                st.error("PMID 格式不正确，应为纯数字")
            edited = {**p, "title": title.strip(), "pmcid": pmcid.strip(), "pmid": pmid.strip(), "doi": doi.strip()}
            duplicate_flags = check_duplicates(edited, info, list_staged())
            if duplicate_flags:
                st.error("疑似重复：" + "；".join(duplicate_flags) + "。只能暂存核对，不代表允许入库。")
            else:
                st.success("暂未根据 PMCID / PMID / DOI / 暂存文件哈希发现重复；仍需核对标题和其他标识。")
            for w in p["warnings"]:
                st.warning(w)
            page_previews = p.get("page_previews") or []
            if p["format"] == "PDF_TEXT":
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("PDF 总页数", p.get("page_count", 0))
                m2.metric("已检查页数", p.get("pages_scanned", 0))
                m3.metric("有文本的页数", p.get("pages_with_text", 0))
                m4.metric("已检查页提取字符", f"{p.get('scanned_text_chars', 0):,}")
                if page_previews:
                    opts = [item["page"] for item in page_previews]
                    selected = st.selectbox("选择要预览的 PDF 页码（当前最多检查前 30 页）", opts,
                                            format_func=lambda n: f"第 {n} 页 · {page_previews[n-1]['chars']:,} 字符",
                                            key="kb_page_" + digest)
                    record = page_previews[selected-1]
                    st.text_area("该页提取文字预览（只读，单页最多显示 12,000 字符）",
                                 value=record["text"] or "（本页无可提取文本）",
                                 height=340, disabled=True, key="kb_page_txt_" + digest + str(selected))
                else:
                    st.warning("没有可以预览的 PDF 页面文字。")
            else:
                st.text_area("提取文字预览（只读）", value=p["preview"] or "（未抽取到可读正文）",
                             height=300, disabled=True, key="kb_preview_" + digest)
            st.checkbox("我已对照原文首页核对标题和论文标识", value=False, key="kb_checked_" + digest)
    confirm = st.checkbox("我明白：这里只是保存原始文件到待入库区，不会更新 921 篇论文或检索索引。")
    if st.button("保存已预览文件到待入库区", disabled=not confirm):
        uploaded_by_hash = {hashlib.sha256(u.getvalue()).hexdigest(): u for u in files or []}
        saved = 0
        for p in valid_previews:
            try:
                u = uploaded_by_hash[p["sha256"]]
                digest = p["sha256"]
                title = st.session_state.get("kb_title_" + digest, p["title"]).strip()
                pmcid = st.session_state.get("kb_pmcid_" + digest, p["pmcid"]).strip()
                doi = st.session_state.get("kb_doi_" + digest, p["doi"]).strip()
                pmid = st.session_state.get("kb_pmid_" + digest, p.get("pmid", "")).strip()
                authors = st.session_state.get("kb_authors_" + digest, "").strip()
                year = st.session_state.get("kb_year_" + digest, "").strip()
                journal = st.session_state.get("kb_journal_" + digest, "").strip()
                if not title or title.startswith("（PDF 标题需核对"):
                    raise ValueError("请核对并填写论文标题后再暂存")
                if pmcid and not normalize_pmcid(pmcid):
                    raise ValueError("PMCID 格式不正确")
                if doi and not normalize_doi(doi):
                    raise ValueError("DOI 格式不正确")
                if pmid and not normalize_pmid(pmid):
                    raise ValueError("PMID 格式不正确")
                if year and (not year.isdigit() or len(year) != 4):
                    raise ValueError("发表年份需要填写四位数字，或者留空")
                checked = st.session_state.get("kb_checked_" + digest, False)
                version_type = st.session_state.get("kb_version_type_" + digest, "UNKNOWN")
                arxiv = p.get("arxiv_id") or ""
                related_record = st.session_state.get("kb_related_publication_" + digest)
                arxiv_record = st.session_state.get("kb_adopted_arxiv_" + digest)
                validate_version_identifiers(version_type, doi=doi, pmid=pmid,
                                             pmcid=pmcid, related_published=related_record)
                if version_type == "PREPRINT" and related_record:
                    candidate_title = str((related_record.get("candidate") or {}).get("title") or "")
                    if title_similarity(title, candidate_title) < 0.88:
                        raise ValueError("当前标题与已关联的期刊版候选不再匹配；请重新查询并确认版本关联")
                if version_type != "PREPRINT" and related_record:
                    # avoid storing stale link if user changed version in UI
                    related_record = None
                source_record = st.session_state.get("kb_selected_metadata_" + digest)
                if version_type == "PREPRINT":
                    source_record = None
                if source_record:
                    original_candidate = source_record["candidate"]
                    current = {"title": title, "pmcid": normalize_pmcid(pmcid),
                               "pmid": normalize_pmid(pmid), "doi": normalize_doi(doi)}
                    current.update({"authors": authors, "year": year, "journal": journal})
                    modified = any(current[k] != str(original_candidate.get(k) or "") for k in current)
                    source_record = {**source_record, "manually_modified_after_selection": modified}
                updated = {**p, "title": title, "pmcid": normalize_pmcid(pmcid),
                           "pmid": normalize_pmid(pmid), "doi": normalize_doi(doi),
                           "authors": authors, "year": year, "journal": journal,
                           "metadata_confirmed": bool(checked),
                           "metadata_provenance": source_record or
                           ({"source": "arXiv API", "selection_method": "USER_ADOPTED_PREPRINT"}
                            if arxiv_record and version_type == "PREPRINT" else
                            {"source": "LOCAL_PDF_OR_MANUAL"}),
                           "version_identity": {
                               "uploaded_version_type": version_type,
                               "arxiv_id": arxiv if version_type == "PREPRINT" else "",
                               "arxiv_version": (p.get("arxiv_version") or "") if version_type == "PREPRINT" else "",
                               "arxiv_detected_from": p.get("arxiv_detected_from", "NONE"),
                               "related_preprint_arxiv_id_hint": arxiv if version_type == "PUBLISHED" else "",
                               "arxiv_record_used": arxiv_record if version_type == "PREPRINT" else None,
                               "related_published_record": related_record if version_type == "PREPRINT" else None,
                               "version_confirmed_by_user": bool(checked),
                               "status": "UNVERIFIED_LINK" if related_record else "NO_CONFIRMED_LINK",
                           }}
                updated["duplicates"] = check_duplicates(updated, info, list_staged())
                path, created = stage_upload(u.name, u.getvalue(), updated)
                st.write(("新增暂存：" if created else "已存在，跳过：") + p["filename"])
                saved += int(created)
            except Exception as exc:
                st.error(f"暂存失败 {p['filename']}：{exc}")
        st.success(f"本次新增暂存 {saved} 个文件。原知识库和索引没有改变。")
        st.cache_data.clear()

st.divider()
st.subheader("2. 待入库区（尚不可用于 RAG 检索）")
staged = list_staged()
if not staged:
    st.caption("暂时没有待入库文件。")
else:
    rows = [{
        "文件": x.get("original_filename", ""),
        "PMCID": x.get("inspection", {}).get("pmcid", ""),
        "PMID": x.get("inspection", {}).get("pmid", ""),
        "DOI": x.get("inspection", {}).get("doi", ""),
        "文件版本": x.get("inspection", {}).get("version_identity", {}).get("uploaded_version_type", "UNKNOWN"),
        "arXiv": x.get("inspection", {}).get("version_identity", {}).get("arxiv_id", ""),
        "疑似期刊版 DOI": ((x.get("inspection", {}).get("version_identity", {})
                          .get("related_published_record") or {}).get("candidate") or {}).get("doi", ""),
        "状态": x.get("status", ""),
        "时间 UTC": x.get("staged_utc", ""),
        "已索引": "否",
    } for x in staged]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"暂存位置：{STAGING_ROOT}")

st.divider()
st.subheader("3. 后续要接入的功能")
st.markdown("PMCID 自动下载和许可检查 → 完整内容解析/表格核验 → Chunk 与 Parent 构建 → 生成独立 V3.2 版本及 BM25/Dense 索引 → 全量校验 → 手工确认发布/可回滚。")
st.caption(f"当前项目目录：{PROJECT_ROOT}")
