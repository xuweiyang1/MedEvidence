# 版本演进

按时间顺序记录关键节点。日期取自本地文件时间（2026 年）。

## 2026-10-13 / 10-14 — V1 数据层

- PMC OA 分疾病下载（936 篇）、MeSH desc2026 / qual2026、Disease Ontology 入库。
- JATS 解析、表格与图注恢复、分层切块 v3 / v3.1（parent/child）。
- 语料过滤、知识导入、BM25（FTS5）与 Dense（BGE-M3）索引，索引版本化与 manifest 校验。
- 24 条 pilot 证据卡与 Streamlit 复核界面；引用字面核验（64/65 命中）。

## 2026-10-15 → 10-18 — 评测与弱标签

- 自动评审脚本（v1 / v1.1 / v12）、弱标签整理、qrels 准备与盲评队列。
- 检索联合标注、候选池复核；明确记录"AI 标签不是 Gold"。

## 2026-10-21 — Agent SDK V0.2.x → V0.3.0

- 迁移到 OpenAI Agents SDK：Planner/Writer、function tools、guardrails、handoffs。
- V0.2.1/0.2.2/0.2.4/0.2.6/0.2.7 逐步修复线程化 SQLite、命名论文定位、诊断与轨迹隐私。
- V0.3.0 引入确定性证据充分性策略（`adaptive_policy.py`）、`decide_next_evidence_action` 工具、
  离线重放对照（`compare_adaptive_policies.py`）。

## 2026-10-22 — V0.4.0 / V0.4.1 实验

- 三臂对照实验（FIFO / 锚点多样化 / 缺口感知）与失败审计。
- 子问题级严格门控、独立复核材料；结果均为代理指标，未集成进正式入口。

## 2026-10-24 — 网页前端

- Web V1：提问、来源、Markdown 导出、SQLite 历史、单 worker 队列。
- Web V2：常驻 Agent（复用 Qdrant 连接与 BGE-M3），性能日志；CLI 模式可回退。
- Web V3：通用/严格双模式，明确区分"模型知识"与"文献核验"，修复浅色主题对比度。

## 2026-10-26 — 仓库重构（本次提交）

- 公开仓库从"数据层 + 冻结 V0.2.7"更新为当前完整版本：`agent/`、`web_app/`、`tests/`、
  评测与运行文档全部入库。
- 重写根 README 与 docs（架构、数据管线、评测、运行手册、本变更记录）。
- 清理：移除 `.bak` 文件、乱码文件名、冻结 SDK 目录（保留在 Git 历史）、重复的 AI 复核 CSV。
- 新增 `.env.agent.example`、`requirements.txt`、`run_web.bat`，补全 `.gitignore`。
- 研究产物归位：`reports/targeted_5q_audit/`、`reports/v041_gapaware_15cases_summary.json`。

## 2026-10-26 — 正确性、时延与可观测性优化

- **状态语义拆分**：新增 `agent/outcome.py`，运行故障返回 `status=error` 与
  `reason_code`/`failure_stage`，不再伪装成 `insufficient_evidence`；网页增加红色"运行失败"分支。
- **JSON 修复轮**：`QwenLLM` 在解析失败时把错误回喂模型一次，减少偶发格式失败导致的整轮丢弃。
- **少一轮模型调用**：`assess_evidence_coverage` 直接返回确定性 `next_action`，
  移除独立的 `decide_next_evidence_action` 工具；Writer 不再强制调用 `list_retrieved_evidence`，
  并在指令中明确"每条主张 1–3 个引用 ID"。
- **分角色模型**：新增 `MEDEVIDENCE_JUDGE_MODEL`，覆盖评估/引用核验可用更快模型。
- **用量统计**：记录 Agent 与判定模型的请求数/tokens，写入轨迹并在网页"本次运行信息"展示。
- **动态语料规模**：网页从索引 manifest 与 `documents.jsonl` 读取文献/块数，不再硬编码 922 / 42,553。
- **等待计时**：运行中的问题显示已等待秒数，减少"卡住了"的错觉。
- **诊断增强**：`--trace-diagnose` 输出 `reason_code`、`failure_stage` 与 token 用量。
- **测试与工程化**：新增 outcome、LLM 修复轮、配置默认值测试（61 → 75 项）；
  新增 `pyproject.toml`（ruff/pytest/coverage 配置）、`requirements-dev.txt` 与 GitHub Actions 离线测试。
- 文档同步：README、架构、评测、运行手册与网页说明更新状态语义与配置项。
