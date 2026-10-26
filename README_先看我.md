# MedEvidence Phase 1C — Qwen 证据卡 + Streamlit 复核（3 条试点）

**当前定位：辅助审核工具，不是 Gold 标签生产器。** 此包只新增文件，不覆盖你原有 BM25、Dense、Qdrant、178 条盲评、24 条试标注或手工标注。演示不需要 GPU，Qwen 使用阿里云百炼 API；真实 API 费用由你的账号承担。

## 1. 在 PyCharm 里安装

项目根目录应是：`E:\Download\Agent\MedEvidence`，现有 Conda 解释器：`D:\Anaconda\envs\xwy\python.exe`。

1. **先备份项目**。解压 ZIP。把 ZIP 的 `scripts/` 下 **5 个新增文件**复制到你项目已有的 `scripts/` 下，不要删除原文件。`requirements_review.txt` 和 `.env.example` 放项目根目录。测试文件可选。
2. PyCharm → **Settings → Project: MedEvidence → Python Interpreter**，确认解释器为 `xwy` 环境，在 Python Packages 安装 `openai`、`streamlit`、`python-dotenv`。或者使用 PyCharm 下方 Terminal（不是要求你另外打开 PowerShell）安装项目根目录 `requirements_review.txt`：`python -m pip install -r requirements_review.txt`。
3. 在项目根目录把 `.env.example` 复制为 **`.env`**。用文本编辑器把 `your_own_dashscope_api_key_here` 改为你自己的阿里云百炼 Key。**不要把 Key 发给任何人，不要提交 Git，也不要在截图中显示。** 若你的 Key 属于其他地域或业务空间，请根据阿里云官方文档修改 `DASHSCOPE_BASE_URL`。默认示例使用北京地域 `https://dashscope.aliyuncs.com/compatible-mode/v1`，默认模型 `qwen-plus`。如果你的账号未授权 qwen-plus，请改成自己有权限的模型。
4. 在 `.gitignore` 中加入 `.env`、`data/review/`、`reports/evidence_cards.jsonl`、`reports/pilot_annotation_24_human_review.csv`，避免无意提交含审核材料和密钥的文件。不要把日志/私密文件上传到公共仓库。

## 2. 所需输入（直接使用你现有文件）

| 用途 | 默认相对路径 |
| --- | --- |
| Pilot CSV（任意一个） | `reports/pilot_annotation_24_AI预标注_非Gold.csv`（优先）；`reports/pilot_annotation_24.csv`（备用） |
| 42,526 条检索 Chunk | `data/processed/knowledge/v3_1_all/retrieval_chunks.jsonl` |
| Parent Context | `data/processed/knowledge/v3_1_all/parent_units.jsonl` |
| 论文元数据 | `data/processed/knowledge/v3_1_all/documents.jsonl` |

模型**不会**收到人工/AI 预标注的标签，只收到问题、标题与选出的原文来源。自动选取命中 Child、匹配 Parent（如可匹配）、摘要及同论文 Methods / Results / 表格相关片段，总字符数默认上限 19,000，最多 12 段。**这不是整篇全文**：有些 Parent ID 可能无法匹配，有些正文不在当前检索库中。提供的文本会截断，并在界面中提示。

## 3. 最安全的三步运行方式：PyCharm 右键 Run

**A. 先离线检查上下文（建议）**：打开 `scripts/generate_evidence_cards.py` → Run → Edit Configurations → Parameters 填 `--context-only` → Run。会打印三份 `reports/context_preview_*.json`。打开检查 `C001/PMC13627872`、`R001/PMC13643946`、`M001/PMC13637635` 的来源是否合理。**此模式不使用 API，不生成卡片。**

**B. 生成三张证据卡**：移除 Parameters 的 `--context-only`，右键 `generate_evidence_cards.py` → Run。默认只生成：
- `C001 / PMC13627872`
- `R001 / PMC13643946`
- `M001 / PMC13637635`

程序读取原文，调用 Qwen 严格 JSON 输出，为每条引文验证 `source_id` 与字面内容，然后保存在**独立** SQLite 文件 `data/review/evidence_review_v1.sqlite3`，并导出 `reports/evidence_cards.jsonl`。已成功生成过的卡片会跳过，**不会覆盖**。如果要重新生成，请先备份 SQLite 后由程序维护，不要直接删掉已有人工审核结果。

若 `DASHSCOPE_API_KEY` 没有读取到：确认 `.env` 在项目根目录且已安装 `python-dotenv`。若模型返回权限/地域错误：检查 `.env` 的模型、Key、BASE_URL。若上下文只有预览，则该卡不能认定全文证据完整。

**C. 启动审核界面**：在 PyCharm 打开 `scripts/run_review_app.py` → 右键 Run。浏览器打开 `http://localhost:8501`（仅本机）；如果浏览器没自动打开，请复制地址访问。按左侧导航选择论文，查看中间 Qwen 卡片、右侧核对并选标签，勾选确认，点击**保存并下一条**。

审核过程实时存入 SQLite，并导出 `reports/pilot_annotation_24_human_review.csv`，**只导出已保存的审核行**（3 条试点最多3行），而不是伪造 24 条已审核或复制 AI 预标注。不要直接手改导出 CSV，它是从 SQLite 自动重建的。想重复人工审核可换一个 reviewer_id（如 R2），避免以 AI 代替第二人。

## 4. 引用核验状态的真实含义

| 状态 | 程序检查 | 不代表什么 |
| --- | --- | --- |
| `QUOTE_VERIFIED` | 该原文引文的字面文本在**指定来源 ID**中出现（大小写和空白标准化） | **不代表**原文支持模型结论或属于原始 RCT |
| `QUOTE_PARTIAL` | 只匹配部分较长连续文本 | **不能**直接引用为逐字证据 |
| `QUOTE_NOT_FOUND` | 在指定原文片段中找不到 | 不能视为可信证据 |

引用核验不检查事实是否正确，也不检查统计推断、样本适用性、RCT 身份。无法确定时请保留 `UNCERTAIN`，不要强行填 `2`。对于 `R001` 公式乱码或表格 OCR 问题，请打开 PMC 原文核查。论文全文、研究设计和专业医学判断仍需专家把关。

## 5. 后续从 3 条扩展到 24 条

只有当3条验证：①命中原文对得上论文 ②研究设计未明显猜测 ③没有伪造引用 ④界面保存正常，才在 PyCharm 的 `generate_evidence_cards.py` **Edit Configurations → Script parameters** 填：

`--all-pilot --limit 24`

然后右键 Run，已有3条自动跳过，其余最多21条通过 Qwen 生成；若 API 报错会单条跳过。然后再次运行 `run_review_app.py`。

**扩大到178条是另一阶段**：应先完成抽样复核和统一标注规则，且 `--all-pilot` 只读取当前 24 行输入，**不会**自动跑178条。

## 6. 常见问题

- **找不到 Streamlit**：PyCharm Python Interpreter 安装 `streamlit`，确认 Python 版本属于 `xwy`。
- **已有卡片被跳过**：正常，为保护已有复核数据。删除卡片可能造成复核结果与卡片不一致，不要随便删除 SQLite。
- **切换模型 / 调提示词后还想重新生成**：请先备份 `data/review/evidence_review_v1.sqlite3`，新建独立试验数据库，而不是修改已有审核基线。
- **显示来源缺失**：Chunk/Parent 映射不一致；先检查 `reports/context_preview_*.json`，不要依靠模型猜想补证据。
- **成本问题**：默认只调用3次 Qwen；每次约最多19k个文本字符再加系统提示，不同论文耗费不同，先检查 API 单价与账号余额。
- **双人审核**：R1 与 R2 独立审核之后需仲裁，不能把模型视为独立第二审核员。

## 7. 代码测试与后续 Agent

附带 `tests/test_evidence_review_core.py`，支持 `python -m pytest -q tests/test_evidence_review_core.py`（需安装 pytest，非运行所必需）。开发者已运行本地语法检查及 4 条离线单元测试；未调用你的阿里云 API，也未访问你的 42,526 条真实 Chunk，因此**真实上下文和医学正确性仍待你的 PyCharm 本机测试**。

接口边界清晰：将来 MedEvidence Agent 可以用 `retrieve(query)` 找候选，`generate_evidence_cards` 做证据抽取，`audit_card` 做文字溯源，`review_app` 作为人工质量闸门。正式 Qwen 回答/Agent 应只使用已核验、适合回答的证据，不要直接把建议标签当 Gold。
