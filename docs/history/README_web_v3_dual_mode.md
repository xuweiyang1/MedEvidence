# MedEvidence 网页双模式升级 V3

这是**现有网页 V2 的小范围功能升级**，不是重装 Agent，也不重建索引。升级包只包含 `web_app/` 下的 5 个 Python 文件及离线测试；不会包含或覆盖 `.env.agent`、`agent/`、`scripts/`、`web_app/data/history.sqlite3` 和 Qdrant 数据。

## 新功能

- **通用医学问答（默认、快速）**：直接请求已经配置的百炼 Qwen 接口。**不**检索 Qdrant、不加载 BGE-M3、不进行引用核验；网页会突出显示“语言模型知识、未经本地证据核验”，不会提供伪造的引用，也不显示虚假的检索覆盖率。
- **可选先查本地文献**：勾选后会先使用原 V0.3.0 Agent。仅在 Agent 返回有证据、且带有引用主张的完整回答时采用该答案；否则转为明确标注的 Qwen 通用回答。**可能仍然很慢**；本地 Qdrant 锁定时会标记检索出错并回退。
- **严格循证问答**：保留原 V0.3.0 的多轮检索和引用核验；证据不足时依旧拒答，不会使用模型知识冒充文献证据。
- **保留历史记录**：自动给旧 SQLite 历史表增加 `mode`、`prefer_evidence` 列，旧问题自动认定为原严格模式。每条新问题记录实际选择的模式。
- **导出修正**：通用问答 Markdown 报告不再冒称“循证报告”，不会虚构检索次数、论文引用或覆盖率。
- **修复文字颜色保留**：新版 `app.py` 已整合前一个浅色主题文字对比度补丁。

## 运行前注意

1. 关闭原 MedEvidence 网页进程（启动窗口 `Ctrl+C`），避免 Qdrant 数据库被多个进程锁定。
2. 将 ZIP 下载到 `E:\Download`；在 `(xwy)` PowerShell 中按下方一次性安装。
3. 程序仍读取你的 `.env.agent`，**不需要提供或复制 API Key**。通用模式会发请求到百炼，需要网络和 API 额度。

## 一次性安装（PowerShell）

```powershell
$root = 'E:\Download\Agent\MedEvidence\MedEvidence_Agent_V0_3_0'
cd $root

$zip = Get-ChildItem 'E:\Download' -Filter 'MedEvidence_Web_UI_Dual_Mode_V3*.zip' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { throw '没有找到双模式升级 ZIP；请先下载到 E:\Download。' }
if (-not (Test-Path '.\web_app\bridge.py')) { throw '请先安装之前能运行的网页 V2。' }

$backup = 'E:\Download\MedEvidence_Web_UI_Backup_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
Copy-Item '.\web_app' $backup -Recurse -Force -ErrorAction Stop
Expand-Archive -LiteralPath $zip.FullName -DestinationPath $root -Force -ErrorAction Stop
python -m unittest discover -s .\web_app\tests -p 'test_*.py' -v
if ($LASTEXITCODE -ne 0) { throw '离线测试失败，请先不要启动，把报错发给我。' }

# 保守起见，严格循证问答仍先使用之前验证过的 CLI 调用方式。
$env:MEDEVIDENCE_WEB_ENGINE = 'cli'
.\启动MedEvidence网页.bat
```

浏览器打开 <http://127.0.0.1:8501>，按 `Ctrl+F5` 刷新。

## 验收（仅一题）

切到默认**通用医学问答**，**不要**勾选“先尝试本地文献检索”，问：`高血压的常见治疗方法有哪些？`。

预期：
- 生成一段有实质内容的科普回答，而不是因本地引用不足而直接拒答；
- 顶部醒目标识：`通用模型知识 · 未经本地文献核验`；
- **没有**伪造的 PMC 引用或“覆盖率 100%”之类数据；
- 左侧可以重新找到此问答；下载的 Markdown 声明回答来源。

严格循证模式仍可能拒答，这**不是**本补丁的错误，而是原有规则的设计。新模式不等于医学准确性已经过临床审核。

## 回退

停止网页后，将 `$backup` 目录下备份的 `web_app` 文件复制回项目根目录，保留新增的历史 SQLite 文件即可；**不要删除** `web_app/data/history.sqlite3`。备份目录在 `E:\Download`，不应上传 GitHub。

**已有实验与数据不变**：V0.3 Agent、V0.4.1 独立实验、BioASQ 结果均没有被修改。仍不支持真正并行的本地 Qdrant 进程，后台工作队列继续一次处理一个请求。通用模式较快是因为跳过本地检索，而不是优化了检索算法本身。
