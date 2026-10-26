# MedEvidence v0.2.3 检索结构诊断（不会修改索引）

## 安装

将压缩包内的 `scripts/inspect_retrieval_schema.py` 解压覆盖到当前独立 Agent 文件夹；不会碰你已有的 `scripts/run_agent.py`、`.env.agent` 或任何已有代码。

## 执行

在 Agent 文件夹内执行：

```powershell
python scripts\inspect_retrieval_schema.py
```

输出展示 Qdrant payload 中的 **字段名、数据类型和字符串长度**（不含正文）；SQLite 表结构、检索用 ID 的示例、可能保存 Chunk 正文的文件名。不会加载 Embedding、不会请求 Hugging Face、不会调用百炼 API，不会修改索引。

将输出完整发给 ChatGPT，就能依据你的真实字段判断：

1. 正文是否在 Qdrant payload 里只是另一个字段名；
2. 是否在 BM25 SQLite 对应的 chunk 表中，可以通过 chunk_id 关联；
3. 是否在 `snapshot_chunks` 或其他 JSONL/Parquet 文件中，需要编写稳定 ID 关联读取器。

**注意：** 如果出现 `Qdrant ... already accessed by another instance`，结束其他正在使用同一本地 Qdrant 的进程后重试。此诊断不等于检索成功，暂时不要重复运行百炼付费问答。
