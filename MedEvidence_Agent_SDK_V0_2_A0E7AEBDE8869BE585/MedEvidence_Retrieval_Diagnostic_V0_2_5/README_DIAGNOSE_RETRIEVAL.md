# MedEvidence 检索质量只读诊断 V0.2.5

这个包只新增 `scripts/diagnose_retrieval_quality.py`，不会覆盖 Agent 现有代码，不修改 Qdrant/SQLite，不调用百炼 API，不输出正文或 API Key。它会加载本地 BGE-M3 查询向量模型，读取真实 Qdrant 和 FTS5 数据。

## 安装

在当前独立 Agent 代码文件夹解压 ZIP 到 `.`：

```powershell
Expand-Archive -LiteralPath "E:\Download\MedEvidence_Retrieval_Diagnostic_V0_2_5.zip" -DestinationPath . -Force
```

若 ZIP 在其他路径，请修改路径。

## 运行

```powershell
python scripts\diagnose_retrieval_quality.py --question "COPD acute exacerbation systemic corticosteroids prednisone 40 mg 5 days treatment duration" --question "AECOPD oral corticosteroid course duration short course versus long course" --top-k 10
```

输出会分开显示 Qdrant 原始 Dense 命中与 SQLite FTS5 命中，列出标题、真实 Chunk ID、原始 Dense 分数/FTS 排名分数、查询词命中情况，以及语料中 COPD/激素相关词命中的 FTS Chunk 数。不会输出证据正文。

## 如何判断

- 如果 FTS 词频几乎为 0，可能是当前语料不包含该主题或 FTS 索引内容未同步。
- 如果 FTS 词频不低，但 BM25 Top-K 没有激素疗程文献，优先检查 FTS 查询策略与索引词法。
- 如果语料有相关词，但 Dense Top-K 明显不相关，下一步应检查 embedding 模型/向量指纹、向量距离配置和原有检索器实现。
- `raw_score_or_rank_score` 在 Dense 部分是 Qdrant 原始分数，在 BM25 部分是当前适配器的倒数排名分数，二者不可直接比较。
