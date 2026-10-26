# scripts/ 导航

脚本很多，按用途分组。所有脚本都以仓库根为工作目录，参数用 `--help` 查看。
数据类脚本默认写入 `data/`，该目录不入库。

## 入口

| 脚本 | 用途 |
| --- | --- |
| `run_agent.py` | Agent CLI：`--self-test`、`--sdk-check`、`--retriever-check`、`--retrieval-smoke`、`--trace-diagnose`、`--question`、`--legacy` |
| `compare_adaptive_policies.py` | 离线重放三种检索策略，比较决策行为（不调 API） |
| `check_threaded_retriever.py` | 验证多线程下 SQLite/FTS 的只读访问 |
| `index_coordinator.py` | 索引版本编排：`plan` / `build` / `verify` |
| `verify_data.py` | 数据完整性校验（迁移或误删后使用） |

## 数据获取

`download_pmc_oa.py`、`download_mesh_do.py`、`download_who.py`、`download_cdc.py`、
`download_cdc_pages.py`、`download_pmc_figure_assets_v1.py` / `v2.py`、`who_iris.py`、
`cdc_syndication.py`、`fetch_pubmed_meta.py`、`metadata_lookup.py`、`diagnose_network.py`。

- 只走官方渠道；逐篇许可与哈希写入 `licenses/registry.csv` 与 `data/raw/pmc/manifest.csv`。
- 下载可断点续跑，已入库的 PMCID 自动跳过。

## 许可与纳排

`license_gate_v2.py`、`license_gate_v3_reconcile.py`、`diagnose_license_issues.py`、
`filter_trial_corpus_v1b1.py`、`audit_corpus_v1.py`、`audit_corpus_fts_coverage.py`、
`check_corpus_coverage.py`。

## 解析与切块

`extract_pmc_text.py`、`parse_pmc_jats_v1.py`、`recover_pmc_tables_figures_v1.py` / `v2.py`、
`build_hierarchical_chunks_v3_pilot.py`、`build_hierarchical_chunks_v3_1.py`、
`verify_hierarchical_chunks_v3.py` / `v3_1.py`、`verify_corpus.py`、`inspect_imported_chunks.py`。

> v3 是试验版，**现行语料使用 v3.1**（`build_hierarchical_chunks_v3_1.py`）。

## 知识库与索引

`knowledge_import_core.py`、`prepare_corpus_v1b.py`、`snapshot_core_v1c1.py`、
`build_corpus_snapshot.py`、`v1c2_common.py`、`build_bm25_v1b.py`、`build_bm25_version.py`、
`build_dense_version.py`、`validate_index_version.py`、`validate_index_version_v1c2.py`。

> 现行索引版本为 `v3_2_trial_clean`；`v1c2` 是索引清单 schema 版本，不是语料版本。

## 检索与评测

`medevidence_bm25_v1.py` / `v1_1.py`、`evaluate_bm25_v1_1.py`、`medevidence_dense_v1.py` / `v1b.py`、
`medevidence_bge_m3_smoke_v1_1.py`、`bge_m3_performance_benchmark_v1.py`、`retrieval_smoke_test.py`、
`hydrate_retrieval_results.py`、`prepare_retrieval_review_v1.py`、
`prepare_blinded_union_review_v1.py` / `v1_1.py`、`medevidence_coverage_probe.py` /
`medevidence_coverage_probe_strict.py`、`test_medevidence_coverage_probe_strict.py`。

## 标注与复核

`prepare_annotation_pilot_v1.py`、`evidence_review_core.py`、`generate_evidence_cards.py`、
`audit_evidence_cards.py`、`verify_evidence_quotes.py`、`review_app.py`、`run_review_app.py`、
`medevidence_auto_judge.py` / `v1_1.py` / `v12.py`、`medevidence_finalize_weak_labels.py`、
`medevidence_prepare_qrels.py`、`evaluate_provisional_qrels_v1.py`。

> 这些脚本产出的是**辅助标注/弱标签**，不是金标准。复核结论需人工确认。

## 可选 GUI 与遗留

`knowledge_manager_app.py`、`run_knowledge_manager.py`、`knowledge_trial_app_v1b.py`、
`run_knowledge_trial_v1b.py` 是早期知识库管理/试用界面；`build_evidence_chunks_v1.py` 等 v1 脚本
仅用于复现旧结果。日常使用以 `run_agent.py` 与 `web_app/` 为准。
