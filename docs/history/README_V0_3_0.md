# MedEvidence Agent V0.3.0 — 证据充分性驱动的检索决策（开发实验版）

**这是开发/研究版，不是已证明优于 V0.2.7 的算法。** 源于此前交付的 V0.2.7 补丁组合，并增加确定性的检索决策工具、审计轨迹与离线重放比较。用户本机的冻结基线可能经过手工调整，因此不宣称此包与其本机文件字节级一致。

## 安装（单独文件夹，绝不覆盖冻结版）

压缩包包含一个顶层目录 `MedEvidence_Agent_V0_3_0`。**把整个 ZIP 解压到 `E:\Download\Agent\MedEvidence`**，使目录如下：

```text
MedEvidence/
  MedEvidence_Agent_SDK_V0_2_.../    # 已冻结的 V0.2.7，保持不动
  MedEvidence_Agent_V0_3_0/          # 本版的独立开发副本
    agent/adaptive_policy.py
    agent/sdk_runtime.py
    scripts/run_agent.py
    scripts/compare_adaptive_policies.py
    tests/...
```

在 PowerShell 终端先进入项目根目录，按实际 ZIP 路径执行：

```powershell
cd E:\Download\Agent\MedEvidence
$zip = Get-ChildItem 'E:\Download',"$HOME\Downloads" -Filter '*V0_3_0*.zip' -File -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $zip) { throw '没找到 V0.3.0 ZIP，请检查下载路径' }
Expand-Archive -LiteralPath $zip.FullName -DestinationPath . -Force
cd .\MedEvidence_Agent_V0_3_0
```

将冻结版 **本机** 的 `.env.agent` 复制过来（只在本机操作，不上传/提交 GitHub）：

```powershell
$old = Get-ChildItem .. -Directory -Filter 'MedEvidence_Agent_SDK_V0_2_*' | Select-Object -First 1
if (-not $old) { throw '请手动指定冻结版 Agent 子文件夹' }
Copy-Item (Join-Path $old.FullName '.env.agent') '.\.env.agent'
```

> 配置优先读取当前独立文件夹的 `.env.agent`。保持现有 Qdrant `v3_2_trial_clean` 和 BM25 的只读路径，不创建索引。

## 验收（推荐按顺序）

```powershell
# 纯离线：不调用百炼、不读大型索引
python scripts\run_agent.py --self-test
python scripts\compare_adaptive_policies.py --demo

# 仅构建真实 SDK 组件图；不调用百炼
python scripts\run_agent.py --sdk-check

# 真实问答：会调用百炼 API，可能收费
$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION='python'   # 仅在之前仍有protobuf兼容问题时需要
python scripts\run_agent.py --question '对于成人2型糖尿病患者，SGLT2抑制剂和GLP-1受体激动剂的肾脏结局证据有哪些差异？'

# 读取这次 V0.3 的独立脱敏日志
python scripts\run_agent.py --trace-diagnose
```

> 早期 V0.2.7 的 SDK、网络、索引真实联合成功记录来自用户本机；本包仅在隔离环境进行了离线 Python 单测，**尚未真实访问用户本地 Qdrant/百炼**。请先跑离线检查，再进行付费 API 测试。

## 核心变化

新 `agent/adaptive_policy.py` 会对每轮证据计算：

- `R`: 被覆盖评估引用的来源块，占本轮已登记证据块的比例（使用率代理，并非真正的语义相关性）；
- `C`: 使用**白名单有效引用 ID**锚定的子问题占比；
- `V`: 具有一定长度非空原文锚点的子问题占比（结构性支持代理，**不是主张级语义蕴含验证**）；
- `K`: 文本证据中由现有覆盖评估器报告的冲突数，截断至 [0,1]；
- `S = clamp(0.20 R + 0.45 C + 0.35 V - 0.20 K)`（**仅为预实验初始设计，没有任何经验有效性结论**）。

决策存在硬门控：缺少有效引用、未覆盖的问题、非空冲突 -> 不允许直接认定“证据充分”；仍有检索预算则建议 `search_again`，预算耗尽则 `partial` 或 `abstain`。连续无新增证据可提前停止。`S` 不可解释为“医学答案有多少概率正确”。最终医学主张仍需通过 V0.2.7 的 CitationVerifier；输出的 `answered` 还要经过 V0.3 决策门控。

SDK 新工具：`decide_next_evidence_action`，模型可在 `assess_evidence_coverage` 后调用，获得下一步行动和当前缺口。实际的具体查询词仍由 Agent 生成，因此真实提升尚需量化。

独立运行轨迹位于 `MedEvidence_Agent_V0_3_0/outputs/agent_runs_v030/`。默认不保存原问题或原文，但请注意**发给模型供应商的请求仍会包含检索证据原文，且本地 .env.agent 含密钥**。

## 对照实验初版（脱机）

```powershell
python scripts\compare_adaptive_policies.py --demo
python scripts\compare_adaptive_policies.py --input my_frozen_cases.jsonl --output outputs\policy_replay.json
```

三组：`single_rag`（1 轮）、`fixed_two_round`（固定 2 轮）、`adaptive`（基于每轮已有证据决定）。`--demo` 的三道题是**合成案例**，仅验证代码行为；不能当作论文实验结果。

自定义 JSONL 每行一个 case，见 `experiments/case_schema_example.jsonl`。输入为**按时间顺序冻结的各轮搜索快照**，未来轮不能参与过去决策。真实公平实验还需要：同一文献索引/模型、同一召回预算、先冻结 dev/test、独立人工核验主张级证据支持与拒答、请求成本及检索时延记录。纯快照 replay 只能比较策略逻辑，不等于控制了不同策略生成不同查询所带来的差异。

## 接下来真正要做的研究

1. 从既有 Pilot/Dev 题中筛出 30–50 条跨疾病问题，固定研究范围、标签标准、索引指纹及模型参数（不以 Test 调参）；
2. 离线核验文献证据覆盖/引用支持/真实冲突，测量对评分与门限的校准；
3. 在共享候选池及一致检索/生成设置下进行线上算法对照，做误差分析与置信区间。

**不应把现在的 100% 启发式覆盖率宣称为答案正确率，也不能因为装了 Agent SDK 就宣称算法有原创性或性能提升。**
