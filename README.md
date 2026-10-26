# MedEvidence

面向医学科研与循证学习的**本地循证问答系统**：把 PMC 开放获取全文做成可检索的证据库，用 Agent 反复检索、判断证据是否覆盖问题，再逐条核验引用；证据不足时明确拒答，而不是让语言模型自由发挥。

> 研究原型，用于科研与教学，**不构成诊断、处方、用药或急症建议**。

## 它解决什么问题

普通 RAG 的通病是"总能编出一段像样的答案"。MedEvidence 的目标相反：

- 回答只能由**通过引用核验的原子医学主张**组成；
- 检索轮次、证据覆盖、引用 ID 都是硬约束，不交给模型自由裁量；
- 检索不到、覆盖不足、引用对不上时，输出 `insufficient_evidence` 并说明缺口；
- 每条结论都能回溯到具体文献块与 PMCID。

## 核心能力

| 能力 | 实现 |
| --- | --- |
| 混合检索 | Qdrant 稠密向量（BGE-M3，1024 维，Cosine）+ SQLite FTS5 BM25，RRF（k=60）融合 |
| 多轮补检 | 每轮评估子问题覆盖，未覆盖则生成新查询继续检索，最多 3 轮（可配） |
| 确定性决策 | `agent/adaptive_policy.py` 依据覆盖、引用锚点、冲突与剩余预算决定 `search_again / answer / partial / abstain` |
| 引用核验 | `agent/citation_verifier.py` 对每条主张做语义支持核验，未通过的引用不计入答案 |
| 输出契约 | 严格 JSON：`status`、`claims[].citation_ids`、`limitations`；白名单外的引用直接 fail-closed |
| 可审计 | 每轮事件写入本地 JSON 轨迹，默认只记哈希与结构，不落问题原文 |
| 失败可区分 | 运行故障返回 `status=error` + `reason_code`，不伪装成"证据不足" |
| 网页双模式 | 通用科普（明确标注"未经本地文献核验"）/ 严格循证（检索 + 核验 + 拒答） |

## 架构

```text
                    ┌──────────────────────────────┐
  浏览器 ──────────▶│ Streamlit（web_app/）         │
                    │ 通用问答 / 严格循证 / 历史队列  │
                    └──────────────┬───────────────┘
                                   │ 单 worker 串行队列（SQLite）
                    ┌──────────────▼───────────────┐
                    │ Agent Runner（agent/）        │
                    │ OpenAI Agents SDK             │
                    │  Planner ──handoff──▶ Writer  │
                    └───┬───────────┬───────────┬──┘
        search / lookup │           │ assess    │ decide
                        ▼           ▼           ▼
                 ┌───────────┐ ┌─────────┐ ┌──────────────┐
                 │ Qdrant    │ │ SQLite  │ │ 覆盖评估(LLM) │
                 │ BGE-M3    │ │ FTS5    │ │ 确定性策略    │
                 └───────────┘ └─────────┘ └──────────────┘
                                   │
                        引用核验（LLM）→ answered / partial / insufficient_evidence / error
```

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `agent/` | Agent 核心：SDK 运行时、检索工具、覆盖评估、引用核验、自适应策略 |
| `web_app/` | Streamlit 前端：双模式、后台队列、历史记录、常驻检索器 |
| `scripts/` | 数据获取/解析/切块/建库/评测脚本，以及 CLI 入口 `run_agent.py` |
| `tests/` | Agent 离线单元测试（无需 API Key、无需索引） |
| `configs/` | 疾病域关键词表等配置 |
| `docs/` | 架构、数据管线、评测、运行手册与历史版本说明 |
| `reports/` | 语料构建、检索评测、标注复核的可读产物 |
| `licenses/` | 来源级与文章级许可登记 |

## 快速开始

### 1. 环境

- Python 3.10+（开发环境为 3.10.13）
- 本地已有 Qdrant 索引与 BM25 数据库（本仓库不含数据，构建方法见 [docs/data_pipeline.md](docs/data_pipeline.md)）
- 阿里云百炼 API Key（默认模型 `qwen-plus`）

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-web.txt   # 仅网页需要
```

> `FlagEmbedding` 会带入 PyTorch，首次安装体积较大；若已有可用的 BGE-M3 推理环境可只装 `qdrant-client`。

### 2. 配置

```powershell
Copy-Item .env.agent.example .env.agent
notepad .env.agent
```

至少填写 `DASHSCOPE_API_KEY`、`MEDEVIDENCE_QDRANT_PATH`、`MEDEVIDENCE_BM25_DB`。`.env.agent` 已在 `.gitignore` 中，**不要提交**。

### 3. 先做离线检查（不花钱）

```powershell
python scripts/run_agent.py --self-test      # 78 项离线单元测试
python scripts/run_agent.py --sdk-check      # 检查 SDK 组件图
python scripts/run_agent.py --retriever-check # 检查 Qdrant collection 与 FTS5 表
```

### 4. 提问

```powershell
python scripts/run_agent.py --question "对于成人 2 型糖尿病患者，SGLT2 抑制剂与 GLP-1 受体激动剂在肾脏结局证据上有哪些差异？"
python scripts/run_agent.py --question "..." --json    # 结构化输出
```

### 5. 网页

```powershell
python -m streamlit run web_app/app.py
```

打开 <http://127.0.0.1:8501>。仅监听本机；后台同一时间只跑一个 Agent，问题会排队。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | — | 百炼 API Key |
| `MEDEVIDENCE_MODEL` | `qwen-plus` | 模型名 |
| `MEDEVIDENCE_JUDGE_MODEL` | 同 `MEDEVIDENCE_MODEL` | 覆盖评估/引用核验用模型，可换更快模型降本 |
| `MEDEVIDENCE_API_BASE` | 百炼兼容地址 | 必须 HTTPS |
| `MEDEVIDENCE_RETRIEVER` | `qdrant` | `qdrant` 或 `hook` |
| `MEDEVIDENCE_QDRANT_PATH` | — | Qdrant local 目录（含 `meta.json`） |
| `MEDEVIDENCE_COLLECTION` | `bge_m3_v3_2_trial_clean` | collection 名 |
| `MEDEVIDENCE_BM25_DB` | — | SQLite FTS5 文件；留空则仅稠密检索 |
| `MEDEVIDENCE_DEVICE` | `cpu` | `cuda:0` 需与 PyTorch 版本匹配 |
| `MEDEVIDENCE_TOP_K` | 8 | 单轮返回块数 |
| `MEDEVIDENCE_MAX_ROUNDS` | 3 | 检索硬上限（1–4） |
| `MEDEVIDENCE_MAX_EVIDENCE` | 12 | 证据块上限（1–30） |
| `MEDEVIDENCE_COVERAGE_THRESHOLD` | 1.0 | `answered` 要求的启发式覆盖率；默认很保守 |
| `MEDEVIDENCE_SEMANTIC_VERIFY` | 1 | 关闭后只核验引用 ID，不核验文字支持 |
| `MEDEVIDENCE_TIMEOUT_SEC` | 90 | 单次模型调用超时 |

## 数据与索引

| 指标 | 数值 |
| --- | --- |
| 下载的 PMC OA 全文 | 936 篇 |
| 进入 v3.2 索引的文献 | 922 篇 |
| 检索块（child chunks） | 42,553 |
| Parent 单元 | 25,696 |
| 证据关系 | 116,640 |
| 向量 | BGE-M3，1024 维，Cosine |
| 稀疏检索 | SQLite FTS5（BM25） |

数据不入库，`data/`、`outputs/` 均已忽略。构建与校验方法见 [docs/data_pipeline.md](docs/data_pipeline.md)。

**许可口径**：PMC 仅收 CC0 / CC BY / CC BY-SA（不收 NC/ND）；MeSH 依 NLM 条款使用并致谢；Disease Ontology 为 CC0 1.0。逐篇许可与哈希见 `licenses/registry.csv` 与下载清单。

## 评测做到哪一步

- **证据卡试点（24 条）**：65 条引文中 64 条字面命中、1 条未找到；自动检查不等于医学复核，AI 建议标签不是 Gold。见 `reports/pilot24_quality_report.md`。
- **检索与切块**：有 BM25 / Dense 的离线评测与索引校验产物，见 `reports/`。
- **V0.4.1 子问题级门控（15 个 case，实验性）**：覆盖代理分约 0.51 → 0.60（三轮），属于 LLM 代理指标，**没有独立临床金标准**。见 `reports/v041_gapaware_15cases_summary.json`。

需要强调的是：**启发式覆盖率不是答案正确率**，引用字面匹配也不等于医学结论成立。

## 已知限制

- 单机、嵌入式 Qdrant，同一时间只能有一个进程打开索引；网页用单 worker 队列规避。
- 默认 `coverage_threshold=1.0`，多数真实问题会以 `partial` 收尾，这是保守设计而非故障。
- 语义引用核验仍由 LLM 完成，不能替代专家审阅；运行故障会明确返回 `error`，不会冒充证据结论。
- 问题与必要证据片段会发送到所配置的模型 API；不要输入患者身份信息。
- 尚无多轮对话记忆：每条问题独立检索。

## 文档

- [架构与设计](docs/architecture.md)
- [数据管线](docs/data_pipeline.md)
- [评测说明](docs/evaluation.md)
- [运行与排错](docs/runbook.md)
- [版本演进](docs/CHANGELOG.md)

## 版本说明

当前代码对应 Agent V0.3.1 + 网页双模式 V3。旧版 V0.2.x 冻结 SDK 已从工作树移除，但仍保留在 Git 历史（首个提交）中；如需对照，可 `git show 586dce3 --stat`。

## 许可与引用

本仓库为比赛/研究项目，第三方数据的许可各自独立（见 `licenses/`）。引用本系统产出的结论时，请同时核对原始文献；不要把它当作临床决策依据。
