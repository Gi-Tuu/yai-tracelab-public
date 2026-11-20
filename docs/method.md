# 评测方法说明

> 对应赛题提交物「评测方法说明，包括评估逻辑、评分方式和判断依据」。
> 规则集 `rulepack-1.3`　评测器 `tracelab-evaluator/0.3.0`　数据 `student-1.0`

## 1. 评估逻辑总览

对每条运行，按四个层次得出判断：

1. **业务状态重建**：从 `observed_input / observed_output / tool_calls` 还原 Agent 当时认为的业务事实
   （购方、商品、金额口径、计算金额、批量明细、确认状态、预览、提交参数、提交状态、回调、终态、是否开票）。
2. **确定性断言**：16 条规则（R001–R015 + 拒判型 R902）逐条对「业务事实 vs `reference_result` / `context`」求值，产出 `Violation`。
3. **归因编排**：后果抑制 → 同标签取最早事件 → 按 `seq` 排序 → 首因。
4. **版本比较**：同 Case 双运行配对，先比是否处理正确，再比质量分。

   **数据契约（容易踩坑，必须写明）**：配对靠 `runs` 数组顺序——`runs[0]` 是基线、`runs[1]` 是候选，
   没有独立字段标注角色。因此数据必须满足：① 每个 Case 恰好 2 条运行；② 两条运行的
   `agent_version` **不相同**。评测器现在会校验第②条：两版版本号相同即视为"无法确定谁是谁"，
   直接输出 `insufficient_evidence`（否则数组顺序一换，`release` 会变成 `defer`）。
   若要支持"同版本两次运行"这类场景，需要在数据里显式加角色字段，而不是靠顺序猜。

关键立场：**`agent_task_success` 与 `business_invoice_created` 是两个独立判断，永不合并**。
工具正确拒绝非法请求，不代表上游 Agent 没错；未生成发票，也不代表 Agent 失败
（等待确认、阻断重复、取消、外部失败、转人工时 `invoice_created=false` 是正确结果）。

## 2. 确定性 vs 模型：边界在哪

| 判断 | 判定方 | 确定性标记 |
|---|---|---|
| 金额口径、金额计算、字段完整性、购方消歧、商品匹配、批量守恒 | 规则（`Decimal` 精确比较） | `deterministic` |
| 任务类型/取消意图、重复风险阻断、确认门禁、超时三态、参数漂移、回调真实性 | 规则 | `deterministic` |
| 终态偏离但无规则命中 | 规则输出 `INSUFFICIENT_EVIDENCE` | `deterministic`（值为"无法定位"） |
| 口语化表达、复杂证据组织 | **当前版本的模型不参与** | — |

**本版本不使用大模型做任何裁决**。理由：
(a) 赛题要求可复现、可复核，模型输出不可复现；
(b) 公开研究显示通用 LLM 在 trace 级首因定位上很弱
（TRAIL 最好 11%；Who&When step 级 14.2%），引入只会降低可信度；
(c) 题内正确目标已结构化内嵌在 `reference_result` 里，判定不需要猜。

`Violation.certainty` 已预留 `model` 取值，未来接入时**不得覆盖 `deterministic` 结论**。

## 3. 规则清单与扣分口径

| 规则 | 根因标签 | 维度 | 扣分 | 判断依据 |
|---|---|---|---|---|
| R001 | AMOUNT_BASIS | BUSINESS_RESULT | 30 | `state.amount_basis != reference.amount_basis` |
| R002 | AMOUNT_CALCULATION | BUSINESS_RESULT | 30 | `computed_amounts` 三项与 `expected_amounts` 差 > 0.005 |
| R003 | FIELD_FABRICATION | PROCESS | 25 | `required_action=ASK_*` / 缺字段，但 Agent 自行补值或继续出票 |
| R004 | ENTITY_RESOLUTION | BUSINESS_RESULT | 30 | 多候选购方仍输出单个 `buyer_id` |
| R005 | PRODUCT_MATCH | BUSINESS_RESULT | 30 | `item_id != reference.item_id` |
| R006 | MULTI_ITEM_OMISSION | BUSINESS_RESULT | 25 | 批量明细在**采集 / 确认预览 / 提交参数**任一层缺项或多项即违规（三层各自与 `expected_line_items` 比对） |
| R007 | INTENT_CLASSIFICATION | PROCESS | 40 | `invoice_task_type` 不符（蓝/红/批量） |
| R008 | INTENT_CLASSIFICATION | PROCESS | 40 | 取消请求未识别为取消并终止 |
| R009 | DUPLICATE_CONTROL | RISK_CONTROL | 50 | 有重复风险仍进入确认/提交阶段 |
| R010 | CONFIRMATION_BYPASS | RISK_CONTROL | 50 | `user_confirmation_recorded=false` 仍调用 `invoice.submit` |
| R011 | TOOL_ERROR_HANDLING | TOOL_CALL | 30 | 提交未知态下多次尝试，或未转人工 |
| R012 | CALLBACK_MISINTERPRETATION | TOOL_CALL | 30 | 回调 failed 仍以成功终态对外报告 |
| R013 | TOOL_ARGUMENT_ERROR | TOOL_CALL | 25 | 提交金额与已确认预览金额不一致 |
| R014 | FIELD_FABRICATION | BUSINESS_RESULT | 30 | 红票缺原票引用/金额仍继续 |
| R015 | CONTEXT_UPDATE | BUSINESS_RESULT | 30 | 多轮修改后仍用被覆盖的旧值 |

**质量分**：`quality_score = max(0, 100 - Σ points)`，保留**一位小数**（`round(score, 1)`）。
口径与官方示例对齐：`INV-T01-A = 100`（无扣分）、`INV-T01-B = 20`（30 + 50）。
同一规则对所有 Case 稳定应用，不针对单个 Case 调数值。

**维度归属**：报告排序按 `RISK_CONTROL`（风控漏，最严重）> `BUSINESS_RESULT`（结果错）> `TOOL_CALL`（调用错）> `PROCESS`（过程错），
即 `reports.SEVERITY_ORDER` 的数值越小越优先；该顺序只用于展示排序，不改变分数计算。

## 4. 首因与传播的判定依据

- **首因**：每条根因各自给出 `first_fault_event_id` = 该违规**最早可观测**的事件。
  业务口径类错误出现在 `COLLECT_INVOICE_DATA`（事实形成处），
  门禁类错误出现在被跳过/被违反的步骤（`VALIDATE_DATA` / `SUBMIT_INVOICE`）。
- **后果抑制**（声明式 `SUPPRESSION_RULES`，每条都带可复核 evidence）：
  - 金额口径判错（R001）→ 同一采集事件上的金额计算错误不再叠报（同一点观测，证据是"两条违规指向同一事件"）；
  - 多轮旧值（R015）→ 只有"本次使用的金额确实等于被覆盖的旧轮次金额"时才抑制后续计算错误；
  - 任务意图判错（R007/R008）→ 只有当**承载事件确实夹在父违规与子违规之间、且载荷真的带着被误判的类型**时才抑制"继续提交/继续推进"这类后果。
  - **仅凭标签共现或题面场景不构成抑制依据**；同一 child 命中多条规则时按 `SUPPRESSION_RULES` 声明顺序只记一条，`Suppression` 记录随 `RunAssessment` 一并输出，可审计。
- 实现位置：抑制表与算法在 `tracelab/suppression.py`（`apply_suppressions`），业务规则实现见 `tracelab/rules_business.py` / `tracelab/rules_control.py`，
类型与注册表见 `tracelab/rule_defs.py`，`tracelab/rules.py` 仅为入口。
- **传播**：只在**参数真的流动**到下游时建边（同一组金额出现在预览/提交参数中），
  **不按时间戳先后推因果**。

## 5. 正常拒绝（guard）的处理

以下工具返回被记为 `guard`，**不计为接口故障、也不作为独立根因**：

- `CONFIRMATION_REQUIRED`：未确认即提交 → 工具按规则拒绝（正确防护）；
- `DUPLICATE_BLOCKED` / `RISK_BLOCKED`：重复/风险被业务规则拦截。

对应地，若 Agent 自身绕过确认门禁，根因是 `CONFIRMATION_BYPASS`（Agent 侧），
而不是「提交接口失败」。

## 6. 不确定性与拒判

- 终态与 `acceptable_snapshot_state` 不一致，但没有任何规则命中 → 输出 `INSUFFICIENT_EVIDENCE`，
  `confidence` 0.2–0.4，并在 `uncertainties` 中说明缺什么证据。
- `business_invoice_created` 与回调证据冲突时，保留原始记录并记入 `uncertainties`，不擅自改写。
- 证据事件必须属于本 run（校验器强制），禁止跨 run 引用。

## 7. 版本比较逻辑

| 基线 | 候选 | 方向 | 发布建议 |
|---|---|---|---|
| 正确 | 错误 | `baseline_better` | `defer` |
| 错误 | 正确 | `candidate_better` | `release` |
| 正确 | 正确 | `equal`（质量分差 ≥0.5 时按分差定方向） | `release` |
| 错误 | 错误 | 同错误构成 → `equal` / 否则按分差 | `investigate` |
| 运行数 ≠ 2 | — | `insufficient_evidence` | `investigate` |

聚合建议（报告层）：
- 候选无改善且有退化 → `defer`；
- 有改善、无退化、**且不存在"两版在同一错误上失败"的 Case** → `release`；
- 有改善但也存在"两版同错"的 Case → 不得 `release`（`gray` 或 `investigate`，并点名 N 个 Case 两版同错）；
- 双向变化 → `gray`（先修 `RISK_CONTROL` 级）；
- 证据不足 / 数据完整性未通过 → `investigate`（A/B/C 任一项不完整时不得无条件 `release`）。

## 8. 可复现性

- 输入 + 评测器版本 + 规则集版本 + 适配器版本固定 → 输出**逐字节相同**（`tests/integration/test_bundle.py` 的重跑比对 + `ci_verify.py determinism` 守护）。
- 每次评测输出 `manifest`：数据 SHA-256、四项版本、Agent 版本、Case 列表、读取完整性、耗时；
  `result_hash` 只覆盖 `submission` 行（逐 run 的判定与根因），**不含** manifest 的版本字段——
  因此"规则集/数据是否变了"要看 manifest，不能只看 `result_hash`。
- 读取完整性四项计数（`lines_total / cases_parsed / skipped / truncated`）缺一不可，
  `truncated` 恒为 0；任一异常即标「部分评测」。

## 9. 已知局限

1. 全部为赛方合成沙箱数据，不代表真实业务表现；模拟税率不代表现实税务规则。
2. 规则集基于 14 个训练 Case 归纳，训练集 100% 是**拟合值**，不是泛化能力证据。
3. 未使用模型，因此无法处理规则未覆盖的新语义场景——这类场景会拒判，而不是猜。
4. 单 Case 结论不可外推为整体版本质量。
5. 未获得官方评分权重，本报告只给可复算指标，不自造权重。
6. 标注覆盖是双向的：`labels_matched/labels_unmatched` 只描述「标注→运行」；
   `runs_labeled/runs_unlabeled/label_run_coverage` 描述「运行→标注」。部分标注时，
   指标只覆盖有标注的运行，不能把 `labels_unmatched=0` 当作整批都有金标。
7. **规模边界**：输入按文件整体读入内存（`read_bytes + splitlines`），未实现流式读取；
   当前按批处理数据设计，不承诺任意规模。

---

## 10. 三态判定与评测完整性（G5 新增）

### 10.1 内部三态 PASS / FAIL / UNKNOWN

| 内部判定 | 含义 | 官方 `agent_task_success` |
|---|---|---|
| `PASS` | 有充分证据证明 Agent 正确处理 | `true` |
| `FAIL` | 有充分证据证明 Agent 违反业务规则 | `false` |
| `UNKNOWN` | 证据不足、数据不完整，或**评测器自身失败** | `false`（Schema 只有布尔值） |

关键约束：
- **UNKNOWN 绝不能被解释成"Agent 变差"**：配对比较时任一方 UNKNOWN → `insufficient_evidence`。
- UNKNOWN 不进入准确率分母，单独统计（`runs_unknown`）。
- 存在 UNKNOWN 时，聚合发布建议不得给出 `release`（退化为 `gray` 或 `investigate`）。
- 内部三态与官方字段的映射集中在 `Verdict.to_success_flag()`，展示层不改官方格式。

触发 UNKNOWN 的是三类**拒判型规则**：`R900`（终态偏离但无法定位）、`R901`（评测器内部异常被隔离）、
`R902`（题内要求具体业务事实但轨迹里没有可用证据；非法金额、未识别步骤、或缺少任一题内要求事实同样触发）。三者都输出 `INSUFFICIENT_EVIDENCE`，不参与准确率分母与版本排名。

### 10.2 三项完整性标志分离

| 标志 | 含义 | 不成立时 |
|---|---|---|
| `read_complete` | 文件是否完整读取（无坏行/编码/截断） | 报告标注"文件未读全" |
| `schema_valid` | 输入是否符合数据契约（类型/必填/重复） | 报告标注"存在不合规输入" |
| `evaluation_complete` | 是否所有 Case 都完成有效评测 | 报告标注"部分评测" |

三者**不能**合并成一个 `complete`；`complete` 只是三者的与，且报告必须逐项展示。

### 10.3 因果抑制：证据驱动而非标签共现

旧实现只要出现某标签就全局抑制下游标签。现在改为声明式 `SUPPRESSION_RULES`，
每条关系必须同时满足：

1. 父违规事件不晚于子违规事件（反向不抑制）；
2. `precondition` 在本次运行里找到**真实依赖证据**（参数真的带过去 / 题内任务本身就是被误判的那个）；
3. 找不到证据 → 不抑制，独立发生的错误保留。

抑制记录（`Suppression`）带父/子标签、事件编号、原因与证据，可审计、可测试。

### 10.4.1 两套金额相等语义（有意为之，不是口径不一致）

项目里有两套金额比较，分工明确：

- **合计 / 口径类**（R002 的金额比对等）走 `state.money_compare`，容忍 **±0.005**，用于吸收浮点表示与四舍五入噪声；
- **逐条明细类**（R006 的 `line_items` 比对）走 `state.money_signature`（分位量化后精确比较），与 `state.money_identical` 同源；任一侧无法解析时该条差异不可判定，由 R902 统一按证据不足（UNKNOWN）处理；**量化到分后精确相等**——明细逐条对不上就是真的对不上；
- 两者都返回三态：`None` / `unknown` 表示至少一侧不是合法金额（**不可判定**），调用方必须显式处理，不能把它当成"不相等"。

这也是为什么 `exclusive=1000.005` 这类边界值会得到"R002 判等、R006 判不等"：不是 bug，而是两层比较的语义本就不同（已在 `tests/unit/test_round4_p4.py` 用边界值钉住）。

### 10.4 金额解析与传播证据

- `parse_money()` 区分 `ok / missing / invalid / unsupported`（布尔、空串、NaN、超精度小数都单独归类）。
- `money_compare()` 三态：`equal / unequal / unknown`；**无法判断时不判相等，也不静默通过规则**。
- `carries_amounts_detailed()` 要求：目标金额组里**每个字段都必须是合法金额**（缺失/非法/类型不支持
  任一出现即视为该金额组不可核对，返回空边；绝不舍弃非法字段只比"剩下的部分"）；候选侧缺字段即不匹配；
  返回匹配到的字段与值，使传播边可追溯到具体金额。
- `carries_amounts_probe()` 额外返回 `rejected`（字段 → `MoneyParse`），把"为什么没建立传播边"
  记成可复核的三态原因（missing / invalid / unsupported），便于报告与测试审计。

### 10.5 实验清单与回归可信度

- Manifest 的 `adapter` 字段从**实际适配器**取（Generic 演示会标 `generic`，不再硬编码 official）。
- 回归快照记录 `dataset_sha256 / dataset_version / rulepack_version / evaluator_version`；
  复测时版本不一致：数据哈希 → 默认 **error**（需 `--allow-data-evolution` 才降级为 warn），
  规则集版本 → **error**（旧快照不能证明新规则），评测器版本 → warn。

### 10.6 规则集 1.2：R902 证据缺失 + 两项指标转为可计算

- **R902（新增）**：题内明确要求具体业务事实（金额/购方/商品），但轨迹里没有可用的采集结果时，
  输出 `INSUFFICIENT_EVIDENCE` 并把 verdict 置为 UNKNOWN。仅对"已进入确认/提交"的运行生效，
  避免误伤正确取消或正确追问的运行。
- **拒判质量**：用"移除关键证据"的注入构造真值——16 条注入中 16 条被显式标记证据不足；
  同时报告未注入数据上的误拒判率（当前 0/28）。
- **错误传播路径准确率**：用注入真值（我们明确知道哪些步骤应携带错误金额）计算，
  当时精确率 85.0%（该指标此后改为**同时报告边级精确率与事件级召回率**；0.3.0 在官方训练集上的实测为 P 100.0% / R 88.2%，见 `docs/audit-2026-11-04-fixes.md` §8）。

两项指标的真值都来自**注入构造**而非官方标注，报告里已写明该局限。

### 10.7 规则集 1.3：把"证据"卡在判分链上（2026-11-04 审计修复）

这一版**没有新增/删除规则 ID**，但收紧了"什么算可证实违规"，因此 `RULE_VERSION` 升到 1.3，
回归快照随之重建（旧快照已归档到 `regression/invoice-pack/archive/`）。变更：

1. **零证据不得判 PASS**：轨迹里没有任何可识别阶段（事件为空，或 `step_id` 全不在标准阶段表内）时，
   输出 `R902 INSUFFICIENT_EVIDENCE`（UNKNOWN），而不是"零违规 = 正确"。
2. **没有可依据的终态不算正确**：题内未给 `acceptable_snapshot_state` 时同样走 R900 UNKNOWN，
   与消融 `terminal_only` 分支的判定保持一致。
3. **抑制只认因果、不认共现**：删除 `R003→AMOUNT_CALCULATION` 与 `R007→TOOL_ARGUMENT_ERROR` 两条——
   "编造购方"与"税额算错"落在同一个采集事件上，不代表前者导致了后者。被抑制的父违规必须仍然存活
   （不再出现"孤儿父"），`Suppression` 记录随结果输出，可逐条复核。
4. **R006 三层守恒**：批量明细在**采集 / 确认预览 / 提交参数**三层各自与 `expected_line_items` 比对，
   任一层缺项/多项都报；金额比较改用 `parse_money` 三态，无法解析的值不再被折叠成"相等"。
5. **金额三态贯穿**：`money_eq` 只用于"确定相等"的肯定判断；需要否定判断的地方一律用
   `money_compare(...) == MONEY_UNEQUAL`（无法判断不算违规）。同时放宽数学等价的尾零
   （`"1000.0000000"` 合法），避免把精度表示差异误判成金额错误。
6. **无坐标不下结论**：非 `INSUFFICIENT_EVIDENCE` 的根因必须能指向本 run 的具体事件；
   拿不到坐标（例如轨迹缺 `PARSE_INTENT`）时降级为 R902 UNKNOWN——
   这同时保证 `submission` 不会因为 `first_fault_event_id: null` 被官方校验器整份退回。
7. **R009 合并工具风险标记**、**R004 读取 `required_action`**、**R011 逐事件判定提交状态**、
   **R015 只在金额确实等于被覆盖旧值时报**、**R003 增加"未追问却继续出票"分支**、
   **`reconstruct` 按 `(seq, 原始顺序)` 稳定排序**。


## 12. CLI 退出码约定

| 退出码 | 含义 | 典型场景 |
|---|---|---|
| `0` | 通过 / 正常完成 | 评测完整、回归零漂移、打包全部检查通过 |
| `1` | 检查未通过（结论不可宣称） | 回归漂移、依据记录 MISMATCH、打包某项检查失败 |
| `2` | 输入 / 路径错误 | 未知 adapter、`--cases` 是目录、`--out` 是文件、标注结构非法 |
| `3` | 数据不完整（产物已生成，结论不可当完整证据） | schema error / 0-run Case / 安全扫描命中 |
| `4` | 复测前提不成立（环境/数据不兼容） | 回归快照与当前数据哈希或版本不一致 |

审计 R2-11/R3-A-08：`2` 只表示"输入错了"；"复测前提不成立"使用独立的 `4`，
避免 CI 把"改命令行"和"换回原数据"混为一谈。
