# MedEvidence Phase 1B：BGE-M3 Dense Baseline 接入说明

> 适用当前项目：Windows PowerShell、Conda 环境 `(xwy)`、RTX 5060 8GB、BGE-M3、Qdrant 本地模式。此套脚本基于你提供的目录和 BM25 CSV 字段设计，但尚未拿到 `retrieval_chunks.jsonl` 的真实单条 JSON；请务必先执行 `inspect`，不要跳过。

## 1. 安装位置

把压缩包里的以下两个文件复制到：`E:\Download\Agent\MedEvidence\scripts\`

- `medevidence_dense_v1.py`（新脚本，支持 inspect/build/search）
- `prepare_blinded_union_review_v1_1.py`（修复版合并器，不覆盖原来的 v1）

在项目根目录执行以下命令。检查当前解释器和依赖（不需要重装已存在的包）：

```powershell
python -c "from FlagEmbedding import BGEM3FlagModel; from qdrant_client import QdrantClient; import torch; print('CUDA:', torch.cuda.is_available())"
```

如果依赖不存在，先确认 `(xwy)` 环境正确，再依据报错安装缺失的包。**不要在已经能运行的环境里无理由升级整个依赖栈。**

## 2. 先只检查 V3.1 Chunk 字段（没有建库、不会下载模型）

```powershell
python scripts\medevidence_dense_v1.py inspect --rows 3
```

确认输出的三个字段符合语义：`chunk_id` 是唯一分块 ID、`document_id` 是 PMC 论文 ID、`text` 是用于向量化的有效证据文本。若报字段缺失，**停止，不要建库**，提供报错里列出的原始字段名或脱敏后的 JSON 样例以调整脚本。不要为了绕过检查直接跳过数据行。

## 3. 建立完整 Dense 索引

```powershell
python scripts\medevidence_dense_v1.py build --expected-count 42526
```

这是基于你此前 benchmark 报告 `full_corpus_size=42526` 的校验值。如果源文件中真实 chunk 数不同，脚本会安全停止；应先查清原因再决定是否修改 `--expected-count`。

默认参数：BGE-M3 (`BAAI/bge-m3`)、CUDA、FP16、`batch_size=2`、`max_length=768`、1024 维 Cosine、每 128 个 chunks 提交一次 Qdrant。新索引的路径是：

`data\processed\index\qdrant_bge_m3_dense_v1`

不修改以前的：`qdrant_bge_m3_smoke_v1_1`。若同一目录已有不完整索引，会拒绝直接覆盖；**只有确认可以覆盖 Dense v1 索引时**才使用 `build --expected-count 42526 --reset`。如果旧脚本正在打开这个 Qdrant 本地路径，请先关闭以免锁冲突。

编译、向量计算及 Qdrant 写入都需要时间。此前 benchmark 的约 17 分钟只是计算外推，不是这里实测的完整入库时长。

## 4. 先用两个查询做端到端测试

```powershell
python scripts\medevidence_dense_v1.py search --limit-queries 2 --topk 10
```

检查数据：

```powershell
Import-Csv .\reports\dense_v1_review_candidates.csv | Select-Object -First 3 | Format-List *
```

应包含 `query_id,query_type,query,paper_rank,document_id,title,best_chunk_id,chunk_excerpt,dense_score` 等列。成功后执行全量查询：

```powershell
python scripts\medevidence_dense_v1.py search --topk 10
```

该脚本默认从已有 `reports\bm25_v1_1_review_candidates.csv` 提取唯一问题 ID/问题文本，按问题搜索 Dense Top-10 **不同论文**，并保存候选 CSV。**注意：如果 BM25 没命中某问题，这个 CSV 可能根本没有那个问题！正式定量评估需提供全部问题的统一查询清单，届时通过 `--queries 你的文件.csv` 传入。**

Qdrant 先检索默认前 300 个 chunk，再去重到 10 篇论文。若某问题不足 10 篇，脚本会在 `reports/dense_v1_search_summary.json` 中记录，并提示你增大 `--fetch-chunks`。

## 5. 合并 BM25 与 Dense 候选生成盲评表

```powershell
python scripts\prepare_blinded_union_review_v1_1.py --dense reports\dense_v1_review_candidates.csv
```

它会读取已有的 `reports\bm25_v1_1_review_candidates.csv`，输出：

- `reports\retrieval_union_blinded_v1.csv`：隐藏 BM25/Dense 来源的人工盲评表（附证据片段文本）；
- `reports\retrieval_union_provenance_v1.csv`：检索来源映射，保存两个检索器各自排名、chunk ID。

为防止覆盖已填的人工标注，输出文件已存在时默认拒绝运行。确实要覆盖时先备份，然后加 `--overwrite`。

## 6. 研究上必须保留的边界

1. `dense_score` 与 BM25 的 `bm25_raw` **不能直接比较绝对值**；后续融合应使用可复现的归一化或 RRF 等策略。
2. 当前盲评表按 `question_id + document_id` 合并，评的是**论文级相关性**，不能直接等同 chunk 级证据充分性。保留了不同检索器的代表 chunk，以便人工审查。
3. 联合候选池通常是**不完整标注池**。人工标注以后，可报告 pooled judgment / judged-only 指标及标注覆盖度，但不能把未标注的全库论文全部当作不相关来宣称 Recall@10 真值。
4. 建立 Dense 与 BM25 正式对比前，要确认两者确实对应同一 V3.1 语料、相同的问题集合和一致的 Top-K 论文级定义。
5. 此版沿用已验证的 `max_length=768`；之前 5000 条样本有 1.1% 被截断。后续需检查长 Chunk 对检索的影响。

## 7. 常见错误

- `ModuleNotFoundError`：确认激活 `(xwy)`，检查已安装 FlagEmbedding / qdrant-client；
- `缺少必要字段`：发送 `inspect` 输出中的原始键，调整映射，不要跳过；
- `实际条数与 --expected-count 不符`：检查 V3.1 语料是否与 benchmark 所用版本一致；
- `local storage is already accessed by another instance`：关闭其他打开同一 Qdrant 路径的 Python 程序；
- `FileExistsError`：盲评文件已存在，先备份再考虑 `--overwrite`。
