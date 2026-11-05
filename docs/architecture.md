# TraceLab 系统架构（G0 校准版）

> 状态：已在 G1–G4 落地并可运行（`rulepack-1.3` / `tracelab-evaluator/0.3.0`）。
> 本文件替代 `docs/design-v0.md` —— 那份是**尚未拿到官方资料包时的推测稿**，其中数据契约一栏已被实测字段全部推翻。

## 一、边界

TraceLab **只做评测，不做开票**。输入是离线轨迹文件，输出是判定、归因、版本比较与报告。
不联网、不调用真实开票接口、不需要模型 API Key 即可完成全部基础评测。

它评测的对象是「**评测程序给出的判断是否正确**」，不是重新实现被评测的 Agent。

## 二、五层结构

```
① 接入 adapters      Official JSONL → Canonical Trace IR
② 重建 state         事件流 → 业务事实（不是事件流本身）
③ 判定 rules         业务语义断言 → Violation（可独立单测）
④ 归因 attribution   首因 / 传播 / 正常拦截 / 不确定性 / 质量分
⑤ 编排 evaluation    批量评测 + 双版本配对 + 实验清单 + 报告
```

### ① 接入层 `adapters.py`（入口；实现见 `adapters_common` / `adapters_official` / `adapters_generic`）

- `Adapter` 协议只要求 `load(path) -> Dataset`；`REGISTRY` 注册，**新 Agent 接入不需要改规则引擎、归因层或报告层**（对应赛题加分项「低成本接入新 Agent」）。
- 规范化**只做索引不删字段**：`Case.raw` / `Run.raw` / `Event.raw` / `ToolCall.raw` 完整保留原始对象。
- 实现拆分：`adapters_common.py`（公共读取与字段校验）、`adapters_official.py`（官方 schema）、
  `adapters_generic.py`（异构 Agent）；`adapters.py` 只保留 `REGISTRY` 与 `load_dataset`。
- **不设任何读取上限**。`Dataset.stats` 显式输出 `lines_total / cases_parsed / skipped / truncated / runs_total / events_total`；
  `truncated != 0` 或 `skipped != 0` 即判为部分评测并置 `complete=false`。
  （这一条是对 Scout 分页教训的吸收：它修掉了 500 条静默截断，但没有实现「总量 vs 已读」报告，我们用计数补上。）

### ② 重建层 `state.py`

把事件流还原为 `BusinessState`：任务类型、意图、购方、商品、金额口径、计算金额、批量明细、
校验结论、风险标记、确认状态、预览金额、提交参数、提交状态、回调状态、终态、是否实际开票。

这是与通用 Trace 工具的分界线：通用工具看到 `step_status=SUCCEEDED`，
我们看到「金额口径从 `tax_exclusive` 变成了 `tax_inclusive`，且这个错误金额流进了提交参数」。

金额统一走 `Decimal` + `ROUND_HALF_UP`，禁止 float 直比（题内规则明确要求十进制四舍五入）。

### ③ 判定层 `rules.py`（入口；实现见 `rule_defs` / `rules_business` / `rules_control` / `rule_engine`）

16 条规则（R001–R015 + 拒判型 R902），每条带 `rule_id / rule_version / precondition / assertion / severity(dimension) / evidence_mapping`，
且**每条都能脱离官方数据被独立单测**（见 `tests/unit/test_rules.py`）。

| 规则 | 根因标签 | 维度 | 扣分 | 断言要点 |
|---|---|---|---|---|
| R001 | AMOUNT_BASIS | BUSINESS_RESULT | 30 | 含税/不含税口径与题内一致 |
| R002 | AMOUNT_CALCULATION | BUSINESS_RESULT | 30 | 口径对但金额/税额算错 |
| R003 | FIELD_FABRICATION | PROCESS | 25 | 缺字段不得编造（含口径未说明） |
| R004 | ENTITY_RESOLUTION | BUSINESS_RESULT | 30 | 多候选购方必须消歧 |
| R005 | PRODUCT_MATCH | BUSINESS_RESULT | 30 | 商品必须匹配 |
| R006 | MULTI_ITEM_OMISSION | BUSINESS_RESULT | 25 | 批量条目多重集合守恒 |
| R007 | INTENT_CLASSIFICATION | PROCESS | 40 | 蓝/红/批量任务类型识别 |
| R008 | INTENT_CLASSIFICATION | PROCESS | 40 | 取消意图识别并终止 |
| R009 | DUPLICATE_CONTROL | RISK_CONTROL | 50 | 重复风险必须阻断 |
| R010 | CONFIRMATION_BYPASS | RISK_CONTROL | 50 | 确认前置门禁 |
| R011 | TOOL_ERROR_HANDLING | TOOL_CALL | 30 | 超时未知态不得无依据重试 |
| R012 | CALLBACK_MISINTERPRETATION | TOOL_CALL | 30 | 以回调为准，话术不能覆盖结果 |
| R013 | TOOL_ARGUMENT_ERROR | TOOL_CALL | 25 | 提交参数不得相对预览漂移 |
| R014 | FIELD_FABRICATION | BUSINESS_RESULT | 30 | 红票须核对原票引用 |
| R015 | CONTEXT_UPDATE | BUSINESS_RESULT | 30 | 多轮修改须用最新值 |

规则实现拆分：`rule_defs.py`（类型/注册表）、`rules_business.py`（R001–R008）、
`rules_control.py`（R009–R015、R902）、`rule_engine.py`（执行/去重/兜底）；`rules.py` 仅入口。
公开 API 约定：各入口模块的 `__all__` 是稳定接口，规则函数与解析 helper 属实现细节。

**后果抑制是声明式表 `SUPPRESSION_RULES`**（不是旧的 `SUPPRESSED_BY` 标签映射），是「首因 vs 传播」的落地方式：
每条关系都必须同时满足"父违规不晚于子违规"和"`precondition` 找到真实依赖证据"，否则不抑制。
实现位于 `tracelab/suppression.py`（`apply_suppressions`）；规则类型与注册表在 `tracelab/rule_defs.py`，
规则实现在 `tracelab/rules_business.py`（R001–R008）与 `tracelab/rules_control.py`（R009–R015、R902），
`tracelab/rules.py` 只是入口。
任务意图判错时，只有承载事件确实夹在父与子之间、且载荷真的带着被误判的类型，才把后续动作视为后果；
口径判错只有在同一点观测时才不再叠报计算错误。**仅凭标签共现不构成抑制依据**；
同一 child 命中多条时按声明顺序只记一条，`Suppression` 记录随结果输出，可审阅、可测试。

**模型不参与这里的任何裁决**。这是对「不应给出貌似精确的虚构根因」最硬的防线，
也是对外可声明的方法边界：所有硬结论都是 `certainty=deterministic`。

### ④ 归因层 `attribution.py`

- **首因**：按 `seq` 取最早的违规事件，逐标签给出 `first_fault_event_id`。
- **传播**：只在参数**真的流动**到下游时建边（同一组金额出现在预览/提交参数里），
  不按时间戳先后推因果。
- **正常拦截**：`CONFIRMATION_REQUIRED` / `DUPLICATE_BLOCKED` 等保护性拒绝记为 `guard`，
  不计为接口故障、也不作为独立根因。
- **不确定性**：终态偏离但无规则命中 → `INSUFFICIENT_EVIDENCE`（confidence 0.2–0.4），并列出缺什么证据。
- **质量分**：`100 - Σ(可证实问题扣分)`，公开口径，同规则稳定应用于全部 Case。

### ⑤ 编排层 `evaluation.py` + `reports.py`（版本比较 / 指标 / 汇总分别见 `version_compare.py` / `metrics.py` / `summary.py`）

- 双版本配对：先比 `agent_task_success`，再比质量分；不可比时输出 `insufficient_evidence`，不给误导性排名。
- **实验清单**：评测器版本、IR 版本、适配器版本、规则集版本、数据 SHA-256、规模、读取完整性、Agent 版本、
  Case 列表、结果哈希（同输入同版本必须逐字节一致，有测试守住）。
- 产出：`submission.jsonl`（官方格式）/ `results.json` / `report.md` / `metrics.json`（仅在有标注时）。

## 三、为什么不做 Web / 不做 LLM 兜底

| 决策 | 理由 |
|---|---|
| CLI 优先，无 Web | 初赛交付物是"可运行离线评测程序"；Web 会挤占 P0 规则质量的时间 |
| 默认不调用模型 | 赛题要求纯离线、不强制模型；且模型自信度 ≠ 结论可靠性 |
| 不引入 SQLite | 单次离线批量评测没有持久化需求，引入即增加迁移/一致性负担 |
| 不复制 Scout 代码 | 抽象可借鉴、实现不可整体复制（其 `store.py` 存在列值不匹配的死代码） |

## 四、扩展点

1. **新数据格式**：实现 `Adapter.load` + `register_adapter`，零改动下游。
2. **新 Agent / 新场景**：只加规则（`@rule` 装饰器）+ 单测；规则集版本号递增以保持结果可比性。
3. **模型语义辅助**：`Violation.certainty` 已预留 `model` 取值，接入后仍不得覆盖 `deterministic` 结论。
4. **回归闭环**：`results.json` 的 `case_id / rule_id / first_fault_event_id` 可直接沉淀为回归用例。

---

## 五、G5 后的三处结构性修正

### 1. 判定层产出三态而不是布尔值

```
rules → violations → _classify() → Verdict{PASS, FAIL, UNKNOWN}
                                   │
                                   ├─ 官方 Schema：Verdict.to_success_flag() → bool
                                   └─ 内部统计/比较：一律用 Verdict
```

`compare_versions` 遇到任一方 `UNKNOWN` 直接返回 `insufficient_evidence`；
`score_against_labels` 把 UNKNOWN 排除出分母；`aggregate_recommendation` 在存在 UNKNOWN 时不给 `release`。
这条改动的原因很具体：旧实现里 R901（评测器异常）也会写成 `agent_task_success=False`，
于是"评测器自己崩了"会被读成"候选版退化"。

### 2. 抑制关系从"标签表"变成"证据表"

旧：`SUPPRESSED_BY = {父标签: {子标签}}` —— 只要标签出现就抑制，无法解释。
新：`SUPPRESSION_RULES = [SuppressionRule(parent_rule_id, child_label, reason, precondition)]`
—— 每条都必须通过"时间顺序 + 真实依赖证据"双检，并留下 `Suppression` 审计记录。

### 3. 完整性从"一个布尔"变成"三项标志"

```
read_complete   ← 文件读全了吗（坏行/编码/截断）
schema_valid    ← 符合数据契约吗（类型/必填/重复）
evaluation_complete ← 每个 Case 都评完了吗（无降级、run 数匹配）
complete = 三者与   ← 报告必须逐项展示，不允许只报一个 complete
```

适配器侧的强化：非法 `seq`/`retry_count`/`duration_ms` 不再抛异常崩整批，
而是按 schema 错误记录并降级取值；`observed_input/output`、`tool_calls`、`arguments` 类型错误同样记录。

## 异构 Agent 的步骤名映射（0.2.0+）

不同 Agent 的步骤命名往往不同。`adapters.STEP_ALIASES` 提供一张常用别名表（如 `SUBMIT → SUBMIT_INVOICE`、`COLLECT → COLLECT_INVOICE_DATA`），`generic` 适配器会先按别名表映射到数据字典步骤；映射不到的步骤名会原样保留，并在结果中显式记录（业务类未识别步骤记 error，非业务类记 warn），不会静默丢弃。新增 Agent 时请先补别名表或在轨迹里直接使用标准步骤名。
