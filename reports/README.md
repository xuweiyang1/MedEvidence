# reports/ 说明

这里的文件是**构建与评测过程的可读产物**，由 `scripts/` 下的脚本生成，不是人工金标准。
大体积逐条 CSV 保留是为了可追溯；日常阅读优先看 `.md` / `summary.json`。

## 语料构建

| 文件 | 内容 |
| --- | --- |
| `pmc_parse_report.csv` | JATS 解析结果与失败篇目 |
| `evidence_chunk_report.csv` | 切块统计 |
| `hierarchical_chunk_v3_all.csv` / `v3_1_all.csv` | v3 / v3.1 分层切块明细 |
| `pmc_media_report.csv` / `_v2.csv`、`pmc_image_download_report_v2.csv` | 图表恢复与下载 |
| `mesh_do_mapping_report.csv` | MeSH / Disease Ontology 映射 |
| `v1c1_validation.json` | 索引版本校验结果 |

## 检索评测

| 文件 | 内容 |
| --- | --- |
| `dense_v3_2_test_queries.csv` / `dense_v3_2_test_results.csv` | 稠密检索查询集与逐条结果 |
| `bm25_v1_1_search_results.json` | BM25 逐查询结果 |
| `dense_v1_build_summary.json`、`dense_v1_search_summary.json` | 构建与检索摘要 |
| `bge_m3_smoke_v1_1_summary.json`、`bge_m3_performance_benchmark_v1.json` | 嵌入模型冒烟与性能基线 |

## 标注与复核

| 文件 | 内容 |
| --- | --- |
| `pilot_annotation_24.csv` | 24 条试点问题–论文对 |
| `pilot_annotation_24_human_review.csv` | 已保存的人工复核行（不是补齐的 24 条） |
| `pilot24_quality_report.md` / `.csv` | 证据卡自动检查报告（引用字面核验） |
| `evidence_cards.jsonl` | Qwen 生成的证据卡（含引文与来源 ID） |
| `retrieval_union_*.csv`、`dense_v1_review_candidates.csv`、`bm25_v1_1_review_candidates.csv` | 候选池与盲评材料 |
| `retrieval_eval_template.csv`、`retrieval_eval_v2_template.csv` | 评测表模板 |

> 证据卡的标签是**AI 建议**，不是 Gold。引用字面命中不代表医学结论成立；请结合
> `docs/evaluation.md` 与 `pilot24_quality_report.md` 的说明解读。

## 定向审计与实验

| 文件 | 内容 |
| --- | --- |
| `targeted_5q_audit/` | 5 个定向问题的候选池、诊断 JSON 与临时 qrels（`assistant_provisional`） |
| `v041_gapaware_15cases_summary.json` | V0.4.1 三臂对照的 15 case 汇总（代理指标，非临床验证） |

## 重新生成

```powershell
python scripts/generate_evidence_cards.py --context-only   # 只导出上下文预览，不调用 API
python scripts/audit_evidence_cards.py                     # 重新生成 pilot 质量报告
python scripts/retrieval_smoke_test.py                     # 检索冒烟
```

生成时间、候选文件与数据库路径会写入报告头部。更新数据后不要手工改历史报告，重新运行脚本即可。
