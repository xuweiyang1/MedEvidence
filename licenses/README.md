# 许可登记（License Registry）

两级登记，全部由脚本自动写入：

1. **来源级**：`registry.csv`（本目录），由 `scripts/download_mesh_do.py` 等写入/更新。  
   字段：`source, item, file, url, sha256, bytes, license, page, retrieved_at`
2. **文章级**：`../data/raw/pmc/manifest.csv`，由 `scripts/download_pmc_oa.py` 写入。  
   字段：`disease, pmcid, version, license_code, is_manuscript, is_retracted, title, xml_bytes, xml_sha256, downloaded_at, xml_path, json_path`

## 收录口径（V1）

| 来源 | 口径 |
| --- | --- |
| PMC OA | 仅 CC0 / CC BY / CC BY-SA（允许商用）；NC、ND 系列不收 |
| MeSH | NLM 条款，可自由使用/再利用，须致谢 NLM |
| Disease Ontology | CC0 1.0 |
| WHO（后续） | 默认 CC BY-NC-SA 3.0 IGO，非商业，单独标注 |
| CDC（后续） | 美国政府公有领域，注明来源 |
| OpenAlex | CC0（仅在线查询，不落盘快照） |
| PubMed | NLM 数据；摘要版权随出版商而异，如实登记 |

## 复核清单（每次批量下载后）

- [ ] `registry.csv` / `manifest.csv` 已更新，含 `retrieved_at`
- [ ] SHA-256 与实际文件一致
- [ ] 没有混入 NC / ND 或其他受限许可
- [ ] `is_retracted = yes` 的条目已在分析阶段单独标记
- [ ] 检索式与出处留存于 `data/raw/pmc/provenance/*.esearch.json`
