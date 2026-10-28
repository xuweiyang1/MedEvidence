# 贡献指南

感谢你参与 MedEvidence 项目。

## 开始之前

- 建议先通过 Issue 或讨论确认修改方向。
- 请勿提交 API Key、患者隐私信息、本地数据库或索引文件。
- 每个 Pull Request 尽量只处理一个明确的问题。

## 开发流程

1. 从 `main` 创建新分支。
2. 在新分支中完成修改。
3. 运行相关检查：

   ```powershell
   python scripts/run_agent.py --self-test
   ```

4. 提交修改并创建 Pull Request。

## Pull Request 要求

请在 Pull Request 中说明：

- 修改了什么；
- 为什么需要修改；
- 如何验证修改；
- 是否影响现有配置或功能。

代码修改应尽量包含相应测试，文档修改应确保命令和路径准确。
