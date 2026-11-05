# 独立评估：冻结 holdout 与第二标注者协议

> **当前状态：未完成。** 冻结与模板已就绪；第二标注者尚未产生独立标注，因此本文件
> **不包含任何 holdout 数值结果**，也不声称「问题已被独立验证」。
> 完成标注后由 `scripts/holdout.py report` 生成 `out/holdout/holdout-report.md`，
> 再把关键数字与分歧摘要誊写进本文件。

## 1. 要检验什么（预注册）

训练集（`02_公开训练轨迹`，14 Case / 28 运行）参与过规则归纳，因此**不能**用它的
100% 指标证明「问题成立」。这里检验的是一个窄问题：

> 规则集与三态口径在**未参与归纳**的轨迹上，是否仍能得到与独立标注一致的结论？

不检验、也不能宣称：隐藏测试集精度、优于某个外部工具、跨业务域泛化。

## 2. 冻结信息（先冻结、后标注、只跑一次）

| 项 | 值 |
|---|---|
| 数据文件 | `data/official/student-1.0/04_无标注验证轨迹.jsonl` |
| 数据 SHA-256 | `8db5c4a559b6b8dcb930fad315fcc93610f8de0563082afb91f0ee8d0900dc13` |
| 规模 | 8 Case / 16 运行 |
| 冻结清单 | `docs/holdout/freeze.json`（含 run 清单与哈希，不复制数据原文） |
| 评测器 | `tracelab-evaluator/0.3.0` |
| 规则集 | `rulepack-1.3` |
| 标注模板 | `docs/holdout/labels.template.jsonl`（16 条，字段为空） |

冻结后若数据文件哈希变化，`holdout check` 会拒绝继续（防止「标完再改数据」）。

## 3. 标注协议（必须由第二个人完成）

- **标注者边界**：标注者未参与 `tracelab/rules.py` 与 `tracelab/rulepacks.py` 的编写；
  标注时**看不到评测器输出**（盲标）。
- 对每条运行独立给出：`agent_task_success`、`business_invoice_created`、
  `root_causes[].label`、`first_fault_event_id`、`evidence_event_ids`。
- 每条记录必须填写 `annotator` 与 `annotator_independent: true`，否则 `check` 退出 3。
- 标注文件保存为 `docs/holdout/labels.jsonl`（模板另存，不覆盖模板）。
- **本队执行约定（2026-11-04）**：标注者为队长。他未参与规则实现（规则由 AI 实现、队长只审阅交付），
  标注时盲标（不看评测器输出）→ 记为 `annotator_independent: true`；但它是**队内独立、非外部第三方**，
  本文件不把它写成"外部评审"。
- 标注完成后**只跑一次**；若之后改规则，必须换一批未看过的数据重新冻结（旧哈希会被拒绝）。

## 4. 预注册指标与口径

| 指标 | 口径 |
|---|---|
| 任务成功率 | `agent_task_success` 与金标一致 / 可评估运行；UNKNOWN 不进分母 |
| 开票状态准确率 | `business_invoice_created` 与金标一致 / 可评估运行 |
| 首因事件 EM | 每个 `(Case, Run, Label, Event)` 四元组；金标首因为空 → 单列 undefined，不进分母 |
| 根因标签 macro-F1 | P/R/F1 按标签集计算；UNKNOWN 跳过 |
| 证据 ID 有效率 | `evidence_event_ids` 中真实存在于该 run 的比例 |
| UNKNOWN 比例 | `runs_unknown / (runs_scored + runs_unknown)`，单独报告 |
| `labels_unmatched` | 标注与运行的配对缺口，必须为 0 才可用于结论 |

同时给出 **Wilson 95% 置信区间**（`scripts/holdout.py report` 会打印）。n=16 的区间
会很宽，因此结论只能写成「与独立标注大体一致 / 存在若干分歧」，不能写成百分比精度。

## 5. 已存在但没有用的证据（明确划界）

- `data/sample` 与变形实验 42/42、18/18：验证**评测器自身**的行为稳定性，是**自造样本**，
  不能当作「真实问题成立」的证据。
- 训练集 1.0000：拟合值，不能外推。
- 消融实验：已按同一批可评估运行比较，分母不同的变体标「—（不可比）」；
  它回答「领域语义值多少」，不回答「问题是否成立」。
- **没有外部基准**：本仓库找不到可比的公开开票轨迹评测基准，因此不伪造「外部工具对比」；
  这一部分分数视为不可得。

## 6. 当前状态

| 步骤 | 状态 |
|---|---|
| 冻结数据哈希与运行清单 | ✅ `docs/holdout/freeze.json` |
| 生成空白标注模板 | ✅ `docs/holdout/labels.template.jsonl`（16 条） |
| 第二标注者独立盲标 | ✅ 16/16 条（2026-11-05 00:11，队长盲标，未看评测器输出） |
| `holdout check` 校验 | ✅ 数据哈希未变、标注完整、`annotator_independent=true` |
| `holdout report` 出数 | ✅ `docs/holdout/holdout-report.md` / `.json`（见 §9） |

## 7. 复现命令

**最省事的方式（不用敲命令）**：双击仓库根目录的 `标注-1-生成表格.cmd`，
在 Excel 里填 `是/否` 后保存，再双击 `标注-2-提交并出报告.cmd`，报告会自动打开。

```powershell
# 0) 生成一页式盲标工作表（只含轨迹，不含评测器结论）
python scripts/holdout.py worksheet `
  --cases "data/official/student-1.0/04_无标注验证轨迹.jsonl" --out .tmp/holdout-worksheet.md

# 1) 校验数据未被改动 + 标注完整 + 标注者声明独立
python scripts/holdout.py check `
  --cases "data/official/student-1.0/04_无标注验证轨迹.jsonl" `
  --labels docs/holdout/labels.jsonl

# 2) 生成预注册指标 + Wilson CI
python scripts/holdout.py report `
  --cases "data/official/student-1.0/04_无标注验证轨迹.jsonl" `
  --labels docs/holdout/labels.jsonl --out out/holdout

# 3) 把结果与分歧摘要誊写回本文件；分歧逐条记录「评测器错 / 标注歧义 / 规则缺口」
```

## 8. 不能得出的结论（写死在协议里）

- 不能把 holdout 比例当作隐藏集精度或版本排名依据；
- 不能声称「优于外部工具」——没有外部基准；
- 不能在改过规则之后继续用本批结果（必须重新冻结数据）。

## 9. 结果（2026-11-05，第一次也是唯一一次运行）

- 标注者：队长（未参与规则实现；盲标，未看评测器输出与规则结论）
- 标注文件：`docs/holdout/labels.jsonl`（SHA-256 `43024a8e2545899e4cad9328231cb40ec1b965bf3f613b5eac5320a3e79ba337`，仓库内 LF 规范化后的文件；生成时 CRLF 副本哈希为 `36f2a82…`，内容相同）
- 只标了两列：`agent_task_success`、`business_invoice_created`；`root_causes` 未标注（按 §3 的"最小诚实版"）
- 评测器：`tracelab-evaluator/0.3.0`，规则集 `rulepack-1.3`；数据哈希与冻结清单一致，`labels_unmatched=0`

| 指标 | 一致/分母 | 一致率 | Wilson 95% CI |
|---|---|---|---|
| 任务判定一致率（评测器 vs 独立标注） | 14/16 | 87.50% | [64.0%, 96.5%] |
| 开票状态一致率（评测器 vs 独立标注） | 16/16 | 100.00% | [80.6%, 100.0%] |
| UNKNOWN | 0/16 | 0.00% | — |
| 证据 ID 有效率（评测器内部） | — | 100.00% | — |
| 首因事件 EM / 根因 macro-F1 | — | 未标注（N/A） | — |

### 9.1 分歧逐条（不合并成一句一致率）

两处分歧都落在同一个 Case（`INV-V04`，场景"多轮修改后应使用最新金额"），且方向成对相反：

| Run | 题内目标 | 实际行为 | 评测器 | 标注者 | 会议结论 |
|---|---|---|---|---|---|
| `INV-V04-A` | 用第 2 轮最新金额 2500 预览并等确认（`WAITING_USER`，不出票） | 用了第 1 轮旧值 2000（`computed=2000/120/2120`） | FAIL，`CONTEXT_UPDATE` | `true` | 评测器与 `reference_result` 一致；标注疑似看反 A/B 行或把"改价后沿用旧值"当成正确 |
| `INV-V04-B` | 同上 | 用了 2500（`computed=2500/150/2650`） | PASS | `false` | 同上，成对反向 |

**处理**：按预注册"只跑一次、不回溯修改"，保留原始标注不改；这两条记为**标注歧义/疑似 A-B 行颠倒**，
不计为"评测器错误"。若后续要排除这一解释，只能在**新冻结的一批数据**上重做，不能在本批补标。

### 9.2 这批结果能支持什么

- 在**未参与规则归纳**的 8 Case / 16 运行上，评测器的任务级结论与独立盲标一致 **14/16**；
  开票状态 **16/16** 一致；没有出现"评测器把明显正确判错"的系统性偏差（至少任务/开票层）。
- 这是对"规则只在训练集上成立"这一质疑的**初步反证**：不是训练集 100% 那种拟合值。
- 明确边界：n=16、同队独立（非外部第三方）、根因层未标注；**不据此宣称隐藏集精度、根因有效性或外部基准优势**。

### 9.3 若要把根因层也补成证据

需要**另冻一批**未看过的数据（例如重新从 04 之外构造/选取），由同一或另一独立标注者标注
`root_causes`（label + 首因事件 + 证据事件），再按同一协议跑一次；不得在本次 16 条上补标根因。
