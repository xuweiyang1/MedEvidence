# MedEvidence Agent SDK V0.2.4 — 检索正文关联修复

**适用**：你当前 `MedEvidence_Agent_SDK_V0_2_*` 独立子文件夹，已安装 V0.2.2，并且使用 `v3_2_trial_clean` Qdrant + BM25 SQLite。

## 为什么报错

经只读诊断：Qdrant 的 payload 含 `chunk_id`、`document_id`、hash 等元数据，但**不保存正文**；同版本 `bm25.sqlite3` 含

- `chunks(id, chunk_id, document_id, title, section_path, body_text, ...)`
- `fts_chunks` 虚拟表，使用 `content='chunks', content_rowid='id'`

旧版错误地期待 Qdrant payload 中直接包含 `text/content/chunk_text`，因此命中后不能取出正文。

## 本次只改 2 个代码文件

- `agent/retrieval_tools.py`：Dense 命中后精确使用 `chunk_id` 查询 SQLite `chunks.body_text`；BM25 使用 `fts_chunks.rowid = chunks.id` 关联真实文献；Dense 和 BM25 使用统一 Chunk ID 进行 RRF 融合。
- `tests/test_agent_sdk_v024_join.py`：6 项完全离线的回归测试。

**不涉及** `.env.agent`、API Key、原有 Qdrant/SQLite 索引、文献快照、其他 Agent 模块。

## 安装（在现有 Agent 独立文件夹）

在 `(xwy)` PowerShell 中，先进入 **含有 `agent`、`scripts`、`.env.agent` 的子文件夹**：

```powershell
cd 'E:\Download\Agent\MedEvidence\MedEvidence_Agent_SDK_V0_2_A0E7AEBDE8869BE585'
Copy-Item .\agent\retrieval_tools.py .\agent\retrieval_tools.py.v022.bak -Force

# 请将 ZIP 路径改为实际下载位置
$zip = 'E:\Download\MedEvidence_Agent_SDK_V0_2_4_正文关联修复.zip'
Expand-Archive -LiteralPath $zip -DestinationPath . -Force
```

也可使用文件管理器把压缩包内的 `agent` 和 `tests` 两个目录合并到当前 Agent 子文件夹；不要增加新一层目录。

## 验证（不调用百炼，不产生成本）

```powershell
python scripts\run_agent.py --self-test
python scripts\run_agent.py --retriever-check
python scripts\run_agent.py --retrieval-smoke --question 'COPD exacerbation systemic corticosteroid duration 5 days'
```

期望：单元测试通过；`--retriever-check` 返回 `retriever_ready: true`；`--retrieval-smoke` 返回 `retrieval_ready: true` 且 `hit_count` 大于零，命中条目含真实 `chunk_id`、`text_length` 大于零。数值以你本机实际结果为准。

*如仍然遇到原先的 `protobuf Descriptors cannot be created directly`，在同一 PowerShell 进程执行：*

```powershell
$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION = 'python'
$env:USE_TF = '0'
```

这不是依赖升级；它只影响当前终端启动的 Python 进程。首次模型加载可能访问 Hugging Face，如缓存完整但网络不稳定，可选择：`$env:HF_HUB_OFFLINE='1'`（仅在模型文件均已缓存时使用）。

## 只有真实检索成功之后，才运行 Agent

```powershell
python scripts\run_agent.py --question 'COPD急性加重患者使用全身糖皮质激素的推荐疗程是多久？'
```

这一步使用通义千问 API，可能产生费用。若仍显示 `insufficient_evidence`，请执行 `python scripts\run_agent.py --trace-diagnose` 看最新失败原因。

## 注意

- 离线测试只能证明结构与算法逻辑，无法代替你本机 42,553 个真实 Chunk 的完整验证。
- 两个配置路径必须指向 **同一** `v3_2_trial_clean` 版本，不能混用 `v4_evidence_augmented_pilot3`。
- 如真实检索命中缺正文，适配器会报错，而不是伪造医学引用。
- Qdrant Local 对超过 20,000 点的警告是性能提示，不影响本补丁的数据关联逻辑。
- 这只是科研教学辅助；医学主张仍需要后续引用与证据校验，不能仅凭检索命中判定医学结论正确。
