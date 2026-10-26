# 架构与设计

本文说明 MedEvidence 的分层、请求生命周期、硬约束，以及几个刻意的"保守"设计。所有路径相对仓库根目录。

## 1. 分层

```text
展示层     web_app/            Streamlit 页面、会话历史、任务队列
控制层     agent/sdk_runtime.py OpenAI Agents SDK 的 Agent 图与运行循环
            agent/adaptive_policy.py  确定性的证据充分性策略
数据层     agent/retrieval_tools.py   Qdrant + SQLite FTS5（RRF 融合）
           agent/evidence_evaluator.py 子问题覆盖评估（LLM）
           agent/citation_verifier.py  主张级引用核验（LLM）
基础设施   agent/config.py / project_locator.py / llm_client.py
```

`agent/orchestrator.py`、`planner.py`、`answer_generator.py` 是 V0.1 的自写循环，仅用于
`python scripts/run_agent.py --legacy` 的对照基线，不在正式路径上。

## 2. 一次提问发生了什么

1. `scripts/run_agent.py` 解析 `.env.agent`，定位项目根（`scripts/index_coordinator.py` 为锚点），构造检索器。
2. `run_sdk_agent()` 建立 `SDKState`：证据表、来源→证据 ID 映射、查询去重集合、轮次计数、事件日志。
3. 若问题里出现明确的论文标题（`extract_explicit_title`），先做一次精确标题预取并登记证据，避免模型误判"论文不存在"。
4. SDK `Runner` 驱动 `EvidencePlanner`：
   - `search_medical_evidence`：混合检索，登记新块为 `E1..En`；
   - `assess_evidence_coverage`：把 2–5 个子问题交给 LLM 判断哪些被哪些证据覆盖；
   - `decide_next_evidence_action`：确定性策略给出下一步动作；
   - 重复直到预算耗尽或策略允许作答，然后 handoff 给 `EvidenceWriter`。
5. `EvidenceWriter` 只输出严格 JSON（`status` / `claims` / `limitations`），不写自由叙述。
6. 输出 guardrail 校验 JSON 结构、引用白名单、是否在最后一次检索后重新评估过覆盖；不通过即 tripwire。
7. `CitationVerifier` 对每条主张核验支持性，未通过的主张被剔除；全部通过且策略允许，才是 `answered`。
8. 结果、事件与脱敏轨迹写入 `outputs/agent_runs_v030/<run_id>.json`。

## 3. 为什么这些约束是"硬"的

| 约束 | 位置 | 作用 |
| --- | --- | --- |
| 检索轮次上限 | `config.max_rounds` + 工具内计数 | 防止无限补检与成本失控 |
| 证据块上限 | `config.max_evidence` | 控制上下文与费用 |
| 引用白名单 | `validate_draft()` | 拒绝任何未登记 ID，杜绝"编造参考文献" |
| 覆盖检查 | `guardrail_decision()` | 没有覆盖评估就不许输出结论 |
| 拒答合法 | `guardrail_decision()` | 允许 `insufficient_evidence` + 空 claims 的诚实拒答 |
| 引用语义核验 | `citation_verifier.py` | 文字存在 ≠ 支持结论，逐条判定 |
| 最终策略复核 | `run_sdk_agent()` 末尾 | 即使模型跳过决策工具，也按最终覆盖重算动作 |

## 4. 状态语义

| 状态 | 含义 | 不该被理解成 |
| --- | --- | --- |
| `answered` | 主张全部通过核验，且策略允许作答 | 医学结论一定正确 |
| `partial` | 有已核验主张，但覆盖或策略仍显示缺口 | 失败；它可能是有用的部分证据 |
| `insufficient_evidence` | 没有通过核验的主张，或覆盖为空 | 文献不存在；也可能是检索/网络问题 |
| `model_knowledge` | 仅网页通用模式：模型知识，无本地证据 | 循证结论 |

> 注意：当前实现中，Agent 运行异常（如模型 API 连接失败）也会落到
> `insufficient_evidence`，并在 `limitations` 里写明"Agent 执行未成功完成"。阅读结果时应先看限制条件。

## 5. 检索细节

- **稠密**：`LocalQdrantRetriever` 用 BGE-M3 编码查询（`FlagEmbedding`，缺省回退 `sentence-transformers`），Qdrant local 模式查询。
- **稀疏**：同一版本 `bm25.sqlite3` 的 FTS5 表，查询词做 `OR` 匹配，按 `bm25()` 排序；通过 `chunks.id` JOIN 保证 chunk_id 与稠密结果同源。
- **融合**：RRF，`score += 1/(60+rank)`，取前 `top_k`。
- **并发**：`_serialized_retrieval` 用 `RLock` 串行化 Qdrant 与 SQLite 访问；SQLite 以 `mode=ro` 打开并 `query_only=ON`。
- **标题定位**：`lookup_document()` 按真实文章标题匹配 parent/child 块，用于"这篇论文怎么说"类问题。

索引版本信息记录在索引目录的 `dense_manifest.json` / `bm25_manifest.json`（构建时生成，不入库）。

## 6. 配置解析

`Config.from_env(root, code_root)` 优先读取代码目录下的 `.env.agent`，否则读项目根的 `.env.agent`；
已存在的环境变量优先（`load_env` 使用 `os.environ.setdefault`）。所有数值都有范围钳制，例如
`top_k ∈ [1,40]`、`max_rounds ∈ [1,4]`。

## 7. 隐私与边界

- 默认 `MEDEVIDENCE_TRACE_CONTENT=0`：轨迹只保存问题/查询的哈希，不保存原文。
- 模型供应商仍会收到问题文本与检索片段；网页历史保存在 `web_app/data/history.sqlite3`（不入库）。
- SDK 的默认 OpenAI Tracing 被替换为本地元数据处理器，不向 OpenAI 上传。
- 单机嵌入式 Qdrant 有进程级锁：不要同时开两个 Agent 进程或两个网页实例。

## 8. 前端

`web_app/` 与核心解耦：它只通过 `web_app/bridge.py` 调用 `scripts/run_agent.py` 或常驻
`resident_runner.py`。`HistoryService` 用单线程 `ThreadPoolExecutor(max_workers=1)` 串行执行任务，
状态为 `queued → running → done/failed`，进程重启会把 `running` 恢复为 `queued`。

常驻模式（`MEDEVIDENCE_WEB_ENGINE=resident`，默认）复用同一个 Qdrant 连接与 BGE-M3 编码器，避免每题重载；
设置 `MEDEVIDENCE_WEB_ENGINE=cli` 可回退为每题子进程。

## 9. 已知的设计取舍

- 覆盖率阈值默认 1.0，宁可不答也不过度承诺，代价是 `partial` 偏多。
- 覆盖与核验都依赖同一个 LLM，存在同源偏差；没有独立第二模型或专家仲裁。
- 单 worker 牺牲并发换取索引安全；真正并行需要把 Qdrant 部署为服务端并做资源隔离。
