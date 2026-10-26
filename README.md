# MedEvidence V1 — 数据层

跨疾病医学循证分析系统（比赛项目）的数据获取与预处理工作区。
V1 只使用公开、合法可复用的数据：PMC OA 为核心证据库，MeSH + Disease Ontology
做疾病实体标准化，WHO / CDC 少量补充，OpenAlex / PubMed 在线查询。

> 本地路径：`D:\Download\Agent\MedEvidence`（2026-10-13 从 C 盘迁移，原路径 `C:\Users\xuweiyang\Documents\默认项目\MedEvidence`）。
> 迁移到其他电脑：见 `docs\迁移指南.md`（含完整性校验脚本 `scripts\verify_data.py`）。

## 目录

| 路径 | 内容 |
| --- | --- |
| `configs/diseases.json` | 五个疾病域的关键词表（可自行增改） |
| `scripts/download_pmc_oa.py` | PMC OA 分疾病下载（eSearch → AWS 云端服务，官方渠道） |
| `scripts/extract_pmc_text.py` | PMC JATS XML → 纯文本（后续切分建库的输入） |
| `scripts/download_mesh_do.py` | MeSH desc2026/qual2026 + DO doid.obo/owl |
| `licenses/registry.csv` | 来源级许可登记（由脚本自动写入） |
| `data/raw/` | 原始数据（PMC 每篇的许可与哈希见 `data/raw/pmc/manifest.csv`） |
| `data/processed/` | 可重建的预处理产物（文本、切分等），随时可删 |

## 快速开始

只需 Python 3.10+，无第三方依赖。

```powershell
cd MedEvidence

# 1) 小规模验证：每个疾病域 3 篇
py -X utf8 scripts\download_pmc_oa.py --email 你的邮箱 --per-disease 3

# 2) 检查 data/raw/pmc/manifest.csv：
#    - 许可证分布（应只有 CC0 / CC BY / CC BY-SA）
#    - 文件大小、is_retracted 标记
#    - provenance\*.esearch.json 记录了每个疾病域的检索式与出处

# 3) 提取全文（自动报告疑似提取失败的篇目）
py -X utf8 scripts\extract_pmc_text.py

# 4) 验证通过后扩量：每疾病 200 篇（约 1000 篇）
py -X utf8 scripts\download_pmc_oa.py --email 你的邮箱 --per-disease 200

# 5) 需要时下载 MeSH + DO（约 0.4–0.7 GB）
py -X utf8 scripts\download_mesh_do.py
```

## 许可口径（V1）

- PMC：仅收录 **CC0 / CC BY / CC BY-SA**（允许商用口径）；NC 系列不收。
  每篇的 `license_code`、`is_retracted`、SHA-256 均记录在 `manifest.csv`。
- MeSH：NLM 条款，可自由使用/再利用，须致谢 NLM。
- Disease Ontology：CC0 1.0。
- 自动化获取只走官方渠道：PMC Cloud Service、E-utilities、IRIS OAI-PMH、CDC Syndication。

## 磁盘

- V1 全量预估 13–22 GB（含 Python 环境与模型缓存）；250–500 篇阶段 < 3 GB。
- 若系统盘空间紧张，把 `--out` 指到空闲盘即可，例如：

```powershell
py -X utf8 scripts\download_pmc_oa.py --email 你的邮箱 --per-disease 50 --out D:\MedEvidence\data\raw\pmc
```

## 常见问题

- **`python` 命令报错或无输出** → 用 `py -X utf8` 代替（Windows 商店占位程序的坑）。
- **HTTPS 证书错误（代理 / 安全软件环境）** → 脚本会自动依次尝试
  `truststore` → `certifi` → 系统默认；都不可用时安装其一：
  `py -m pip install certifi`。诊断命令：`py -X utf8 scripts\diagnose_network.py`。
- **下载中途中断** → 直接重跑，已入库的 PMCID 会自动跳过。
- **GitHub raw / jsDelivr 直连不通（部分地区网络）** → 先开代理，并在运行前设置：
  `$env:HTTPS_PROXY="http://127.0.0.1:7897"; $env:HTTP_PROXY="http://127.0.0.1:7897"`
  （端口换成你自己的代理）；脚本会自动使用环境变量里的代理。

## 后续计划（不在本目录内实现）

- 切分 + BGE-M3 向量化 + Qdrant 建库
- WHO（IRIS OAI-PMH）/ CDC（Syndication API）小批量补充
- SCI/实验：SciFact、PubMedQA、BEIR 评测集单独下载（评测用，不进知识库）
