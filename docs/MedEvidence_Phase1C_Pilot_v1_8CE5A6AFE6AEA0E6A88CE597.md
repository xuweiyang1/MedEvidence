# MedEvidence Phase 1C — Pilot v1 医学证据人工标注指南

> 版本：2026-10-14 · 数据：8 个问题，24 条试点候选（全量候选池为 178 个问题–论文对）

## 一、定位和使用限制

- 本指南依据用户提供的 24 条候选 CSV 的**问题、论文标题、证据预览**制定；没有逐篇核验原始全文，也未通过外部医学数据库核实研究注册、研究设计和最新指南。
- `MedEvidence_Pilot24_候选初审提示_非Gold.csv` 所有原有人工标注字段保持空白；`AI_preview_suggestion_NOT_GOLD` 是仅根据片段的**待核验建议**，不是 Gold Set 或第二名独立人工标注者的结果。
- 评价单位是 `(question_id, document_id)` 问题–论文对；`support_chunk_ids` 属于该论文可能支持答案的证据片段，不能把论文命中和片段直接支持混为一谈。
- 盲评过程不得查看检索方法、分数和排名；论文全文可以查阅，但不得据此推断方法来源。
- 人工发现预览截断、图表/OCR 失真时，必须回到原文；`UNCERTAIN` 不得映射为 `0`。

## 二、固定的标注标签

| 字段 | 值与判定 |
|---|---|
| `relevance_label` | **2**：候选论文的可核验证据直接满足此问题要求；**1**：仅间接相关/部分要求匹配/用于证据发现；**0**：与问题核心约束不符；**UNCERTAIN**：必须补充全文或关键信息才能判断。 |
| `evidence_directness` | `DIRECT`、`INDIRECT`、`BACKGROUND`、`UNCERTAIN`；根据当前 query_type 和必要研究设计解释，不能只按文章名称判定。 |
| `study_design` | `RCT`、`OBSERVATIONAL`、`SECONDARY_ANALYSIS`、`REVIEW`、`GUIDELINE`、`CASE_REPORT`、`ECONOMIC_MODEL`、`PROTOCOL`、`OTHER`、`UNKNOWN`；研究设计须看 Methods 或出版类型核验。 |
| `population_match` | `YES`、`PARTIAL`、`NO`、`UNCERTAIN`；不限定人群的问题可记录 `N/A`（在协议内统一）。 |
| `outcome_match` | `YES`、`PARTIAL`、`NO`、`UNCERTAIN`；要核对问题要求的结局/变量，而非只看主题关键词。 |
| `supports_answer_from_paper` | `YES`、`PARTIAL`、`NO`、`UNCERTAIN`；表示**这篇论文**能否支持问题的部分或全部，不表示整组文献能否回答。 |
| `reviewer_id` | 人工标注人员编号（R1/R2）；AI 的初审提示**不能**当作人工复核。 |
| `review_notes` | 记录理由、全文核验位置、Methods/表格编号、对照文献 ID、歧义。 |

**建议新增字段（正式版模板再加）**：`query_type`、`required_study_design`、`required_population`、`required_outcome`；全文核验记录（如 `fulltext_checked`, `verification_location`）；冲突仲裁状态 `adjudication_status`。这些属于问题属性或流程记录，不应混成模型预测标签。

## 三、八个问题的专属判定标准

### C001｜RCT 发现 / 药物与 eGFR 下降
**原问题**：Which published randomized trials investigate drug effects on eGFR decline?

- 设计/任务：发表的原始随机分配试验；药物干预；实际报告 eGFR 下降、斜率或相关预定肾脏终点
- 人群：人口以原始试验入排标准为准；问题未限定具体疾病
- 结局/变量：eGFR decline / slope（区分短期下降与长期下降）
- 特别注意：原始 RCT 符合要求可 2；二次分析或综述发现线索通常 1；仅提到 eGFR 未确认随机设计需待核验

### D001｜CKD 诊断 / 指南规范
**原问题**：How is chronic kidney disease diagnosed using eGFR and albuminuria?

- 设计/任务：能说明 CKD 诊断标准及慢性持续性要求；eGFR 与白蛋白尿的使用方式
- 人群：一般 CKD 人群；特定儿科标准需特别注明
- 结局/变量：诊断阈值、持续时间、白蛋白尿指标及适用条件
- 特别注意：只谈风险分层而不解释诊断通常 0/1；不要把分期等同于诊断

### G001｜基因变异关联
**原问题**：ABCC8 polymorphism and type 2 diabetes

- 设计/任务：明确 ABCC8 变异位点/基因型与 2 型糖尿病风险或表型的关联研究
- 人群：2 型糖尿病病例/对照或相关人群
- 结局/变量：ABCC8 polymorphism（位点）+ T2D 结局、效应方向/估计
- 特别注意：仅出现 diabetes 不算匹配；没有 ABCC8 的候选通常 0

### M001｜疾病机制解释
**原问题**：What mechanisms link chronic inflammation to insulin resistance?

- 设计/任务：解释慢性炎症到胰岛素抵抗的因果或可能信号路径（须区分假说与验证）
- 人群：不限疾病，但需关联有关组织/人群或生物学模型
- 结局/变量：炎症细胞因子、受体信号、组织代谢等明确机制
- 特别注意：仅并列出现 inflammation 和 insulin resistance 多为 1；直接机制描述可 2，但需标明原始/综述来源

### P001｜PICO 亚组对比
**原问题**：Do kidney outcomes of SGLT2 inhibitors differ between adults with and without type 2 diabetes?

- 设计/任务：SGLT2i 对肾结局，并比较有无 2 型糖尿病的成人人群
- 人群：成年人有 T2D vs 无 T2D；最好提供分层/交互分析
- 结局/变量：eGFR slope、肾衰竭/复合肾结局等
- 特别注意：只含 T2D 或只说心血管获益，不足以直接支持跨组比较

### R001｜检验公式 / 估算方法
**原问题**：How is eGFR estimated in chronic kidney disease?

- 设计/任务：说明用哪些检测值、哪个方程、单位与适用版本计算 eGFR
- 人群：慢性肾病评估语境；考虑年龄与人群适用条件
- 结局/变量：eGFR 公式及变量（肌酐、胱抑素 C 等）
- 特别注意：数学 OCR 乱码须全文核对；仅报告 eGFR 数值或纳入阈值通常 0

### S001｜药物安全性 / 试验不良事件
**原问题**：What adverse events were reported for SGLT2 inhibitors in chronic kidney disease trials?

- 设计/任务：CKD 人群的 SGLT2i 临床试验中实报 adverse events，并有事件类型/数量/比较
- 人群：CKD 临床试验人群；明确治疗组和对照组
- 结局/变量：不良事件类型、发生率和试验来源
- 特别注意：药物一般性介绍、个案或经济模型不能替代试验安全性数据

### T001｜临床实验室表格检索
**原问题**：creatinine and eGFR reference range in a clinical laboratory table

- 设计/任务：表格明确给出肌酐与 eGFR 的适用参考范围/参考阈值；区别患者值、IQR 与参考区间
- 人群：明确实验室受试者、年龄性别或测量方式（必要时）
- 结局/变量：肌酐参考区间 + eGFR 参考信息及单位
- 特别注意：只含其中一个指标可 1；患者 eGFR 中位数不是参考区间；此查询还需澄清参考范围定义

## 四、24 条片段初审——只供人工核验，不是最终标签

| 问题 | 文献 PMCID | 片段初审建议 | 理由摘要 |
|---|---|---|---|
| C001 | PMC13627872 | `UNCERTAIN` | 比较 SGLT2i 与安慰剂并报告 eGFR 变化，但片段没有证明随机分配或 eGFR 下降是否为试验预设结局 |
| C001 | PMC13641454 | `1` | 利用 CANVAS/CREDENCE/DAPA-CKD 试验样本开展 uEGF/eGFR 分析；有试验线索，但片段不能表明本文是原始随机试验主报告 |
| C001 | PMC13607204 | `1` | 综述表格列举 FIND-CKD 和 eGFR 结局，可用于发现试验，但不是该试验的原始报告 |
| D001 | PMC13601971 | `1` | 提到 eGFR 与白蛋白尿用于 CKD 分期，但没有给出诊断条件及慢性持续性要求 |
| D001 | PMC13612674 | `1` | 儿童 APOL1 研究使用 eGFR 阈值和白蛋白尿作病例定义/关联分析，不是通用 CKD 诊断说明 |
| D001 | PMC13625882 | `0` | 聚焦 CKD 进展风险和医疗成本，所给片段未说明如何依 eGFR/白蛋白尿诊断 CKD |
| G001 | PMC13627992 | `0` | 片段涉及 GZMK+ CD8 T 细胞、HCC/T2DM 假说，不涉及 ABCC8 变异关联 |
| G001 | PMC13630088 | `0` | 糖尿病分型研究方案，未涉及 ABCC8 多态性与 2 型糖尿病关联 |
| G001 | PMC13630686 | `0` | 糖尿病筛查诊断阈值，不涉及 ABCC8 基因多态性 |
| M001 | PMC13610795 | `1` | 主要叙述肥胖/胰岛素抵抗与肾脏结局的链条，不直接阐释慢性炎症如何导致胰岛素抵抗 |
| M001 | PMC13637635 | `2` | 片段明确写出慢性炎症通过细胞因子损害胰岛素受体信号和糖代谢，可直接解释所问机制；但属于引言/引用性描述 |
| M001 | PMC13604364 | `1` | 将低度炎症与胰岛素抵抗列为共同机制，未说明炎症导致胰岛素抵抗的具体通路 |
| P001 | PMC13627872 | `1` | 提到 SGLT2i 对非糖尿病患者也有心血管/死亡获益，但没有直接比较糖尿病分层的肾脏结局 |
| P001 | PMC13640178 | `0` | 研究为老年 T2D 住院/肾功能情况，未包含无 T2D 的 SGLT2i 肾结局对比 |
| P001 | PMC13627207 | `0` | MASLD 药物综述表涉及 SGLT2i，但片段未呈现 T2D 与非 T2D 的肾结局比较 |
| R001 | PMC13643946 | `UNCERTAIN` | 出现 CKD-EPI 估算公式，方向高度相关，但数学排版抽取已严重损坏，不可直接用于计算或引用 |
| R001 | PMC13629947 | `0` | 只用 eGFR 小于 60 作为试验纳入标准，没有说明 eGFR 如何估算 |
| R001 | PMC13619518 | `0` | 图注把 eGFR 当风险指标，未介绍公式或计算方法 |
| S001 | PMC13646093 | `0` | 病例系列和神经病变背景，并非 CKD 随机试验中的 SGLT2i 不良事件报告 |
| S001 | PMC13640178 | `0` | 老年 T2D 住院情况，片段未给出 CKD SGLT2i 临床试验的具体不良事件 |
| S001 | PMC13634386 | `0` | 达格列净在 CKD 的成本效益分析，片段未报告临床试验不良事件 |
| T001 | PMC13637892 | `0` | 描述肌酐等检测方法，未给肌酐或 eGFR 参考区间 |
| T001 | PMC13645658 | `0` | eGFR 25.5（IQR 21.3–28.2）是患者样本分布值，并非健康人群的参考区间 |
| T001 | PMC13649160 | `1` | 临床病例表提供血肌酐 0.5–1.0 mg/dL 参考范围，但片段未见 eGFR 参考范围 |

## 五、双人标注与数据保护

1. 保留 `reports/pilot_v1_frozen/` 快照，不对其进行标注；正式标注只在工作副本中进行。
2. 标注者首先读题目、问题级标准和预览；如果预览不足，打开论文 Methods/Results/Table/原始图表，记录核验位置后再定标签。
3. 推荐对全部 24 条试点样本进行双人独立试标；扩大到 178 条时对约 20%–30% 做分层双人复核，覆盖不同问题和 BM25/Dense 来源层。分层抽样由不参与标签判定的协调人员进行，不向标注者暴露来源。
4. 用 0/1/2 完全标注的共同子集计算加权 Cohen’s κ；同时报告 UNCERTAIN 和缺失率。争议样本进行明确仲裁，保存原始 R1、R2 和最终仲裁标签。
5. 如果没有第二名独立审稿人，明确标注为单人 Pilot，不可虚构一致性指标，也不能把 AI 初审当成人工双标。
6. 不能将任何**未标注、被排除或未检索到**的论文自动判为 0。

## 六、后续评价协议注意事项

- 先冻结同一批完整问题 ID，确认 BM25 与 Dense 的语料版本一致。
- 排序比较使用相同每问题预算（Top-5/10）；如需 Top-20，先扩展检索池并人工标注新增候选。
- BM25、Dense、RRF 在同一套已判定的 `(query_id, document_id)` 标签上计算论文级指标，未判断项不得默认为不相关。
- RRF 参数/证据感知规则在开发集选择；最终测试集不能参与参数设计。当前 8 题是流程 Pilot，不能作为最终泛化证据。
- `relevance_label=2` 的比例不是临床证据等级；应单独跟踪研究设计、结果可重复性、证据直接性、表格/公式提取可靠性。

## 七、在 PyCharm 中怎么用

1. 将此 `.md` 放入项目 `docs/` 或 `reports/` 供标注者阅读。
2. 在 `reports/pilot_annotation_24.csv` 的**副本**上人工填写 16 个原始字段中的相关性与证据属性；不要把附表 `AI_preview_suggestion_NOT_GOLD` 复制到 `relevance_label`。
3. 第一轮重点核验 `C001/PMC13627872` 的随机设计，以及 `R001/PMC13643946` 的残损公式，同时试标若干明显不相关的 G001/S001。
4. 完成 24 条后再确定如何将标注规则应用到 178 条候选，并执行双人复核。
