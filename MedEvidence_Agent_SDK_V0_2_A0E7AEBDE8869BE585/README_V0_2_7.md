# MedEvidence Agent SDK V0.2.7 — SQLite 跨线程修复

## 已验证的根因

V0.2.6 在主线程构造 `LocalQdrantRetriever` 时创建了 `sqlite3.Connection`，但 SDK 工具 `search_medical_evidence`、`lookup_medical_document` 及预加载通过 `asyncio.to_thread()` 在工作线程执行。默认的 SQLite 连接不允许从其他线程访问，出现 `ProgrammingError`；后续检索则抛出包装后的 `RetrievalError`。**这并不表示论文未收录。**

本补丁只修改 `agent/retrieval_tools.py`，新增工作线程诊断及测试：

- 以 `check_same_thread=False` 打开 **`mode=ro` 只读** SQLite（同时保留 `PRAGMA query_only = ON`）。
- 加入 `threading.RLock`，对包括标题检索、FTS5、Dense 关联及搜索在内的共享连接与 Qdrant 客户端访问进行串行化；避免 SDK 并发工具调用共用 SQLite 游标。
- 不修改 API 配置、Guardrail、文献 ID 或索引内容；不取消引用白名单及证据充分性判断。

**注意**：`check_same_thread=False` 单独使用不能确保并发安全，因此必须保留上述 `RLock`。本补丁面向当前单进程 Agent CLI；若日后采用多进程/Web 并发部署，推荐每个请求独立创建检索器或采用服务端 Qdrant 与连接池。

## 安装（保持独立 Agent 文件夹）

在 `E:\Download\Agent\MedEvidence\MedEvidence_Agent_SDK_V0_2_A0E7AEBDE8869BE585` 下执行：

```powershell
# 可选：保存原文件备份
Copy-Item .\agent\retrieval_tools.py .\agent\retrieval_tools.py.v026.bak

# 注意改成 ZIP 实际保存的路径，且要解压到当前目录
$zip = Get-ChildItem 'E:\Download',"$HOME\Downloads" -Filter '*V0_2_7*.zip' -File -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $zip) { throw '没有找到 V0_2_7 ZIP，请检查下载位置' }
Expand-Archive -LiteralPath $zip.FullName -DestinationPath . -Force
```

然后依次运行：

```powershell
python scripts\run_agent.py --self-test
python scripts\check_threaded_retriever.py
```

期待 `43 tests ... OK`、`"threaded_retrieval_ready": true`、`lookup_hit_count` 大于零。第二条命令从**工作线程**调用标题检索及 BM25，**不会调用百炼，也不会加载 BGE-M3**。

如果想验证 SDK 工作线程里 Dense 也能运行（可能尝试连接 Hugging Face）：

```powershell
python scripts\check_threaded_retriever.py --with-dense
```

只有这两项测试成功，再运行实际 Agent：

```powershell
python scripts\run_agent.py --question "根据Real-world effectiveness of dupilumab in COPD这篇研究，作者指出了哪些研究局限性？请提供文献证据。"
python scripts\run_agent.py --trace-diagnose
```

真实 Agent 会调用百炼 API，可能计费。`--trace-diagnose` 应看到成功的检索、登记的证据 ID 和覆盖评估；即使仍因证据校验拒答，也不应再出现 SQLite `ProgrammingError`。

## 验证范围与局限

已在模拟 Qdrant 和真实临时 SQLite 上完成 **43 项离线测试**，包含线程池并发、`asyncio.to_thread()`、只读模式、真实 `chunk_id` 映射。**尚未访问你的本机 Qdrant 和通义千问 API**；实际 Agent 问答成功与否以你的运行轨迹为准。
