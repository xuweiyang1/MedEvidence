# 数据管线

本仓库不包含数据。本文说明当前索引是如何从公开数据构建出来的，以及如何在本机复现或校验。
完整的下载链接与来源清单位于 [数据下载清单_v1.md](数据下载清单_v1.md)。

## 1. 数据来源与许可

| 来源 | 用途 | 许可口径 |
| --- | --- | --- |
| PMC Open Access | 核心证据库（全文 XML / 元数据） | 仅 CC0 / CC BY / CC BY-SA，不收 NC / ND |
| MeSH（desc2026 / qual2026） | 疾病与主题实体标准化 | NLM Terms，可复用，须致谢 NLM |
| Disease Ontology（doid.obo / owl） | 疾病实体映射 | CC0 1.0 |
| WHO IRIS / CDC | 指南层补充（当前索引未纳入） | 各自许可，单独标注 |
| PubMed / OpenAlex | 在线元数据查询，不落盘 | 按来源条款 |

所有自动化获取只走官方渠道（E-utilities、PMC Cloud Service、IRIS OAI-PMH、CDC Syndication）。
逐篇的 `license_code`、`is_retracted`、SHA-256 记录在 `licenses/registry.csv` 与 PMC `manifest.csv`。

## 2. 五个疾病域

`configs/diseases.json` 定义了心血管、内分泌代谢、呼吸、肾脏、感染五个域的关键词。
检索式按域构造，写入 `data/raw/pmc/provenance/*.esearch.json` 以便复现。

```json
{
  "cardiovascular": ["hypertension", "coronary heart disease"],
  "endocrine_metabolic": ["type 2 diabetes", "thyroid disease"],
  "respiratory": ["asthma", "chronic obstructive pulmonary disease"],
  "renal": ["chronic kidney disease"],
  "infectious": ["pneumonia"]
}
```

## 3. 构建步骤

```powershell
# 0) 小规模验证：每个疾病域 3 篇
python scripts/download_pmc_oa.py --email you@example.com --per-disease 3

# 1) 正式下载（示例：每域 200 篇）
python scripts/download_pmc_oa.py --email you@example.com --per-disease 200

# 2) 术语资源（约 0.4–0.7 GB）
python scripts/download_mesh_do.py

# 3) JATS XML -> 纯文本，并输出解析报告
python scripts/extract_pmc_text.py

# 4) 结构化解析（标题、章节、表格、图注）
python scripts/parse_pmc_jats_v1.py

# 5) 分层切块（parent/child，v3.1 方案）
python scripts/build_hierarchical_chunks_v3_1.py

# 6) 过滤为试验语料（纳排、去重、许可复核）
python scripts/filter_trial_corpus_v1b1.py

# 7) 导出知识库 jsonl（documents / parent_units / retrieval_chunks / relations）
python scripts/knowledge_import_core.py

# 8) 建索引：BM25 与 Dense 分别构建，再统一校验
python scripts/index_coordinator.py --version v3_2_trial_clean plan
python scripts/index_coordinator.py --version v3_2_trial_clean build --model-path <BAAI/bge-m3 本地路径>
python scripts/index_coordinator.py --version v3_2_trial_clean verify
```

每一步都以"可重建"为前提：中间产物在 `data/processed/` 下，可随时删除重跑。

## 4. 产物布局

```text
data/
├── raw/
│   ├── pmc/                 # 每篇 PMC 的 XML/JSON + manifest.csv + provenance/
│   ├── mesh/                # desc2026 / qual2026
│   └── do/                  # doid.obo / doid.owl
└── processed/
    ├── pmc_text/            # 纯文本
    ├── knowledge/
    │   └── v3_2_trial_clean/
    │       ├── documents.jsonl          # 922 篇
    │       ├── parent_units.jsonl       # 25,696
    │       ├── retrieval_chunks.jsonl   # 42,553
    │       └── evidence_relations.jsonl # 116,640
    └── index_versions/
        └── v3_2_trial_clean/
            ├── qdrant/                  # Qdrant local 目录（含 meta.json）
            ├── bm25.sqlite3             # FTS5 / BM25
            ├── dense_manifest.json      # 块数、指纹、模型身份
            └── bm25_manifest.json
```

索引 manifest 记录块 ID 集合哈希、语料哈希、模型文件哈希与构建参数，用于证明"索引与语料同源"。

## 5. 校验

```powershell
python scripts/verify_data.py            # 迁移/完整性校验
python scripts/verify_corpus.py          # 语料一致性
python scripts/validate_index_version_v1c2.py --version v3_2_trial_clean --with-vectors
python scripts/run_agent.py --retriever-check
```

`--retriever-check` 会打开 Qdrant collection 并确认 FTS5 表存在，不调用任何模型 API。

## 6. 磁盘与时间

- 全量数据与索引约 5–6 GB；模型缓存另计。
- 下载与解析是最耗时的部分；嵌入构建建议使用 GPU（索引 manifest 中记录为 `cuda`、fp16）。
- 若系统盘紧张，下载脚本均支持 `--out` 指向其他磁盘，索引路径在 `.env.agent` 中配置绝对路径。

## 7. 常见问题

- **证书错误 / 代理**：脚本会依次尝试 `truststore`、`certifi`、系统默认；诊断用 `python scripts/diagnose_network.py`。
- **下载中断**：直接重跑，已入库的 PMCID 会跳过。
- **Qdrant 被锁**：嵌入式 Qdrant 同一时间只允许一个进程；先关闭其他 Agent/网页进程。
- **索引与语料不一致**：以 manifest 中的块哈希为准，不要手工改 `data/processed/`。
