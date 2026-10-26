# MedEvidence Agent SDK V0.2：安装、排错与真实运行

> **重点修复**：V0.1 如果被 Windows 解压器解压到额外一层 `MedEvidence_Agent_V0_1_xxx/`，就会导致 `.env.agent.example` 找不到，`--probe` 只扫描子目录。本版提供安装脚本、自动项目根目录识别，以及真正使用 **OpenAI Agents SDK** 的运行模式。**不修改你原有的索引文件。**

## 1. 推荐安装方式（PowerShell）

将压缩包下载到任意目录。请在 PowerShell 里先进入真正的项目目录：

```powershell
conda activate xwy
cd E:\Download\Agent\MedEvidence
# 把这个路径改成你实际下载位置。-DestinationPath . 不会额外创建同名文件夹
Expand-Archive -LiteralPath "$HOME\Downloads\MedEvidence_Agent_SDK_V0_2_根目录覆盖包.zip" -DestinationPath . -Force
```

如果习惯 Windows 文件管理器右键解压，并且生成了**额外一层**文件夹，也没关系：进入那个内层文件夹，运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\INSTALL_AGENT.ps1 -ProjectRoot "E:\Download\Agent\MedEvidence"
```

脚本会**只复制这个 Agent 包提供的文件**到外层项目。若目标文件存在，会先备份到 `outputs/agent_backups/时间戳/`；不会覆盖已有 `.env.agent`，也不会触碰 Qdrant 索引和文献数据库。

最终应在 `E:\Download\Agent\MedEvidence` 下直接看到 `agent/`、`scripts/run_agent.py`、`requirements-agent-sdk.txt`、`.env.agent.example` 等文件。不要在内层文件夹执行旧脚本。

## 2. 不需要任何 API 的初步排查

```powershell
cd E:\Download\Agent\MedEvidence
python scripts\run_agent.py --self-test
python scripts\run_agent.py --probe
```

离线自测只检验流程实现、目录识别、引用结构与模拟检索，不代表 SDK 联网调用或真实医学表现已验证。

新版 `--probe` 输出 `code_root`、`project_root`、`project_root_detected`、`qdrant_candidates`、`sqlite_candidates`。如果扫描结果仍然为空，说明**这些常见本地格式未在扫描范围内被识别**，不意味着之前验证过的 `v3_2_trial_clean` 索引失效。

你可以用以下方式找已有索引的实际路径：

```powershell
Get-ChildItem . -Recurse -File -Filter meta.json -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
Get-ChildItem . -Recurse -File -Filter '*.sqlite' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
Select-String -Path scripts\validate_index_version_v1c2.py -Pattern 'qdir|QdrantClient|qdrant|BM25' -Context 2,2
```

请不要根据脚本中的示例路径猜测索引目录；以现有 `validate_index_version_v1c2.py` 对 `QdrantClient(path=...)` 的真实目录为准。存储目录一般包含 `meta.json`。如果索引位于项目之外，直接在 `.env.agent` 中填绝对路径。不要重新建索引。

## 3. SDK 环境与配置

```powershell
python -m pip install -r requirements-agent-sdk.txt
python scripts\run_agent.py --sdk-check
Copy-Item .env.agent.example .env.agent   # 如果安装脚本已自动创建，这一步不用重复
notepad .env.agent
```

关键配置：

```ini
DASHSCOPE_API_KEY=你的百炼APIKey
MEDEVIDENCE_MODEL=qwen-plus
MEDEVIDENCE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
MEDEVIDENCE_RETRIEVER=qdrant
MEDEVIDENCE_QDRANT_PATH=E:/Download/Agent/MedEvidence/你的真实Qdrant目录
MEDEVIDENCE_COLLECTION=bge_m3_v3_2_trial_clean
MEDEVIDENCE_DEVICE=cpu
MEDEVIDENCE_TOP_K=8
MEDEVIDENCE_MAX_ROUNDS=3
MEDEVIDENCE_SEMANTIC_VERIFY=1
```

`MEDEVIDENCE_QDRANT_PATH` 填 **存储根目录**，不是 collection 子目录，也不是 SQLite 数据库文件。`MEDEVIDENCE_BM25_DB` 只有实际是 SQLite FTS5 时才填；其他 BM25 格式请使用下面的 Hook 方式。代码**不会**重新嵌入索引或写入原始数据库，但本地 QdrantClient 可能对目录持有进程级锁，关闭占用它的索引进程后再运行。

若现有 Qdrant payload 缺少正文，或索引依赖单独的块内容存储，请编辑 `my_retrieval_bridge.example.py` 并复制成 `my_retrieval_bridge.py`，接入**已有检索函数**；设置：

```ini
MEDEVIDENCE_RETRIEVER=hook
MEDEVIDENCE_RETRIEVER_HOOK=my_retrieval_bridge:retrieve
```

其 `retrieve(query: str, top_k: int) -> list[dict]` 返回的每条结果必须包含真实稳定的 `chunk_id` 和非空 `text`，其余可包含 `title`、`pmcid`、`section` 等。示例桥接器不会生成假文献。必须先从你的真实项目代码确定检索函数接口，才能做最后一步连接。

## 4. 新版 SDK 与图片架构一一对应

| 图片中的组件 | 本项目真正实现的位置 | 行为 |
|---|---|---|
| **Runner + Agent Loop** | `agent/sdk_runtime.py` | `Runner.run(planner, question, ...)` 负责模型工具循环、handoff，限制 `max_turns` |
| **Agent** | `build_sdk_agents()` | `EvidencePlanner` + `EvidenceWriter` 两个真实 `Agent` 对象 |
| **Tools** | `@function_tool` | `search_medical_evidence`、`assess_evidence_coverage`、`list_retrieved_evidence`；检索次数硬上限 |
| **Models** | `OpenAIChatCompletionsModel` + `AsyncOpenAI` | 连接阿里云百炼 OpenAI 兼容接口，仍使用通义千问模型，不需要 OpenAI API Key |
| **Guardrails** | `@input_guardrail` + `@output_guardrail` | 检查输入长度、最终 JSON、引用白名单、是否完成证据覆盖检查 |
| **Handoffs** | `EvidencePlanner.handoffs=[EvidenceWriter]` | 真实 SDK handoff；如果绕过移交，就拒绝输出医学结论 |
| **Tracing** | `set_trace_processors([LocalMetadataTraceProcessor])` + `trace(...)` | SDK 追踪事件仅保存本地结构元数据，不使用默认 OpenAI 导出器 |
| **Run Artifacts** | `outputs/agent_runs/*.json` | 检索工具、证据覆盖、输出校验、真实块 ID 映射和结果；默认隐去题目和全文 |

**注意**：证据覆盖是启发式评分，不代表医学证据等级；语义支持性仍依赖 LLM，而非独立医学专家审核。官方 SDK 的 guardrails 检查程序输出格式和白名单，不能自动保证医学真实性。工具结果必须独立审查。

## 5. 真正运行医学问答

```powershell
python scripts\run_agent.py --question "高血压合并慢性肾病的降压药物选择有哪些循证依据？"
python scripts\run_agent.py --question "慢阻肺急性加重有哪些诊断证据？" --json
```

Agent 会先调用本地检索，再检查覆盖，必要时补检索，最后移交 EvidenceWriter。最终答案**仅由通过引用检查的原子医学主张**组成。证据不足、工具失败、SDK 终止、输出格式不合规或引用不受支持时，拒绝输出医学结论。

与旧版本对照（不使用 Agents SDK）：

```powershell
python scripts\run_agent.py --legacy --question "慢阻肺急性加重有哪些诊断证据？"
```

**请不要把 `--legacy` 的测试结果当作 SDK 测试结果。**

## 6. 状态声明与下一步

本包已经完成 Python 编译和 16 项离线测试，包括旧 V0.1 模拟检索测试、路径探测、引用白名单、追踪隐私等。**未验证**你真实的 `v3_2_trial_clean` 数据目录、SDK 安装后的完整运行、阿里云百炼工具调用和真实医学答案质量，因为当前环境无法访问你的 Windows 项目文件及百炼 Key，也无法下载 `openai-agents` 依赖。

请先贴：

```powershell
python scripts\run_agent.py --probe
python scripts\run_agent.py --sdk-check
```

若 `--probe` 仍为空，请再贴 `Select-String ... validate_index_version_v1c2.py ...` 的结果（**不要贴 API Key**）。即可根据你的真实路径和接口做最终联调，而不是重做 RAG。
