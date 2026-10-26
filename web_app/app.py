"""MedEvidence web interface. Execute with `python -m streamlit run web_app/app.py`."""
from __future__ import annotations

import html
import sys
import textwrap
from pathlib import Path

import streamlit as st

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))
from bridge import configuration_status, pmc_url
from reporting import export_question_report
from history_store import HistoryService

st.set_page_config(
    page_title="MedEvidence · 医学循证智能助手",
    page_icon="🩺", layout="wide", initial_sidebar_state="expanded",
)

st.markdown("""
<style>
:root {--ink:#172a3d;--navy:#102b41;--blue:#166f8c;--muted:#728399;--line:#e3eaf1;}
[data-testid="stAppViewContainer"] {background:#f5f8fb;}
[data-testid="stHeader"] {background:transparent;}
[data-testid="stSidebar"] {background:#fdfefe;border-right:1px solid #e8edf4;}
[data-testid="stSidebar"] .stButton button {border-radius:11px;text-align:left;}
.block-container {max-width:1080px;padding-top:2.25rem;padding-bottom:6rem;}
.hero {background:linear-gradient(112deg,#102c42,#14526a 62%,#18798a); color:#fff;
       border-radius:23px;padding:34px 38px;margin-bottom:24px;box-shadow:0 12px 34px rgba(16,44,66,.13);}
.hero-label {font-family:Arial,'Microsoft YaHei',sans-serif;letter-spacing:2.3px;font-size:.75rem;font-weight:800;color:#a5e2de;margin-bottom:13px;}
.hero h1 {color:white;font-size:2.05rem;letter-spacing:-.6px;margin:0 0 9px;font-weight:780;}
.hero p {max-width:680px;color:#d0e4ec;margin:0;font-size:.97rem;line-height:1.8;}
.hero-strip{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px;}
.hero-pill {font-size:.78rem;border-radius:100px;background:#ffffff1c;border:1px solid #ffffff29;padding:7px 13px;color:#e9f7f8;}
.kicker {font-weight:800;letter-spacing:1.4px;color:#1b7e91;font-size:.76rem;margin-top:6px;}
.section-heading {color:#162b3b;font-size:1.15rem;font-weight:800;margin:8px 0 12px;}
.side-logo {font-family:Arial,'Microsoft YaHei',sans-serif;font-weight:800;font-size:1.25rem;color:#153449;padding:12px 0 4px;}
.side-subtitle {font-size:.82rem;color:#6e8293;margin-bottom:18px;}
.tag {display:inline-block;border-radius:999px;font-size:.74rem;font-weight:750;padding:6px 12px;margin-bottom:13px;}
.tag-answered {background:#daf4e8;color:#0f7452;}
.tag-partial {background:#fff1d5;color:#9a661c;}
.tag-insufficient_evidence {background:#feeaeb;color:#b04650;}
.tag-other {background:#eef1f8;color:#354b68;}
.mini-note {font-size:.82rem;color:#738499;line-height:1.7;}
.reference-card {padding:12px 16px;background:white;border:1px solid #e3eaf1;border-radius:13px;margin-bottom:9px;}
.reference-id {color:#1f8294;font-size:.79rem;font-weight:800;letter-spacing:.8px;}
.reference-title {color:#1d3547;font-size:.94rem;font-weight:700;line-height:1.5;margin:4px 0;}
.reference-meta {color:#7a8999;font-size:.76rem;margin:5px 0;}
div[data-testid="stMetric"] {background:white;border:1px solid #e3eaf1;border-radius:15px;padding:16px 17px;}
[data-testid="stChatMessage"] {background:#fff;border:1px solid #e7edf3;border-radius:17px;margin:10px 0;}
[data-testid="stChatInput"] {box-shadow:0 -7px 26px rgba(16,44,66,.025);}
/* Keep composer legible regardless of OS/browser Streamlit dark theme. */
[data-testid="stChatInput"] {color-scheme:light;}
[data-testid="stChatInput"] textarea,
[data-testid="stChatInput"] [data-baseweb="textarea"] textarea {
    background-color:#fff!important;
    color:#172a3d!important;
    -webkit-text-fill-color:#172a3d!important;
    caret-color:#172a3d!important;
    opacity:1!important;
}
[data-testid="stChatInput"] textarea::placeholder {
    color:#738499!important;
    -webkit-text-fill-color:#738499!important;
    opacity:1!important;
}
[data-testid="stChatInput"] [data-baseweb="textarea"],
[data-testid="stChatInput"] > div {
    background-color:#fff!important;
}
/* The original UI also uses white message cards; keep their text visible. */
[data-testid="stChatMessage"] {
    color:#172a3d!important;
}
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li,
[data-testid="stChatMessage"] span:not([data-testid]) {
    color:inherit;
}
.stDownloadButton button{border-radius:11px;}
#MainMenu {visibility:hidden;} footer{visibility:hidden;}
/* MEDEVIDENCE_LIGHT_CONTENT_FIX_BEGIN */
/* Force readable foregrounds on light custom cards, regardless of Streamlit/browser dark preference. */
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stSidebar"] {
  color-scheme: light;
}
[data-testid="stChatMessage"] {
  background-color: #ffffff !important;
  color: #172a3d !important;
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] :is(
  p, li, h1, h2, h3, h4, h5, h6, strong, em, blockquote, th, td
) {
  color: #172a3d !important;
  -webkit-text-fill-color: #172a3d !important;
  opacity: 1 !important;
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] a,
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] a:visited {
  color: #0876bf !important;
  -webkit-text-fill-color: #0876bf !important;
}
[data-testid="stChatMessage"] [data-testid="stCaptionContainer"],
[data-testid="stChatMessage"] [data-testid="stCaptionContainer"] * {
  color: #637487 !important;
  -webkit-text-fill-color: #637487 !important;
}
[data-testid="stChatMessage"] [data-testid="stMetric"] {
  background: #ffffff !important;
  color: #172a3d !important;
}
[data-testid="stChatMessage"] [data-testid="stMetric"] :is(
  [data-testid="stMetricLabel"], [data-testid="stMetricValue"],
  [data-testid="stMetricLabel"] *, [data-testid="stMetricValue"] *
) {
  color: #172a3d !important;
  -webkit-text-fill-color: #172a3d !important;
  opacity: 1 !important;
}
[data-testid="stChatMessage"] [data-testid="stAlert"] {
  color: #172a3d !important;
}
[data-testid="stChatMessage"] [data-testid="stAlert"] :is(p, li, strong, em, span) {
  color: #172a3d !important;
  -webkit-text-fill-color: #172a3d !important;
  opacity: 1 !important;
}
[data-testid="stChatMessage"] [data-testid="stExpander"] summary,
[data-testid="stChatMessage"] [data-testid="stExpander"] summary :is(span, p) {
  color: #172a3d !important;
  -webkit-text-fill-color: #172a3d !important;
}
/* Custom tags deliberately retain their semantic green/yellow/red ink. */
[data-testid="stChatMessage"] .tag-answered {color:#0f7452 !important;-webkit-text-fill-color:#0f7452 !important;}
[data-testid="stChatMessage"] .tag-partial {color:#9a661c !important;-webkit-text-fill-color:#9a661c !important;}
[data-testid="stChatMessage"] .tag-insufficient_evidence {color:#b04650 !important;-webkit-text-fill-color:#b04650 !important;}
[data-testid="stChatMessage"] .reference-id {color:#1f8294 !important;-webkit-text-fill-color:#1f8294 !important;}
[data-testid="stChatMessage"] .reference-title {color:#1d3547 !important;-webkit-text-fill-color:#1d3547 !important;}
[data-testid="stChatMessage"] .reference-meta {color:#7a8999 !important;-webkit-text-fill-color:#7a8999 !important;}
/* Composer fix kept even when OS/Streamlit is in dark mode. */
[data-testid="stChatInput"] textarea {
  background:#ffffff !important;
  color:#172a3d !important;
  -webkit-text-fill-color:#172a3d !important;
  caret-color:#172a3d !important;
}
[data-testid="stChatInput"] textarea::placeholder {color:#68798b !important;-webkit-text-fill-color:#68798b !important;}
/* MEDEVIDENCE_LIGHT_CONTENT_FIX_END */

/* General answer tag and mode selection remain visible under light theme. */
.tag-model_knowledge {background:#e8f0ff;color:#295592;}
[data-testid="stChatMessage"] .tag-model_knowledge {color:#295592 !important;-webkit-text-fill-color:#295592 !important;}
</style>
""", unsafe_allow_html=True)

EXAMPLES = [
    "成人2型糖尿病患者，SGLT2 抑制剂与 GLP-1 受体激动剂在心血管及肾脏结局方面有什么区别？",
    "对于 HFrEF 患者，ARNI、β受体阻滞剂、MRA 和 SGLT2 抑制剂分别有哪些循证依据？",
    "针对静脉血栓栓塞症，现有医学证据对抗凝疗程及延长治疗有哪些限制？",
]

@st.cache_resource(show_spinner=False)
def _history_service() -> HistoryService:
    # A single queue worker is shared across Streamlit browser sessions.
    return HistoryService()


history = _history_service()
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None

status = configuration_status()
with st.sidebar:
    st.markdown('<div class="side-logo">✚ MedEvidence</div><div class="side-subtitle">Clinical Evidence · Research Assistant</div>', unsafe_allow_html=True)
    page = st.radio("工作区", ["循证问答", "知识库概览", "项目与使用说明"], label_visibility="collapsed", key="page")
    st.divider()
    st.caption("运行状态")
    if status["code_ready"] and status["key_configured"]:
        st.success("Agent 已配置 · 本地服务")
    elif not status["code_ready"]:
        st.error("找不到 Agent 脚本")
    else:
        st.warning("尚未检测到 API Key")
    st.caption(f"模型配置 · {status['model']}")
    st.caption("后端版本 · V0.3.0 SDK")
    st.divider()
    st.caption("数据资产")
    a, b = st.columns(2)
    a.metric("医学论文", "922")
    b.metric("证据片段", "42,553")
    st.caption("固定本地知识库；与 BioASQ 独立实验不混用")
    st.divider()
    if st.button("＋ 新建对话", use_container_width=True):
        # The old conversation remains safely stored in SQLite.
        st.session_state.conversation_id = None
        st.rerun()
    st.markdown("**历史对话**")
    saved = history.list_conversations()
    if not saved:
        st.caption("暂无已保存的对话")
    for convo in saved:
        cid = convo['id']
        title = convo['title']
        selected = cid == st.session_state.conversation_id
        if st.button(("● " if selected else "◦ ") + title, key=f"hist_{cid}", use_container_width=True):
            st.session_state.conversation_id = cid
            st.rerun()
    counts = history.counts()
    st.caption(f"后台任务：运行 {counts['running']} · 排队 {counts['queued']}")
    st.caption("多条问题可连续提交；为了保护本地 Qdrant，Agent 后台逐条执行。")
    st.caption("历史记录存储在本机 web_app/data/history.sqlite3。")
    st.caption("仅供医学科研与教学，不用于个人诊断、处方或急救决策。")

if page == "循证问答":
    st.markdown(textwrap.dedent("""
    <div class="hero">
      <div class="hero-label">MEDICAL EVIDENCE INTELLIGENCE</div>
      <h1>让医学回答，建立在证据之上。</h1>
      <p>常见医学问题可快速获取通用解释；专业研究问题可切换严格循证模式，检索本地文献并核验引用。</p>
      <div class="hero-strip"><span class="hero-pill">⌁ 混合检索</span><span class="hero-pill">◎ 自适应 Agent</span><span class="hero-pill">▣ 引用核验</span><span class="hero-pill">◈ 本地知识库</span></div>
    </div>
    """), unsafe_allow_html=True)
    st.markdown('<div class="kicker">ASK WITH EVIDENCE</div>', unsafe_allow_html=True)
    mode_choice = st.radio(
        "选择问答模式",
        options=["通用医学问答（快速）", "严格循证问答（文献核验）"],
        horizontal=True, key="active_qa_mode",
        help="通用模式使用 Qwen 模型知识，不自动生成或伪造引用；严格模式沿用原始 RAG Agent。",
    )
    selected_mode = "strict" if mode_choice.startswith("严格") else "general"
    prefer_evidence = False
    if selected_mode == "general":
        st.caption("快速模式直接调用 Qwen，不启动本地 Qdrant；回答不经过文献核验。")
        prefer_evidence = st.checkbox(
            "先尝试本地文献检索（较慢；如果证据不足再使用通用回答）",
            value=False, key="prefer_local_evidence",
        )
    else:
        st.caption("严格模式使用原有 RAG Agent；证据不足时会拒答，不会自动使用模型知识补齐。")
    if not st.session_state.conversation_id:
        st.markdown("#### 从一个医学问题开始")
        st.caption("也可以点击下方示例问题。首次查询需要加载本地嵌入模型，因此可能较慢。")
        for i, ex in enumerate(EXAMPLES):
            if st.button("↗  " + ex, key=f"example_{i}", use_container_width=True):
                st.session_state.pending_question = ex
        st.markdown(" ")

    def show_answer(result: dict, *, question: str, job_id: str) -> None:
        code = result.get("status", "other")
        labels = {"answered": "已完成 · Agent 引用核验通过", "partial": "部分回答 · 证据仍有缺口", "insufficient_evidence": "证据不足 · 未生成医学结论", "model_knowledge": "通用模型知识 · 未经本地文献核验"}
        cls = code if code in labels else "other"
        label = labels.get(code, "Agent 返回状态：" + str(code))
        st.markdown(f'<span class="tag tag-{cls}">{html.escape(label)}</span>', unsafe_allow_html=True)
        st.write(str(result.get("answer", "暂无回答内容。")))
        if result.get("answer_origin") == "model_knowledge" or code == "model_knowledge":
            st.warning("这段回答来自语言模型知识，未经过本地医学证据或引用核验，不应当作诊疗依据。")
            attempt = result.get("local_attempt")
            if isinstance(attempt, dict):
                if attempt.get("outcome") == "error":
                    st.caption("本地检索运行失败，已明确切换到通用模型回答（不包含文献引用）。")
                else:
                    st.caption("本地检索未形成可完整核验的答案，已切换为通用模型知识回答。")
            if result.get("model"):
                st.caption("生成模型：" + str(result["model"]))
            if result.get("limitations"):
                with st.expander("知识来源与注意事项", expanded=False):
                    for item in result["limitations"]:
                        st.markdown("- " + str(item))
            st.download_button(
                "↓ 导出本次问答（Markdown）",
                data=export_question_report(question, result),
                file_name=f"MedEvidence_{str(result.get('run_id') or job_id)[:80]}.md",
                mime="text/markdown", key=f"download_{job_id}",
            )
            return
        if code == "partial":
            st.info("这是部分回答。请结合下方来源和限制信息理解，未覆盖的临床维度不能视为已有充分证据。")
        elif code == "insufficient_evidence":
            st.warning("目前未取得足以支持医学结论的引用，系统已选择保守回答。")
        col1, col2, col3 = st.columns(3)
        rounds = result.get("rounds", 0)
        try:
            pct = min(100, max(0, float(result.get("coverage_ratio", 0)) * 100))
        except (ValueError, TypeError):
            pct = 0.0
        citations = result.get("evidence", [])
        col1.metric("检索调用", f"{rounds} 次")
        col2.metric("启发式覆盖", f"{pct:.0f}%")
        col3.metric("登记来源", f"{len(citations)} 条")
        st.caption("启发式覆盖率仅衡量内部检索主题覆盖，不代表引用核验通过率或医学准确率；因此即使显示 100%，也可能因引用不足而拒答。")
        if result.get("claims"):
            with st.expander(f"已核验引用的医学主张 · {len(result['claims'])} 条", expanded=True):
                for i, claim in enumerate(result["claims"], 1):
                    st.markdown(f"**{i}.** {claim.get('text', '')}")
                    ids = ", ".join(str(s) for s in claim.get("citation_ids", []))
                    st.caption("引用 ID：" + ids)
        if citations:
            with st.expander(f"检索到的文献证据 · {len(citations)} 条", expanded=True):
                for e in citations:
                    title = html.escape(str(e.get("title") or "未标注文献标题"))
                    evidence_id = html.escape(str(e.get("id") or "?"))
                    pmcid = str(e.get("pmcid") or "")
                    sec = html.escape(str(e.get("section") or "未指定章节"))
                    link = pmc_url(pmcid)
                    st.markdown(
                        f'<div class="reference-card"><div class="reference-id">{evidence_id}</div>'
                        f'<div class="reference-title">{title}</div>'
                        f'<div class="reference-meta">{html.escape(pmcid or "PMCID 未提供")} · {sec}</div></div>',
                        unsafe_allow_html=True,
                    )
                    if link:
                        st.markdown(f"[查看 {evidence_id} PMC 原文 ↗]({link})")
        if result.get("limitations"):
            with st.expander("限制条件与未解决的问题", expanded=code != "answered"):
                for item in result["limitations"]:
                    st.markdown("- " + str(item))
        st.download_button(
            "↓ 导出本次问答（Markdown）",
            data=export_question_report(question, result),
            file_name=f"MedEvidence_{str(result.get('run_id') or job_id)[:80]}.md",
            mime="text/markdown", key=f"download_{job_id}",
        )

    @st.fragment(run_every="3s")
    def display_conversation():
        cid = st.session_state.conversation_id
        if cid is None:
            return
        records = history.get_questions(cid)
        for entry in records:
            with st.chat_message("user", avatar="👤"):
                st.write(entry["question"])
            with st.chat_message("assistant", avatar="🩺"):
                st.caption("问答模式：" + ("严格循证" if entry.get("mode", "strict") == "strict" else "通用医学知识"))
                if entry['status'] == 'done' and entry.get('result'):
                    show_answer(entry['result'], question=entry['question'], job_id=entry['id'])
                elif entry['status'] == 'queued':
                    st.info("已加入后台队列。你可以继续提交其他问题，或打开历史对话。")
                elif entry['status'] == 'running':
                    st.info("⏳ 正在检索本地医学证据并生成回答……")
                else:
                    st.error(entry.get('error') or '本次查询未完成。')
        if records and any(item['status'] in ('queued', 'running') for item in records):
            st.caption("页面每 3 秒自动刷新任务状态；关闭浏览器标签不会清除已经保存的任务。")

    display_conversation()
    question = st.session_state.pop("pending_question", None)
    submitted = st.chat_input("输入医学问题（按上方选择的模式处理）…", max_chars=1800)
    if submitted:
        question = submitted
    if question:
        try:
            cid = history.enqueue(question, st.session_state.conversation_id,
                                  mode=selected_mode, prefer_evidence=prefer_evidence)
            st.session_state.conversation_id = cid
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

elif page == "知识库概览":
    st.markdown('<div class="kicker">KNOWLEDGE BASE</div>', unsafe_allow_html=True)
    st.title("医学证据知识库")
    st.caption("本页展示现有 MedEvidence V0.3 项目经过验证的静态资产规模，不是实时在线医学数据库。")
    a, b, c = st.columns(3)
    a.metric("PMC 文献", "922")
    b.metric("分块证据", "42,553")
    c.metric("检索引擎", "Hybrid")
    st.divider()
    st.subheader("系统工作流程")
    st.markdown("**医学问题** → **BM25 / BGE-M3 检索** → **Qdrant 证据** → **Agent 缺口判断** → **引用核验** → **有依据的回答或保守拒答**")
    st.info("BGE-M3 向量检索与 SQLite FTS5/BM25 联合工作，文献证据实际来源于部署电脑上的本地知识库。")
    st.subheader("工程技术栈")
    st.markdown("Python · Qdrant · SQLite FTS5 · BGE-M3 · OpenAI Agents SDK · 阿里云百炼 Qwen · Streamlit")
    st.caption("现有索引版本：bge_m3_v3_2_trial_clean。索引无需因安装网页前端而重建。")

else:
    st.markdown('<div class="kicker">ABOUT THIS PROJECT</div>', unsafe_allow_html=True)
    st.title("MedEvidence · 项目说明")
    st.markdown("""
    ### 这是一个什么系统？
    MedEvidence 是面向医学研究与循证学习的**本地知识库问答 Agent**。它结合混合检索、动态证据补检和引用核验，尽可能避免在证据不足时生成缺乏来源的确定结论。

    ### 网页双模式
    - **通用医学问答（默认）**：直接调用现有 Qwen API，使用模型知识提供科普解释，不进行本地文献检索；未核验内容有明确标识，不能将其作为临床证据。
    - **可选本地证据优先**：先调用原 Agent，无法形成完整引用支持的回答时才回退到明确标注的模型知识回答；该功能可能较慢。
    - **严格循证问答**：保留原 Agent 的文献检索、引用检查及证据不足时的拒答机制。

    ### 当前网页接入的真实能力
    - **V0.3.0 Agent SDK**：本地混合检索、多轮证据搜索、证据覆盖判断、引用核验及保守拒答。
    - **V0.4.1 严格子问题级支持门控**：已完成独立实验，**尚未集成进这里的正式运行入口**。
    - **BioASQ 公开基准**：用于研究实验，不与当前 922 篇文献的端到端检索效果混为一谈。

    ### 使用与隐私
    - 网页默认仅监听本机 `127.0.0.1`，不会自动公开到互联网；问答记录保存在本机 SQLite 文件。
    - 本地 Qdrant 检索运行在当前电脑上，但医学问题及部分证据摘录会发往已配置的百炼模型 API。
    - 不要提交真实患者的姓名、身份证号、联系方式、完整病历等敏感信息。
    - 本系统**不构成医疗器械、诊断或治疗建议**，不适用于急症判断。

    ### 对话历史与并发能力
    - 左侧“历史对话”可以重新打开以前的问题和结果。
    - 可以连续提交多个问题，后台队列逐条执行；当前本地 Qdrant 模式**不支持多个 Agent 同时运行**。
    - 每次任务会保存其问答模式；升级前的记录保留并标注为严格循证模式。
    - 已保存的是各次独立问答记录，**不是带上下文记忆的多轮追问**；新问题需要写明医学背景。
    - 完全并行执行需要另行部署 Qdrant 服务端并验证模型内存及资源隔离。

    ### 如何停止服务？
    关闭启动网页的命令行窗口，或在其中按 `Ctrl + C`。
    """)

st.markdown('<div class="mini-note" style="margin-top:28px;border-top:1px solid #e3eaf1;padding-top:16px;">MEDEVIDENCE · Research prototype · 医学科研/教学辅助，不用于临床决策</div>', unsafe_allow_html=True)
