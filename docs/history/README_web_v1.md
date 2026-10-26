# MedEvidence V1.0 网页前端（直接连接现有 V0.3.0 Agent）

## 功能

- 浏览器里提问、查看有来源的回答和参考文献链接
- 展示检索次数、启发式证据覆盖率、引用核验后的医学主张、证据不足原因
- 导出 Markdown 问答报告；支持连续多条会话记录（当前浏览器会话期间）
- 默认仅在本机 `http://127.0.0.1:8501` 开放，不改动任何原有 RAG、Agent 或实验脚本
- 当多个用户同时请求时串行处理，避免本地 Qdrant 并发冲突

## 安装：只做一次

1. 解压这个 ZIP，**目标目录是**：

   `E:\Download\Agent\MedEvidence\MedEvidence_Agent_V0_3_0`

   解压之后目录结构应该是：

   ```text
   MedEvidence_Agent_V0_3_0/
   ├── agent/                        # 原有，不需要修改
   ├── scripts/run_agent.py          # 原有，不需要修改
   ├── web_app/
   │   ├── app.py
   │   ├── bridge.py
   │   ├── setup_launcher.py
   │   └── requirements-web.txt
   └── 启动MedEvidence网页.bat
   ```

2. 使用**原来能够成功运行 MedEvidence 的 (xwy) PowerShell**，执行以下三条命令：

   ```powershell
   cd E:\Download\Agent\MedEvidence\MedEvidence_Agent_V0_3_0
   python -m pip install -r .\web_app\requirements-web.txt
   python .\web_app\setup_launcher.py
   ```

   预期输出：`WEB LAUNCHER CONFIGURED`。脚本只保存 Python 解释器路径，不会保存 Key。

3. 以后**双击 `启动MedEvidence网页.bat`**。浏览器一般会自动打开；如未打开，在浏览器访问：

   `http://127.0.0.1:8501`

   使用者不用打开 PyCharm 或命令行执行 Python。关闭启动时出现的命令行窗口即停止本地服务。

## API Key

继续使用原项目的 `.env.agent`（原位置不变）。不要将 `.env.agent` 上传到 GitHub。
如果已经能用 `python scripts/run_agent.py --question "..."` 正常提问，则网页复用同一份配置。

## 重要说明

1. 本网页调用的是真正的 **V0.3.0 Agent SDK CLI (`--json`)**，没有使用假回答。它运行本地 Qdrant/SQLite 检索，再按既有设置调用百炼 API。
2. V0.4.1 严格子问题级门控仍属于实验模块，没有接入正式 V0.3 Agent；网页不冒称已启用。
3. 首次提问要加载本地模型，可能相对慢。每次问答新建 Agent 子进程，更稳定但会重复加载模型；后续若有真实性能需求，再考虑常驻后端。
4. 这是**本地网页**，本机用户不需要 IDE。其他电脑远程访问需要单独部署，并设计身份认证、隐私保护和本地索引访问，不能直接将 8501 端口公开到互联网。
5. 默认追踪中不保存原问题内容，但问题和必要证据会被传往配置的模型 API。请勿提交含患者个人身份信息的病例。
6. 不构成临床决策支持器械或诊疗建议。

## 常见问题

- **双击闪退 / 找不到 Streamlit**：重新在 `(xwy)` 环境执行 `python -m pip install -r .\web_app\requirements-web.txt` 和 `python .\web_app\setup_launcher.py`。
- **API Key 未配置**：检查原有 `.env.agent` 中的 `DASHSCOPE_API_KEY`。界面只判断是否有配置，实际可用性由真实请求确认。
- **8501 被占用**：关闭之前打开的 Streamlit 服务，或将 BAT 中的 `--server.port=8501` 改为另一个端口。
- **Qdrant / 模型无法加载**：回到 `(xwy)` 环境验证已有 Agent，本前端不修改原检索栈。
- **上传 GitHub**：提交 `web_app/*.py`、依赖文件和 BAT；**不要**提交 `web_app/.python_path.txt`、`.env.agent`、密钥、完整 Qdrant 索引和患者材料。

## 验证（可选）

在已有环境中：

```powershell
python -m unittest discover -s .\web_app\tests -p "test_*.py" -v
```
