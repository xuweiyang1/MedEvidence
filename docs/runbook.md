# 运行与排错

## 1. 安装

```powershell
python -m pip install -r requirements.txt        # Agent + 检索
python -m pip install -r requirements-web.txt    # 仅网页需要
```

要求 Python 3.10+。`FlagEmbedding` 会安装 PyTorch；若本机已有 BGE-M3 推理环境，可只装 `qdrant-client`
并用 `MEDEVIDENCE_RETRIEVER=hook` 接入自己的检索函数（见 `my_retrieval_bridge.example.py`）。

## 2. 配置 `.env.agent`

```powershell
Copy-Item .env.agent.example .env.agent
notepad .env.agent
```

必须项：

```ini
DASHSCOPE_API_KEY=你的百炼Key
MEDEVIDENCE_QDRANT_PATH=E:/path/to/index_versions/v3_2_trial_clean/qdrant
MEDEVIDENCE_BM25_DB=E:/path/to/index_versions/v3_2_trial_clean/bm25.sqlite3
```

注意：

- `QDRANT_PATH` 填**包含 `meta.json` 的目录**，不是 collection 名，也不是 sqlite 文件；
- 路径含空格时不要加引号以外的转义，脚本按原样读取；
- `.env.agent` 不要提交，也不要在截图/日志里暴露 Key。

## 3. 启动前自检

```powershell
python scripts/run_agent.py --self-test       # 离线测试
python scripts/run_agent.py --sdk-check       # SDK 组件图
python scripts/run_agent.py --retriever-check # Qdrant + FTS5
python scripts/run_agent.py --online-check    # PubMed 连通性（不调用百炼）
python scripts/run_agent.py --probe           # 扫描本地索引候选
```

都通过再提问，能省掉大部分"跑不起来"的排查。

### 启用在线摘要检索

在 `.env.agent` 增加：

```ini
MEDEVIDENCE_ONLINE_ENABLED=1
MEDEVIDENCE_ONLINE_SOURCES=pubmed        # 或 pubmed,europepmc
MEDEVIDENCE_ONLINE_MAX_RESULTS=5
MEDEVIDENCE_ONLINE_MAX_ROUNDS=2
MEDEVIDENCE_CONTACT_EMAIL=you@example.com
# NCBI_API_KEY=...                       # 可选，3/s -> 10/s
```

- 只在本地证据明显不足时触发；在线结果会标注“摘要”，并进入同一套引用核验。
- 宽泛/总览问题可走 `search_online_guidelines`（自动加 `guideline[pt] OR review[pt]`）。
- 查询必须是英文；包含中文会被拒绝且不消耗预算。
- 查询会发送到 NCBI/EBI；缓存位于 `data/online_cache/`，可离线复现。
- 关闭：改回 `MEDEVIDENCE_ONLINE_ENABLED=0`。

## 4. 命令行提问

```powershell
python scripts/run_agent.py --question "问题"
python scripts/run_agent.py --question "问题" --json
python scripts/run_agent.py --trace-diagnose   # 读取最近一次轨迹的脱敏摘要
```

`--trace-diagnose` 不调用 API，能看到每轮命中数、覆盖事件、guardrail 与错误类型。

## 5. 网页

```powershell
python -m streamlit run web_app/app.py
```

或双击根目录的 `run_web.bat`（Windows）。默认地址 <http://127.0.0.1:8501>，只监听本机。

- 首次提问会加载 BGE-M3，通常明显慢于后续；
- 常驻引擎（默认）复用同一检索器与编码器；设置 `$env:MEDEVIDENCE_WEB_ENGINE="cli"` 可回退为每题子进程；
- 历史记录在 `web_app/data/history.sqlite3`，删除该文件即清空历史；
- 后台单 worker 串行执行，多个问题会排队。

## 6. 常见问题

| 现象 | 排查 |
| --- | --- |
| `Qdrant 打开失败（是否被其他进程锁定？）` | 嵌入式 Qdrant 只允许一个进程。关掉其他网页/Agent/Python 会话，确认没有残留 `python.exe` |
| `collection ... 不存在` | `QDRANT_PATH` 指错层级；`--probe` 会列出候选目录 |
| `SQLite 中未发现 FTS5 虚拟表` | `BM25_DB` 不是本项目的 `bm25.sqlite3`，或路径指向了别的库 |
| 401 / 403 | Key 无效或无该模型权限；到百炼控制台确认模型名与业务空间 |
| 429 | 额度或并发限制；降低 `MAX_ROUNDS`/`TOP_K` 或稍后重试 |
| 第一次提问特别慢 | 模型加载 + 首次检索；常驻模式只慢一次 |
| 网页显示"运行失败" | 看 `reason_code` / `failure_stage`：网络、Guardrail、检索还是引用核验；不是证据不足 |
| 检索全部失败（`retrieval_failed`） | 确认索引可读、`FlagEmbedding` 可加载；若报 protobuf/`Descriptors cannot be created directly`，见下方 |
| 在线检索失败（`online_retrieval_failed`） | 用 `--online-check` 测 PubMed/EBI 连通性；检查代理、超时（默认 25s）与限速 |
| 在线检索很慢 | 每次请求约 10–30 秒；减少 `ONLINE_MAX_RESULTS` / `ONLINE_MAX_ROUNDS`，缓存命中会快很多 |
| 8501 被占用 | 关闭旧进程，或 `--server.port 8502` |
| CUDA 报错 / OOM | 改回 `MEDEVIDENCE_DEVICE=cpu`，不要改索引 |

### 关于 `error` 与 `insufficient_evidence`

结果状态已经把两类情况分开：

- `error`：本次运行没有完成。`reason_code` 为
  `agent_runner_error`（网络/SDK）、`guardrail_rejected`（输出结构或引用不合法）、
  `verification_failed`（引用核验调用异常）、`invalid_output`（未按契约返回 JSON）、
  `retrieval_failed`（所有本地检索调用都失败）、`coverage_failed`（覆盖评估不可用）或
  `online_retrieval_failed`（在线检索失败且没有在线成功结果）。
- `insufficient_evidence`：运行完成，但没有通过核验的主张——这才是证据层面的结论。

排查顺序：看 `reason_code` → `--trace-diagnose` → 检查网络与 Key → 再怀疑检索。
网页对 `error` 显示红色"运行失败"标签，不会显示成"证据不足"。

### protobuf / TensorFlow 冲突

若日志出现 `TypeError: Descriptors cannot be created directly`，说明 Transformers 试图导入
TensorFlow，而 TF 自带的 `_pb2` 与 protobuf ≥ 4 不兼容。本项目不使用 TF/Flax，
`agent/__init__.py` 会在任何库导入前强制 `USE_TF=0`、`USE_FLAX=0`。
手工运行脚本时不要覆盖这两个变量为 1。

## 7. 数据与索引

重建索引见 [data_pipeline.md](data_pipeline.md)。索引目录必须包含 `dense_manifest.json` 与
`bm25_manifest.json`；两者记录的块哈希应与 `retrieval_chunks.jsonl` 一致。

## 8. 日志与隐私

- Agent 轨迹：`outputs/agent_runs_v030/*.json`（已忽略，不入库）；
- 默认只记录哈希与结构，不记录问题原文；
- 网页历史：`web_app/data/history.sqlite3`（已忽略）；
- 需要彻底重置：停止服务，删除 `web_app/data/` 与 `outputs/agent_runs_v030/`。
