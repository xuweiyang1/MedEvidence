# MedEvidence 网页 V2 性能修复：常驻 Agent（非新算法）

## 为什么会慢

旧版网页每个问题均执行 `subprocess.run([python, scripts/run_agent.py, ...])`：进程结束后 BGE-M3 编码器和本地 Qdrant 连接会被释放。新问题只能重新加载模型及连接索引。除此之外，最多 3 轮的 Agent 规划、覆盖判断与引用核验可能多次等待云端 Qwen。

## 这次修复

- 前端改为 **Streamlit 服务进程内的唯一后台 worker** 调用冻结的 `agent.sdk_runtime.run_sdk_agent`，**复用同一个本地 Qdrant/FTS5 连接以及 BGE-M3 模型**。每次问题重新建立独立 Agent 状态，不继承前一次证据。
- 不修改 `agent/`、`scripts/`、Qdrant 索引和 `.env.agent`；保留 V2 历史记录及排队机制。**同时真正运行的 Agent 仍只有一个**，不是多线程并行。
- 在 `web_app/data/performance.jsonl` 记录运行耗时和检索耗时（秒）、检索次数、模型和设备，不写入问题原文或 API Key。
- 保留原子进程接口作回退：启动网页前在 PowerShell 设置 `$env:MEDEVIDENCE_WEB_ENGINE="cli"`。
- CUDA 为可选。RTX 5060 8GB 用户若 PyTorch 支持 GPU，可在启动前 `$env:MEDEVIDENCE_DEVICE="cuda:0"`。若出现 CUDA 错误/OOM，换回 `cpu`，不要修改索引。
- 可选快速模式 `$env:MEDEVIDENCE_MAX_ROUNDS="2"`，减少最多补检次数（**可能降低证据覆盖；默认保留 3**）。不要通过关闭语义引用核验追求速度。

## 安装（Windows PowerShell）

1. 在运行网页的 PowerShell 窗口按 `Ctrl+C`，停止 Streamlit，等待窗口结束。
2. 下载 ZIP 到 `E:\Download`。
3. 复制下面整个命令块（在 xwy 环境）：

```powershell
cd "E:\Download\Agent\MedEvidence\MedEvidence_Agent_V0_3_0"
$zip = Get-ChildItem "E:\Download" -Filter "MedEvidence_Web_UI_V2_Speed_Fix*.zip" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { throw "未找到性能补丁 ZIP，检查浏览器下载目录" }
if (!(Test-Path ".\web_app\bridge.py")) { throw "未找到原网页：请先安装网页前端 V1" }
Copy-Item ".\web_app" ".\web_app_backup_before_speed" -Recurse -Force -ErrorAction Stop
Expand-Archive -LiteralPath $zip.FullName -DestinationPath "." -Force -ErrorAction Stop
python -m unittest discover -s .\web_app\tests -p "test_*.py" -v
if ($LASTEXITCODE -ne 0) { throw "离线测试未通过，请回退备份" }
$env:MEDEVIDENCE_WEB_ENGINE = "resident"
.\启动MedEvidence网页.bat
```

4. 打开 `http://127.0.0.1:8501`，先问 1 道，然后问第 2 道**不同的问题**。第一道可能仍然很慢：模型首次加载会耗时。第二道不应再为载入模型重复付费。
5. 关闭服务后，查看耗时：

```powershell
Get-Content .\web_app\data\performance.jsonl -Tail 5 -Encoding UTF8
```

其中 `run_seconds` 是单次 Agent 总耗时；`retrieval_seconds` 包括检索器/BGE-M3 的调用时间，差值大致是 Agent 决策、Qwen API 和核验等时间。**不能简单将差值全算成 Qwen 网络时间**。

## 更快但有代价的选项（验证速度后再考虑）

如果确实需要更短的回答等待，可以仅对网页设置：

```powershell
$env:MEDEVIDENCE_MAX_ROUNDS = "2"
.\启动MedEvidence网页.bat
```

这会将最高检索轮次从 3 调到 2，可能降低某些复杂问题的证据覆盖率。不改变项目已有评测参数、算法源码或历史实验结果。

若 CUDA 可用，也可启动前设置 `$env:MEDEVIDENCE_DEVICE="cuda:0"`；是否提速取决于显卡驱动及安装的 PyTorch 版本。**先不要同时改这两个变量**，避免难以归因。

## 回退（不丢历史）

若常驻模式报错，停止网页后先执行：

```powershell
$env:MEDEVIDENCE_WEB_ENGINE = "cli"
.\启动MedEvidence网页.bat
```

这会恢复旧版的每题独立子进程方式，保留新版本的历史记录。若要完全回到备份代码，先停止网页，再恢复 `web_app_backup_before_speed` 内容；不要删除现有 `web_app/data/history.sqlite3`。

## 说明

- 已进行离线逻辑与单元测试；未在用户的 Windows + GPU + Qdrant + 百炼环境中做端到端性能测试，因此**不承诺具体秒数或提速倍数**。
- Streamlit 和一个常驻后台 worker 仍然都是同一台电脑上的服务。多个浏览器可以排队，但当前仍非同时执行多个 Agent。需要多进程真正并行，应另行迁移到 Qdrant 服务模式并做资源评估。
- 问答历史可能包含敏感信息，不要上传 `web_app/data/` 到 GitHub。
