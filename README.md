# YAI TraceLab

> 证据驱动、业务规则感知的 AI 开票 Agent 离线轨迹评测工具。
> 从结构化运行轨迹判断执行结果、定位首个错误、比较版本变化；零第三方运行时依赖，离线可复现。

```bash
python -m tracelab demo                 # 打开即用体验页（自造示例数据）
python -m tracelab evaluate --cases data/sample/sample-traces.jsonl --out out/sample
python -m pytest tests/unit -q          # 公共单元测试
```

---

# YAI TraceLab

**Evidence-Grounded Business Agent Evaluation**
面向业务 Agent 的证据驱动、业务规则感知、可解释执行轨迹评测系统。

> 赛题：云帐房 AI 技术创新大赛 · 赛题三「AI 开票 Agent 离线轨迹评测」
> 口号：**We evaluate business correctness, not just task completion.**

TraceLab 评测的对象是**评测程序本身要给出的判断**，不是重新实现开票 Agent。它读取离线轨迹文件，回答四个问题：

1. Agent 是否正确处理了业务请求？（与"发票是否真正生成"分开报告）
2. 整条轨迹里**最早可证实的错误**是什么？证据是哪几个事件？
3. 错误如何传播到后续工具调用与业务状态？
4. 新版本改善了什么、退化了什么？

---

## 30 秒跑起来

```bash
# 无第三方依赖，Python >= 3.11
python -m tracelab evaluate \
  --cases  data/official/student-1.0/02_公开训练轨迹.jsonl \
  --labels data/official/student-1.0/03_公开训练参考标注.jsonl \
  --out    out/train

# 无标注验证集
python -m tracelab evaluate \
  --cases data/official/student-1.0/04_无标注验证轨迹.jsonl \
  --out   out/valid
```

产出 `out/<name>/`：

| 文件 | 内容 |
|---|---|
| `submission.jsonl` | 官方提交格式，每行一个 Case |
| `results.json` | 机器可读的完整结论（含规则 ID、证据事件、传播边、不确定性） |
| `report-official.md` | **按官方《版本评测报告模板》7 节生成**（A/B 分列指标、场景簇、证据链、发布建议） |
| `report.md` | 综合分析报告（业务+研发+产品三视角合一） |
| `report-business.md` | 业务版：结论、影响面、优先处理场景、**不能得出的结论** |
| `report-engineering.md` | 研发版：规则命中、证据链、拒判清单、复现命令 |
| `report-product.md` | 产品版：能力维度分布、版本方向矩阵、改进优先级 |
| `metrics.json` | 仅在提供 `--labels` 时生成：主指标 6 项 + **补充指标 5 项（三态：已计算/结构性成立/不可评估）** |

本地格式校验（官方工具）：

```bash
python data/official/student-1.0/08_提交格式校验.py \
  --cases data/official/student-1.0/04_无标注验证轨迹.jsonl \
  --submission out/valid/submission.jsonl
```

其他命令：

```bash
python -m tracelab ablate \
  --cases  data/official/student-1.0/02_公开训练轨迹.jsonl \
  --labels data/official/student-1.0/03_公开训练参考标注.jsonl \
  --out    out/ablation                # 消融实验：领域语义规则值多少准确率
python -m tracelab regression build \
  --cases data/official/student-1.0/02_公开训练轨迹.jsonl --out regression/invoice-pack
python -m tracelab regression verify \
  --pack regression/invoice-pack/regression-pack.json \
  --cases data/official/student-1.0/02_公开训练轨迹.jsonl --out out/regression-verify
# 异构格式：同一套规则跑完全不同的输入结构
python -m tracelab evaluate --adapter generic --cases data/demo/generic-traces.jsonl --out out/generic
python scripts/build_submission_bundle.py --out dist
# ↑ 生成提交包 + 逐项自检清单（任一项失败即非 0 退出）
#   默认不打包 data/official/ 与 regression/；确需随包交付时显式开启对应参数
python -m tracelab mutate --cases data/official/student-1.0/02_公开训练轨迹.jsonl --out out/mutation
# ↑ 缺陷注入实验：自己造真值，量化回归发现率（含漏检原样报告）
python -m tracelab experiments --out out/experiments
# ↑ 变形实验：语义保持 + 业务变化（**不需要官方资料包**；固定种子，两次运行逐字节一致）
#   产出 report.md（人读）+ report.json（机读：分子/分母、覆盖规则、样本摘要）
python -m tracelab rules               # 列出规则集（规则 ID / 前置条件 / 断言）
python -m tracelab manifest --cases …  # 输出实验清单（数据哈希、版本、配置）
python -m pytest tests/unit -q          # 612 项公共单元测试（不依赖官方数据；Windows 上 0 skip，
                                        #  Linux 上仅 2 项 Windows 路径语义用例按平台跳过，CI 会逐条登记）
python -m pytest tests -q               # 752 项全量（含需官方数据的集成测试）
```

CI（GitHub Actions）四类作业 + 两层门禁自检：`lint + test`（3.11/3.12，含 ruff/mypy/覆盖率）、
`Windows 兼容`（3.11/3.12）、`回归与注入门禁`、`官方校验器联调 + 可复现性`（ubuntu + windows）；
每个作业都会自检「用例数下限 / 跳过必须登记（nodeid 白名单 + 上限）/ 覆盖率真的跑了（读本次 coverage.json）」。

**覆盖率口径分两个独立门槛**（不是一个数字糊住全部代码）：
- `tracelab/`（评测器本体）：branch 口径门槛 **80%**（实测值见 CI 日志的 `--cov-report=term`）；
- `scripts/`（发布闸门与数据工具）：门槛 **45%**（实测值见 CI 日志），单独数据文件、单独断言（不与 tracelab 合并口径，否则会被高覆盖率掩盖）；
- 门禁自检还会断言 `num_statements ≥ 3300` 与 tracelab 模块数 ≥ 15，防止用 coverage `omit` 把模块从报表里拿掉。

ubuntu 与 **windows** 双平台跑「原始数据哈希校验 → 两套评测 → 官方校验器 → 指标回归门禁 → 重跑逐字节一致」。

---

## 快速体验（打开即用）

1. 双击 **`启动体验.cmd`**（或命令行 `python -m tracelab demo`）。
   会自动用自带自造样例跑一次完整评测，并在浏览器打开 `out/demo/demo.html` 体验页。
2. 体验页里能直接看到：逐 Case 版本对比、PASS/FAIL/UNKNOWN 三态、根因标签、首因事件与证据事件编号；
   同一目录下有完整报告：`report-official.md`（官方 7 节）、`report.md`、业务/研发/产品三份受众报告。
3. 想自己复跑：

   ```powershell
   python -m tracelab evaluate --cases data/sample/sample-traces.jsonl --out out/demo
   python -m pytest tests/unit -q
   ```

4. 使用赛事数据时：

   ```powershell
   python -m tracelab evaluate --cases "data/official/student-1.0/02_公开训练轨迹.jsonl" --labels "data/official/student-1.0/03_公开训练参考标注.jsonl" --out out/train
   python scripts/ci_verify.py metrics --dir out/train   # 期望六项全 1.0000
   ```

> 体验页使用仓库内置示例数据；页面不含时间戳与耗时，可离线打开。

## 为什么不是"又一个通用 Agent Benchmark"

通用 Trace 工具（Langfuse / Braintrust / Phoenix 等）已经覆盖了 trace 可视化、LLM-as-judge、数据集与实验对比。**TraceLab 的差异化不在定位算法，而在领域可判定性：**

| 开票业务约束 | 通用工具的处理 | TraceLab 的处理 |
|---|---|---|
| 含税/不含税口径必须保持 | 只能看文本像不像 | `Decimal` 恒等式闭合校验（R001/R002） |
| 购方多候选必须消歧 | 看不出来 | 候选集 vs 实际选择的确定性比对（R004） |
| 确认前置：结果对但过程违规 | 计入"成功" | 确认记录作为硬门禁单独计分（R010） |
| 重复开票需跨轨迹台账 | 单条 trace 原理上做不到 | 用 context 风险标记做前置阻断断言（R009） |
| 红票必须引用原票 | 无反票语义 | 原票引用可达性断言（R014） |
| 提交超时是**三态**（成功/失败/未知） | 未知常被计失败 | 三态建模 + 重试依据断言（R011） |
| 批量条目守恒 | 忽略字段完整性检查 | 多重集合守恒断言（R006） |

此外三条方法论主张：

- **正常拒绝 ≠ 故障**：工具对未确认提交/重复开票的保护性拒绝被记录为 `guard`，不记为接口错误、也不作为独立根因。
- **首因 ≠ 传播后果**：传播链只在参数**真的流动**到下游时建立（金额从预览流到提交参数），不按时间戳先后推因果。
- **允许拒判**：证据不足时输出 `INSUFFICIENT_EVIDENCE`，不生成貌似精确的虚构根因。

---

## 架构

```
data/official (只读原始轨迹)
   │  adapters.official      ① 接入：官方 schema → Canonical Trace IR（保留 raw，不丢事件）
   ▼
tracelab.contracts / state   ② 重建：事件流 → 业务事实（购方/商品/金额口径/门禁/提交/回调）
   │
   ├─ rules (16 条确定性规则) ③ 判定：业务语义断言 → Violation（规则 ID + 证据事件 + 确定性）
   │
   ├─ attribution            ④ 归因：首因 / 传播 / 正常拦截 / 不确定性 / 质量分
   │
   └─ evaluation + reports   ⑤ 编排：批量评测 + 双版本配对 + 实验清单 + JSON/Markdown 报告
```

设计取舍见 [`docs/architecture.md`](docs/architecture.md)。

---

## 当前实测结果

| 数据集 | Case / 运行 | 处理正确 | 实际开票 | 官方格式校验 |
|---|---|---|---|---|
| 公开训练 `02` | 14 / 28 | 14 / 28 | 6 / 28 | ✅ 通过 |
| 无标注验证 `04` | 8 / 16 | 9 / 16 | 6 / 16 | ✅ 通过 |

> **必须打折看**：训练集上的 100%（任务成功、开票状态、版本方向、首因 Exact Match、标签 macro-F1、证据有效性）是**规则由该数据集归纳出的拟合值**，不是泛化能力的证据。验证集无专家标注，只能做规则级复核；真正的外推检验需要隐藏测试集或扩充标注样本（n=14 时单例 7.1pp，统计推断不可用）。

与官方给出的 `INV-T01` 示例逐字段一致（A: 100 分 / 无扣分；B: 20 分 / `AMOUNT_BASIS`+`CONFIRMATION_BYPASS`；版本方向 `baseline_better`；发布建议 `defer`）。

### 消融实验：领域语义到底值多少（`python -m tracelab ablate`）

同一批数据、同一评测器，只改规则集：

| 变体 | 任务正确率 | 可评估运行 | 首因 EM | 版本方向准确率 | 漏判（真值错→判对） | 静默漏报回归 |
|---|---|---|---|---|---|---|
| `full`（全部 16 条规则） | 100.0% | 28 | 100.0% | 100.0% | 0 | 0 / 14 |
| `terminal_only`（只看终态） | 71.4% | 28 | 0.0% | 42.9% | 8 | 8 / 14 |
| `generic_health`（通用健康检查） | 75.0% | 28 | 40.0% | 50.0% | 7 | 7 / 14 |
| `business_only`（领域语义规则） | 95.8% | **24**（另 4 条 UNKNOWN） | 81.8% | 90.0% | 1 | 1 / 14 |

读法（**注意分母**）：
- **通用健康检查（确认门禁/重复风险/超时/回调/参数漂移）只比"只看终态"高 3.6 pp** —— "Agent 有没有按协议办事"抓不到业务口径。
- **领域语义规则（95.8%）与通用健康检查（75.0%）不可直接相减**：`business_only` 去掉通用规则后有 4 条运行判 UNKNOWN、被移出分母（24 vs 28），差值混入了"拒判口径变化"，只能作方向性参考。这也是消融报告的**可证伪判据**：只有同分母时才计算差值（`—（分母不同，不可比）`）。
- **只看终态会漏掉 8/14 的版本变化**，因为"结果对但过程违规"的 Case 两版终态相同。
- `business_only` 的首因 EM 为 81.8%（低于 full 的 100%）：判对成败 ≠ 定位到哪一步错了。

完整数据见发布包中的消融报告。


### CLI 退出码约定（`tracelab` 与打包脚本）

| 退出码 | 含义 | 典型场景 |
|---|---|---|
| `0` | 通过 / 正常完成 | 评测完整、回归零漂移、打包全部检查通过 |
| `1` | 检查未通过（结论不可宣称） | 回归有漂移、依据记录 MISMATCH、提交包某项检查失败 |
| `2` | 输入 / 路径错误（未改动或未写入文件） | 未知 adapter、`--cases` 是目录、`--out` 是文件、标注结构非法 |
| `3` | 数据不完整（已产出，但结论不可当完整证据） | 存在 schema error / 0-run Case / 安全扫描命中；`--allow-incomplete` 可放行前两类 |
| `4` | 复测前提不成立（环境/数据不兼容） | 回归快照与当前数据哈希/版本不一致（需 `--allow-data-evolution` 或换数据） |

> `2` 只表示"输入错了"，不再与"环境不兼容"共用；复测前提不成立已拆到 `4`。
---

## 目录

```
tracelab/            contracts / adapters / state / rules / attribution / evaluation / ablation / regression / official_report / reports / cli
                     + metamorphic / experiments（G6 稳定性实验）+ evidence_record / evidence_verify（G6 可复核证据）
                     + rulepacks（G6 规则包接口）+ pack_report（规则包无关的版本对比与报告）
tests/unit/          612 项公共单测（规则/三态/金额/契约/打包安全/发布检查）
tests/integration/   140 项集成测试（需官方资料包：指标、官方校验器、报告模板、消融、注入）
data/official/       官方训练数据 + SHA-256 清单（只读）
docs/                architecture.md、method.md、g0/（G0 审查证据）、design-v0.md（历史）
                     + g6-method.md / g6-evidence.md / g6-rulepacks.md（G6 三个能力的方法与边界）
                     + submission-index.md（作品索引：交付物 / 评审维度 / 加分项 → 文件与命令）
scripts/             G0 数据剖析 + CI 验证脚本
out/                 评测产出
```

> 第一次看这个仓库？先读 [`docs/submission-index.md`](docs/submission-index.md)：它把项目交付物、
> 功能模块与可执行命令映射到具体文件，便于逐条核验。

## 业务规则扩展（G6-C：已实现 vs 仅预留，别混着宣传）

| 能力 | 状态 | 说明 |
|---|---|---|
| 开票规则包（16 条，R001–R015 + R902） | **COMPLETE** | 既有实现，行为未变；经 `rulepacks.INVOICE_PACK` 以"委托"方式接入统一引擎，结果逐字段一致 |
| 规则包接口（身份/版本/场景/事实抽取/规则函数/结果与证据映射） | **COMPLETE** | `tracelab/rulepacks.py`；两个包共用同一评测入口与同一输出结构 |
| 第二个业务规则包（报销单审核，4 条规则） | **PROTOTYPE** | 100% 自造场景，**不接真实财务系统**；只覆盖金额一致/预算/审批顺序/重复结算四条 |
| 规则包无关的版本对比与报告（方向 / 聚合建议 / 逐 Case 表 / UNKNOWN 与"两版同错"口径） | **COMPLETE** | `tracelab/pack_report.py`；开票包走同一层时方向与既有 `compare_versions` 一致 |
| 报销规则的可复核依据记录 | **COMPLETE（原型数据）** | `evidence_record.build_expense_records` + 同一独立检查器（EXP001–EXP004） |
| 异构 JSON 输入 | **COMPLETE** | 既有 Generic Adapter |
| 声明式规则 DSL / 跨包首因与传播 / 规则包版本迁移 | **未实现（仅预留）** | 见 `docs/g6-rulepacks.md` §5 |

## 局限（与结论同等重要）


1. 全部数据为赛方合成沙箱数据，不代表真实业务系统表现。
2. 规则集 `rulepack-1.3` 基于 14 个训练 Case 归纳，未见场景依赖 adapter 与规则扩展，
   而非"自动泛化"；未覆盖场景会显式拒判而不是猜。
3. 未实现 LLM 语义辅助层（刻意）：初赛要求纯离线、不强制模型调用，
   模型接入点预留但默认关闭，避免把"模型自信度"当成"结论可靠性"。
4. 单 Case 结论不可外推为整体版本质量。
5. **规模边界**：当前实现按文件整体读入内存（`read_bytes + splitlines`），适用于批处理级数据；
   超大文件需要流式读取改造（未实现），不要假设可处理任意规模。

## 反馈与问题

- **Issue 跟踪**：<https://github.com/Gi-Tuu/yai-tracelab-public/issues>。
- 提交问题时请附：完整命令、操作系统与 Python 版本、`python -m tracelab --version`、
  `python -m tracelab manifest --cases <文件>` 的输出，以及**脱敏后**的最小轨迹片段。
  请勿粘贴真实客户信息或未获授权的内容。

- **独立评估边界**：训练集指标是拟合值，不能证明「问题成立」；`docs/independent-eval.md` 记录了冻结 holdout 与独立盲标结果（16 条：任务级一致 14/16、开票一致 16/16；根因未标注、n 小，不宣称泛化）。

## 许可

本项目代码 MIT。`data/official/` 内容版权归赛事主办方，仅用于本次竞赛。









---

## 开发流程（分支保护 + 自动合并）

`main` 是受保护分支：**不允许直接 push**，必须走 PR 且通过必需检查（实际配置以
[`.github/branch-protection.json`](.github/branch-protection.json) 为准，与 GitHub 上一致）。

```bash
git checkout -b <feature-branch>
# …改动…
git add -A && git commit -m "<message>"
git push -u origin <feature-branch>
gh pr create --title "<title>" --body "<body>"

# 开启自动合并：检查全绿后自动 squash 合并并删分支
gh pr merge <number> --squash --delete-branch --auto
```

- 必需检查（7 条）：`lint + test (py3.11)`、`lint + test (py3.12)`、
  `Windows 兼容 (py3.11)`、`Windows 兼容 (py3.12)`、`回归与注入门禁`、
  `官方校验器联调 + 可复现性 (ubuntu-latest)`、`官方校验器联调 + 可复现性 (windows-latest)`
- 直推 main 会被服务端拒绝（`GH006: Protected branch update failed`）
- `--auto` 的作用：即使 runner 排队或卡住，检查一旦全绿就自动完成合并，不需要人守着
- 需要立即合并时不要用 `--admin` 绕过规则——那等于绕过门禁
