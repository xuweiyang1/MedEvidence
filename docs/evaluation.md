# 评测说明

本项目的评测分三层：**检索/切块**、**证据卡（引用字面核验）**、**端到端回答与门控**。
三层都不能单独证明"医学结论正确"，本文把每层能说明什么、不能说明什么写清楚。

## 1. 离线单元测试

```powershell
python scripts/run_agent.py --self-test                     # 75 项，无需 Key / 索引 / SDK 网络
python -m unittest discover -s web_app/tests -p "test_*.py" # 网页层：队列、双模式、历史迁移
```

测试覆盖：引用白名单、拒答路径、覆盖策略、重放不泄露未来轮次、历史库迁移、模式路由。
它们验证的是**代码契约**，不是医学正确性。

## 2. 检索与切块

产物在 `reports/`：

| 文件 | 内容 |
| --- | --- |
| `dense_v3_2_test_results.csv` | 稠密检索逐查询结果 |
| `dense_v3_2_test_queries.csv` | 测试查询集 |
| `bm25_v1_1_search_results.json` | BM25 逐查询结果 |
| `dense_v1_build_summary.json` / `bge_m3_performance_benchmark_v1.json` | 构建与性能基线 |
| `hierarchical_chunk_v3_1_all.csv` | 切块统计 |
| `pmc_parse_report.csv` / `pmc_image_download_report_v2.csv` | 解析与图表恢复质量 |

这些是**检索层指标**（命中、排序、覆盖），不涉及答案是否成立。

## 3. 证据卡试点（24 条问题–论文对）

`reports/pilot24_quality_report.md` 的自动检查结果：

- 24 张卡全部生成，0 缺失、0 格式无效；
- 65 条引文中 **64 条字面命中**、0 条部分匹配、**1 条未找到**；
- 10 张卡含至少一段被截断的来源，合计 17 段；
- 自动分级：1 条 HIGH（`QUOTE_NOT_FOUND`）、12 条 REVIEW、11 条仅通过字面检查。

**能说明**：引用文字确实来自指定来源，卡片结构可用。
**不能说明**：该引文支持结论、研究设计判断正确、或标签可当作 Gold。试点标签是 AI 建议，仍需人工复核与仲裁。

## 4. 端到端门控实验（V0.4.1，15 个 case）

`reports/v041_gapaware_15cases_summary.json` 比较三种证据注入策略（FIFO / 锚点多样化 / 缺口感知），
使用同一套检索暴露与严格门控，结果为 LLM 代理指标：

| 策略 | 第 1 轮覆盖 | 第 2 轮 | 第 3 轮 | 第 3 轮独立文献数 |
| --- | --- | --- | --- | --- |
| FIFO | 0.511 | 0.556 | 0.578 | 6.53 |
| 锚点多样化 | 0.511 | 0.556 | 0.600 | 7.20 |
| 缺口感知 | 0.511 | 0.511 | 0.600 | 7.27 |

摘要文件同时标注：

- `independent_clinical_validation: false` —— 没有独立临床金标准；
- `actual_shared_support_judge_api_calls: 107` —— 判定本身也消耗模型调用；
- `fairness_limit` —— 三臂共享同一检索轨迹，不是三条独立策略轨迹；
- `weak_qrels_provisional_only: true` —— 相关判断是临时弱标签。

**因此**：这些数字说明"改动是否改变了代理分数与文献多样性"，不能写成"准确率提升"。

## 5. 解读规则

1. 启发式覆盖率 ≠ 答案正确率；即使 100% 也可能因引用核验失败而拒答。
2. 引用字面存在 ≠ 语义支持；后者由 `citation_verifier.py` 判定，仍是模型判断。
3. AI 预标注 ≠ Gold；需要独立人工复核与仲裁。
4. `partial` 不等于失败，可能是"有证据但未覆盖全部子问题"的诚实表达。
5. 运行故障现在返回 `error`（带 `reason_code`），不再计入"证据不足"；统计拒答率时应把 `error` 排除。

## 6. 尚未做的

- 大规模、预先注册的 test set 与双人盲评；
- 与固定检索基线在同一查询、同一预算下的在线 A/B；
- 成本、时延与拒答率的联合报告；
- 外部临床专家对主张级支持性的独立审核。

这些是下一步研究计划，不在当前版本的能力声明内。
