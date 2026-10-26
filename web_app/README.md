# web_app — Streamlit 前端

在核心 Agent 之上提供浏览器界面，不修改 `agent/` 与 `scripts/` 的任何逻辑。

## 两种模式

| 模式 | 行为 | 标注 |
| --- | --- | --- |
| 通用医学问答（默认） | 直接请求百炼 Qwen，不检索、不核验 | 显著标记"通用模型知识 · 未经本地文献核验" |
| 严格循证问答 | 调用 `scripts/run_agent.py` 对应的 Agent 路径，检索 + 覆盖 + 引用核验 | 证据不足拒答；不自动用模型知识补齐 |

通用模式可勾选"先尝试本地文献检索"：只有 Agent 返回 `answered` 且带引用主张时才采用，否则回退到明确标注的通用回答。

## 结果状态

| 状态 | 页面表现 |
| --- | --- |
| `answered` | 绿色：主张全部通过引用核验 |
| `partial` | 黄色：有已核验主张，但覆盖仍有缺口 |
| `insufficient_evidence` | 红色：运行完成，但没有可核验证据 |
| `error` | 深红"运行失败"：网络/SDK/guardrail/核验异常，附 `reason_code` 与排查提示 |
| `model_knowledge` | 蓝色：仅通用模式，明确标注未核验 |

`error` 与 `insufficient_evidence` 严格分开：前者是故障，后者是证据结论。每次严格模式回答还会展开"本次运行信息"，显示模型与 token 用量。

## 文件

| 文件 | 职责 |
| --- | --- |
| `app.py` | 页面、样式、结果卡片、导出 |
| `bridge.py` | 子进程/常驻调用、配置状态、Markdown 报告 |
| `question_router.py` | 模式路由与回退规则 |
| `general_qa.py` | 通用模式（OpenAI 兼容接口） |
| `history_store.py` | SQLite 历史与单 worker 队列 |
| `resident_runner.py` | 常驻检索器（复用 Qdrant + BGE-M3） |
| `reporting.py` | 导出内容的事实性声明 |
| `tests/` | 队列、双模式、历史迁移的离线测试 |

## 在线检索

`.env.agent` 设置 `MEDEVIDENCE_ONLINE_ENABLED=1` 后，严格模式在本地证据不足时可通过
`search_online_literature` 查 PubMed/Europe PMC 摘要，宽泛问题优先用 `search_online_guidelines`
查指南/综述摘要。侧边栏会显示“联网检索 · 已开启（摘要级）”，证据卡片标注“PubMed 摘要”并提供链接；
导出报告会注明在线摘要条数与“仅摘要”限制。

宽泛且未指定具体疾病的问题会显示“范围提示”，建议缩小到心衰、冠心病、房颤等方向；
该提示不是答案，也不会被当作证据。

关闭时改为 `0`，行为与纯本地版本一致。

## 运行

```powershell
python -m streamlit run web_app/app.py
# 或 Windows 双击仓库根目录的 run_web.bat
```

默认 <http://127.0.0.1:8501>，只监听本机。首次提问加载模型会较慢。

引擎选择：

```powershell
$env:MEDEVIDENCE_WEB_ENGINE = "resident"   # 默认：复用进程内检索器
$env:MEDEVIDENCE_WEB_ENGINE = "cli"        # 回退：每题独立子进程
```

## 并发与数据

- 后台 `ThreadPoolExecutor(max_workers=1)`：同一时间只跑一个 Agent，避免嵌入式 Qdrant 并发锁。
- 历史存 `web_app/data/history.sqlite3`，进程重启会把未完成任务恢复为排队。
- 历史文件、`.python_path.txt` 均已被 `.gitignore` 忽略。

## 测试

```powershell
python -m unittest discover -s web_app/tests -p "test_*.py" -v
```

## 隐私

问题和必要证据片段会发送到配置的模型 API；历史记录保存在本机。不要输入患者姓名、证件号、
联系方式或完整病历。
