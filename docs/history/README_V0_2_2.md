# MedEvidence Agent SDK V0.2.2 运行问题排查补丁

## 此补丁修复什么

- 使用 OpenAI Agents SDK 的真实 `Runner/Tools/Handoff/Guardrails`，没有改回自定义假 Runner。
- 修复 Writer 最后一轮检索后证据覆盖检查过期导致的过度拦截：若已具备有效子问题和文献，在 Guardrail 执行前自动重新评估最新证据；评估失败仍不得生成未经验证的医学主张。
- 对 `insufficient_evidence` 且 `claims=[]` 的安全拒答允许通过 Guardrail；**不会放行未引用的医学结论**。
- Guardrail 轨迹记录具体失败原因（`invalid_draft`、`coverage_missing_after_latest_search` 等），而不是仅输出异常类名。
- 覆盖率判定兼容 LLM 返回子问题 `index` 的情况（严格校验证据 ID），降低文本轻微改写导致的误判。
- CLI 新增 **`--trace-diagnose`**：只读最新本地轨迹并打印脱敏摘要，离线/免费。
- CLI 新增 **`--retrieval-smoke`**：使用现有 BGE-M3/Qdrant/BM25 真正检索，打印命中数量、标题、ID、文本长度；不调用百炼 API。
- 修复 Windows 临时目录的短路径/长路径导致的一条单元测试误失败。

**重要说明：** 补丁通过 25 项离线测试，尚未在用户本机调用真实 Qdrant/百炼；不能保证本次失败仅由过期覆盖率造成。先用新增命令定位真实原因。

## 安装（不要创建额外内层目录）

PowerShell，先进入你现有的 Agent 子文件夹：

```powershell
cd E:\Download\Agent\MedEvidence\MedEvidence_Agent_SDK_V0_2_A0E7AEBDE8869BE585

# 修改为你实际下载位置
$zip = "E:\Download\MedEvidence_Agent_SDK_V0_2_2_诊断修复补丁.zip"
Expand-Archive -LiteralPath $zip -DestinationPath . -Force
```

ZIP 内最上层就是 `agent/`、`scripts/`、`tests/` 和说明文档；覆盖这些代码文件即可。**ZIP 不包含 `.env.agent`、索引或任何 API Key**。

## 第一步：免费查看上次失败的本地日志

```powershell
python scripts\run_agent.py --trace-diagnose
```

特别关注：

- `search_hit_counts`：真实返回多少条（`检索调用2次`不意味着有命中）
- `errors`：有无 `tool_error`、`runner_error`
- `coverage_events`：是否 `ratio=0.0`，以及是否执行覆盖检查
- `guardrail_events.reason`：真正触发拦截的安全原因

## 第二步：独立验证真实检索（不调用百炼）

```powershell
python scripts\run_agent.py --retrieval-smoke --question "COPD exacerbation systemic corticosteroid duration 5 days"
```

第一次运行要加载 BGE-M3，可能较慢；注意避免与其他 Qdrant local 进程同时打开同一索引。

## 第三步：运行测试，然后再尝试真实问答

```powershell
python scripts\run_agent.py --self-test
python scripts\run_agent.py --sdk-check
python scripts\run_agent.py --question "COPD急性加重患者使用全身糖皮质激素的推荐疗程是多久？"
python scripts\run_agent.py --trace-diagnose
```

真实回答涉及百炼 API，可能计费。只有 `--retrieval-smoke` 有命中、文本长度不为零时才建议执行最后的完整问答。

此项目仅供科研、医学文献检索及教学辅助，不用于个体化处方或临床决策。
