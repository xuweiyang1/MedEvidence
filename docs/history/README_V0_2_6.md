# MedEvidence Agent SDK V0.2.6 — 指定论文检索及错误“未收录”判断修复

本补丁用于你当前单独放置的 `MedEvidence_Agent_SDK_V0_2_*` 文件夹，基于 V0.2.4 的检索适配与 V0.2.2 的 SDK 流程，不改变原有 RAG 数据。

## 为什么需要修复

本地 `bm25.sqlite3` 已经有：

- 论文：`Real-world effectiveness of dupilumab in COPD: a German multicenter analysis`
- Chunk：`PMC13640210:parent:body:s0010:child:0010`，内容开头为 `Several limitations should be acknowledged...`

但原有通用 BM25 查询将大量词语用 OR 连接，加上 Dense 检索的主题相近文献，导致模型并未收到这一段，错误地将“没有命中”说成“可能未发表或未收录”。这不是证据不充分的有效判断。

## 本次补丁

- `agent/retrieval_tools.py`：新增 `lookup_document(title, top_k, focus)`。从现有同版本 `bm25.sqlite3` 的 `chunks.title` 精确定位对应文献，按问题侧重点对真实 Chunk 排序，返回真实 `chunk_id` 与正文。识别明确写出的中英文论文标题，遇到特定论文的问题时优先使用标题定位；一般医学问题继续使用原有混合检索。未知标题返回空，不捏造来源。
- `agent/sdk_runtime.py`：新增 OpenAI Agents SDK 工具 `lookup_medical_document`。在启动 SDK Runner 前，对于明确指出论文标题的问题，做一次只读的精确定位与证据预登记，避免 Planner 查询改写时丢失论文标识；Planner/Writer 可以查阅并引用已登记证据。明确禁止将“检索未命中”误说成“论文未收录/未发表”。仍保留证据充分性评估、引用白名单、语义校验与安全拒答。
- `scripts/run_agent.py`：新增 `--document-lookup`，**不调用百炼 API、不加载 BGE-M3**，仅返回论文标题、真实 chunk_id、正文长度以及是否包含局限性用语（不会输出正文）。
- `tests/test_agent_sdk_v026_named_title.py`：新增 7 项离线回归测试。
- `tests/test_agent_sdk_v021_patch.py`：更新旧测试对工具数量的硬编码断言，改为检查新 SDK 工具集合。

**不更改** `.env.agent`、Qdrant、SQLite 或任何文献快照，不降级 Guardrail、不改变模型参数或本地向量索引。

## 安装

在目前含 `agent/`、`scripts/` 和 `.env.agent` 的 **Agent 独立文件夹** 中运行：

```powershell
Copy-Item .\agent\retrieval_tools.py .\agent\retrieval_tools.py.before_v026.bak
Copy-Item .\agent\sdk_runtime.py .\agent\sdk_runtime.py.before_v026.bak
Copy-Item .\scripts\run_agent.py .\scripts\run_agent.py.before_v026.bak

# 把 ZIP 放到 E:\Download 后运行；也可替换 ZIP 的实际路径。
$zip = Get-ChildItem 'E:\Download\*V0_2_6*.zip' -File | Select-Object -First 1
if ($null -eq $zip) { throw '没有找到 V0.2.6 ZIP，请先确认下载路径' }
Expand-Archive -LiteralPath $zip.FullName -DestinationPath . -Force
```

> ZIP 根部直接是 `agent/`、`scripts/`、`tests/` 等，不会额外套一层目录。不要从上级 MedEvidence 根目录执行解压。

## 验证（不调用百炼 API）

```powershell
python scripts\run_agent.py --self-test
python scripts\run_agent.py --sdk-check
python scripts\run_agent.py --document-lookup "Real-world effectiveness of dupilumab in COPD"
```

预期：离线共 **38 项测试**通过，`sdk_ready: true`；最后一条返回 `document_lookup_ready: true`、`hit_count > 0`，且结果中有 `PMC13640210:parent:body:s0010:child:0010` 或该论文其他有效 Chunk。局限性段落通常会排在前面；实际结果以本机为准。

**注意**：V0.2.6 的 38 项测试使用内存 SQLite 和模拟 Qdrant；此处没有你的本机知识库与百炼连接，无法承诺真实结果已通过。

## 完整 Agent 问答（会产生百炼 API 费用）

仅在 `--document-lookup` 找到论文后执行：

```powershell
python scripts\run_agent.py --question "根据Real-world effectiveness of dupilumab in COPD这篇研究，作者指出了哪些研究局限性？请提供文献证据。"
```

如果仍 `insufficient_evidence`：

```powershell
python scripts\run_agent.py --trace-diagnose
```

报告诊断结果即可。**文献能够定位不等于每一条医学主张都得到支持**，如果引用语义核验失败，仍应安全拒答，并报告真实失败环节，而不是编造理由称文献不存在。
