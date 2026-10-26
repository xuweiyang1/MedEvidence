# MedEvidence Agent SDK V0.2.1 兼容性修复包

此压缩包是针对 **现有独立文件夹安装** 的增量补丁，不需要重建医学 RAG 索引，也不需要重新安装依赖。

## 修复内容

1. 修复 SDK `@function_tool` 注解解析报错 `NameError: name 'RunContextWrapper' is not defined`。在 SDK 工具定义模块使用运行时可解析的注解。
2. 支持 `.env.agent` 位于独立 Agent 子目录（优先），找不到时回退到外层 MedEvidence 项目根目录；不再要求用户移动文件。
3. `--sdk-check` 只有在实际构建 SDK Agent / FunctionTool / Guardrail / Handoff 图成功后才给出 `sdk_ready: true`。
4. 新增 `--retriever-check`：仅检查 Qdrant 集合、point 数和可选 SQLite FTS5（不调用 API、不加载 Embedding），以避免盲目开始收费模型调用。
5. 新增 2 项回归测试，总计 18 项离线测试。

## 安装步骤（PowerShell）

打开终端并进入 **已经存在的 Agent 子目录**：

```powershell
conda activate xwy
cd E:\Download\Agent\MedEvidence\MedEvidence_Agent_SDK_V0_2_A0E7AEBDE8869BE585

# 若 ZIP 已在 Downloads 文件夹：
Expand-Archive -LiteralPath "$HOME\Downloads\MedEvidence_Agent_SDK_V0_2_1_修复补丁.zip" -DestinationPath . -Force
```

安装后结构保持不变。确认 `agent/sdk_runtime.py`、`agent/config.py`、`scripts/run_agent.py` 已更新。

编辑同一目录下的 `.env.agent`，至少设置：

```dotenv
DASHSCOPE_API_KEY=填你自己密钥，不要提交到 Git
MEDEVIDENCE_RETRIEVER=qdrant
MEDEVIDENCE_QDRANT_PATH=E:/Download/Agent/MedEvidence/data/processed/index_versions/v3_2_trial_clean/qdrant
MEDEVIDENCE_COLLECTION=bge_m3_v3_2_trial_clean
MEDEVIDENCE_BM25_DB=E:/Download/Agent/MedEvidence/data/processed/index_versions/v3_2_trial_clean/bm25.sqlite3
```

顺序执行：

```powershell
python scripts\run_agent.py --self-test
python scripts\run_agent.py --sdk-check
python scripts\run_agent.py --retriever-check
```

预期：`sdk_ready: true`、`retriever_ready: true`，`point_count` > 0。只有都通过了，再运行：

```powershell
python scripts\run_agent.py --question "高血压合并慢性肾病患者选择降压药有哪些循证考虑？"
```

> 注意：本补丁已运行 18 项离线测试，并通过函数注解解析模拟回归测试；本制作环境没有安装 SDK 的权限或你的本地索引，**未进行真实百炼/Qdrant/SDK 联合调用**。需以你本机 `--sdk-check`、`--retriever-check` 的结果为准。
