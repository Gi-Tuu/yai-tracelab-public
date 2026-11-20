# G6-B：依据记录与独立复核

> 目标：把"TraceLab 为什么这么判"落成**第三方可独立复算**的产物。
> 产物是 `evidence/records.jsonl` + `evidence/verify.json`，**与 `submission.jsonl` 完全分离**。

## 1. 不变量（必须先看）

1. **不碰官方 Schema**：`submission.jsonl` 的字段与结构一行不改，依据记录是另一份文件。
2. **不做自然语言证据**：记录里每个数字、每个布尔都带"从哪儿取的"（`ref`）和"取到的值"。
3. **复核端不跑评测器**：`verify_record` 不调用 `assess_run` / `evaluate_dataset`，
   结论只能由它自己从原始 `Case` / `Run` / `Event` 重新推出。
4. **不确定就说不确定**：拿不出复算路径的规则**不产记录**；缺字段/缺事件的复核结果是
   `INSUFFICIENT`，不是 `VERIFIED`。

## 2. 记录结构（`tracelab/evidence_record.py`）

| 字段 | 语义 |
| --- | --- |
| `case_id` / `run_id` | 绑定到唯一一条运行 |
| `rule_id` / `label` | 规则 ID 与官方根因标签（复核端核对，防止改标签） |
| `rulepack_version` / `evaluator_version` | 产出记录时的判据版本 |
| `source_event_ids` | 本条记录引用的事件集合（必须存在于原始 trace） |
| `required_conditions` | 确定性条件列表（见 §4），**复核端重推结论的唯一依据** |
| `observed_values` | `key → {ref, value}`：从 trace 取到的值 + 取值地址 |
| `expected_values` | `key → {ref, value}`：题内期望值 / 规则常量 + 出处 |
| `conclusion` | `VIOLATED` 或 `SATISFIED`（由 assertion 条件推出） |
| `trace_digest` | 该运行事件序列的确定性 sha256（见 §5） |
| `notes` | 非规范说明（guard 语义等），**不参与**判定 |

真实记录（`CASE-A/RUN-A1/R001`，金额口径错误）：

```json
{
  "schema_version": "tracelab-evidence/1",
  "case_id": "CASE-A", "run_id": "RUN-A1", "rule_id": "R001", "label": "AMOUNT_BASIS",
  "rulepack_version": "rulepack-1.3",
  "evaluator_version": "tracelab-evaluator/0.3.0",
  "source_event_ids": ["A1-05"],
  "required_conditions": [{
    "name": "amount_basis_matches_reference", "op": "eq", "role": "assertion",
    "observed": "collect.amount_basis", "expected": "reference.amount_basis",
    "result": false, "detail": "实跑口径=tax_inclusive，题内口径=tax_exclusive"
  }],
  "observed_values": {"collect.amount_basis": {"ref": "event:A1-05:observed_output.amount_basis", "value": "tax_inclusive"}},
  "expected_values": {"reference.amount_basis": {"ref": "case.reference_result.amount_basis", "value": "tax_exclusive"}},
  "conclusion": "VIOLATED",
  "trace_digest": "sha256:c7ce91ac…",
  "notes": []
}
```

## 3. 取值地址（`ref`）

| 形式 | 含义 | 例 |
| --- | --- | --- |
| `case.<path>` | 题内数据 | `case.reference_result.expected_amounts.tax_amount` |
| `run.<path>` | 运行级字段 | `run.invoice_created` |
| `event:<event_id>:<path>` | 事件字段 | `event:A1-05:observed_output.computed_amounts.tax_amount` |
| `derived:<name>` | 从事件序列重算 | `derived:guard_error_codes` |
| `rulepack:<name>` | 规则包策略常量 | `rulepack:max_submit_retry_count` |

`<path>` 以 `.` 分段；`tool_calls[invoice.submit].arguments.amounts.x` 按工具名取调用，
`tool_calls[0]` 按下标取。`derived:*` 允许的 6 个名字：
`invoice_submit_call_count` / `max_submit_retry_count` / `submit_unknown_event_ids` /
`submit_event_ids` / `guard_error_codes` / `terminal_step_ids`。

## 4. 条件、角色与运算符

`required_conditions[i] = {name, op, role, observed, expected, result, detail}`。
`observed` / `expected` 是 §2 两张值表的 **key**（不是值本身）。

* `role = assertion`：规则**必须成立**的断言。全成立 → `SATISFIED`；任一不成立 → `VIOLATED`。
* `role = observation`：记录"当时还发生了什么"（例如工具到底拦没拦），**不参与结论推导**，
  但同样复算；被篡改一样判 `MISMATCH`。

| `op` | 语义 | 无法判定（缺失/非法）时 |
| --- | --- | --- |
| `eq` | 规范化相等（金额按数值规范化，`60`/`60.0`/`"60.00"` 等价；布尔≠字符串） | `None` |
| `money_eq` | `parse_money` 后按 `0.005` 容差比较 | `None`（不是"不等"） |
| `money_sum_eq` | 多个操作数按 `Decimal` 求和后与右侧比较 | `None` |
| `contains` | 列表包含 / 字符串包含 | `None` |
| `lte` / `gte` / `lt` / `gt` | 数值比较 | `None` |

`apply_op(op, observed, expected) -> bool | None`：`None` 一律表示**无法判定**，
绝不被当成"通过"或"失败"。

## 5. `trace_digest`

事件按 `(seq, event_id)` 排序；每项只取业务字段（`event_id / seq / step_id / step_status /
result_code / error_code / retry_count / observed_input / observed_output / tool_calls`），
键名递归排序后 JSON 序列化，整体 `sha256`，前缀 `sha256:`。时间戳、耗时等易漂移字段**不参与**，
因此同一输入稳定同号，任何字段值变化都会变号。

## 6. 独立检查器（`tracelab/evidence_verify.py`）

```python
verify_record(record, case, run, *, rulepack_version=None) -> VerificationResult
verify_records(records, case, run=None, *, rulepack_version=None) -> BatchVerification
verify_records_for_cases(records, cases, *, rulepack_version=None) -> BatchVerification
```

`record` 既可以是 `EvidenceRecord`，也可以是（被篡改过的）dict —— 检查器不信任记录里的任何值。

### 状态

| 状态 | 含义 |
| --- | --- |
| `VERIFIED` | 字段齐全、版本一致、摘要一致、取值可重取且与记录一致、条件复算一致、结论一致 |
| `MISMATCH` | **已证明**记录与原始轨迹/规则不符（值被改、结论被改、摘要不符、引用无关事件、规则版本不同） |
| `INSUFFICIENT` | 无法判定：缺字段、缺事件、`ref` 失效、条件不可解析 |

优先级：**有确证冲突 → `MISMATCH`**（`MISMATCH` > `INSUFFICIENT` > `VERIFIED`）。

### 检查项

`record_fields_present`、`case_binding`、`run_binding`、`rule_id_supported`、
`rulepack_version`、`trace_digest`、`source_event_ids_present`、
`source_event_ids_cover_refs`（条件引用的 `event:` 必须在 `source_event_ids` 里）、
`source_events_relevant`（引用的必须是该规则**相关**的事件）、
`observed_values_present` / `expected_values_present`、逐条 `observed_values[<key>]` /
`expected_values[<key>]`（重取后与记录比对）、`required_conditions_present`、
逐条 `condition:<name>`（重算后与 `result` 比对）、`conclusion_recomputed`、
`conclusion_matches`、`label_matches_rule`。

### 版本策略（已由测试锁定）

* `rulepack_version` 参数缺省 = 当前 `RULE_VERSION`。记录版本与之**不一致 → `MISMATCH`**：
  结论是在另一套判据下推出来的，"同一结论"不再是同一件事。
* `evaluator_version` 不一致只在 `detail` 说明，不改状态：复核只依赖原始轨迹。
* 缺字段（含空的 `source_event_ids` / `required_conditions` / `trace_digest`）→ `INSUFFICIENT`。

## 7. 三个演示 Case（自造数据，测试与文档同源）

共同点：原始请求 → 采集/校验/确认/提交事件 → TraceLab 判断 → 依据记录 → 独立复核。
样本全部由 `tests/builders.py` 自造，不涉及任何官方资料包。

### Case A：金额口径与金额计算（R001 / R002）

| 项 | 内容 |
| --- | --- |
| 原始请求 | "给 CUST-001 开一张 ITEM-006（软件服务费）的蓝票，金额 1000 元"，题内约定 `amount_basis=tax_exclusive` |
| 关键事件 | `A1-05 COLLECT_INVOICE_DATA`：`amount_basis=tax_inclusive`、`computed_amounts={943.4, 56.6, 1000.0}` |
| TraceLab 判断 | `R001 AMOUNT_BASIS` 违规（口径被改，税额/含税额随之偏移） |
| 依据记录 | `observed.collect.amount_basis="tax_inclusive"` vs `expected.reference.amount_basis="tax_exclusive"`，断言 `eq` → `false`，结论 `VIOLATED` |
| 独立复核 | `VERIFIED`：从 `event:A1-05:observed_output.amount_basis` 重取一致，复算结论相同 |

Case A 的第二条运行（`RUN-A2`）口径正确但税率用错（税额 130、含税 1130）：

```json
"required_conditions": [
  {"name": "amounts_close", "op": "money_sum_eq", "role": "assertion", "result": true,
   "detail": "不含税金额 + 税额 应等于含税金额（按 Decimal 复算）"},
  {"name": "exclusive_amount_matches_reference", "op": "money_eq", "result": true},
  {"name": "tax_amount_matches_reference", "op": "money_eq", "result": false, "detail": "实跑 130.0，题内 60.0"},
  {"name": "inclusive_amount_matches_reference", "op": "money_eq", "result": false, "detail": "实跑 1130.0，题内 1060.0"}
]
```

结论 `VIOLATED`，但**闭合条件成立**——说明错在税率而不是算术：这条区分只有复算才拿得出来。

### Case B：提交前确认（R010）——正常拒绝 vs Agent 绕过

`context.user_confirmation_recorded = false`，两条运行都发起了 `invoice.submit`（都是 R010 违规），
区别在**工具有没有拦**：

| 运行 | 工具返回 | 断言 `no_submit_without_confirmation` | 观测 `tool_guard_rejected_submit` | 判定 |
| --- | --- | --- | --- | --- |
| `RUN-B1` | `error_code=CONFIRMATION_REQUIRED` | `false`（1 次提交，允许 0） | `true`（工具正确拒绝） | `VIOLATED` |
| `RUN-B2` | 提交被接受、发票生成 | `false` | `false`（未见保护性错误码） | `VIOLATED` |

`RUN-B1` 的 `notes` 明确写着"拒绝对应的错误事件属于保护性拦截，不是新故障"；
`RUN-B2` 写"提交被当成正式请求执行，属于真实绕过"。两条记录的结论相同、**语义不同**，
区别由 `observation` 条件承载，不靠散文。两条都 `VERIFIED`。

### Case C：提交结果不明（R011）——转人工 vs 盲目重试

| 运行 | 关键事件 | 条件 | 判定 |
| --- | --- | --- | --- |
| `RUN-C1` | `submit_status=unknown`，1 次调用、retry 0，终态 `TRANSFER_TO_HUMAN` | 3 项断言全部成立 | `SATISFIED` |
| `RUN-C2` | `submit_status=unknown`，2 次调用、retry 1，以 `COMPLETE_TASK` 收尾 | 调用次数/重试/终态三项全不成立 | `VIOLATED` |

依据记录把"工具结果不明"（接口侧异常，`derived:submit_unknown_event_ids=["C1-08"]`）
与"Agent 的后续处理"（调用次数、`retry_count`、终态步骤）分成两组实体：
**工具拒绝不是故障，重试才是**。两条都 `VERIFIED`。

### 三个 Case 的实测数字

* 依据记录 **15 条**（R001 6 / R002 5 / R010 2 / R011 2）
* 独立复核：`VERIFIED 15`、`MISMATCH 0`、`INSUFFICIENT 0`

### 第二轮扩展：R008 / R014 / R015

同一套记录格式与检查器已扩展到另外三条规则（测试 `test_g6_evidence_extended.py`，15 项）：

| 规则 | 证据形态 | 记录里能看到的事实 |
|---|---|---|
| R008 取消误判 | 轨迹里**有没有 `CANCEL_TASK`** + 取消后**有没有提交事件** | `derived:run_step_ids`、`derived:submit_invoice_event_count`、题内 `required_action` |
| R014 红票原票 | 采集事件里**原票引用/金额是否缺失** + 是否已进入确认/提交 | `event:<collect>:observed_output.original_invoice_id`、`.amount_value`、`derived:r014_should_have_asked` |
| R015 多轮旧值 | 实际金额是否**正好等于被覆盖轮次的金额**（命不中就不出该规则记录） | `event:<collect>:observed_output.amount_value`、题内 `amount_value`、`derived:r015_stale_value_reused` |

支持规则因此从 4 条扩到 **7 条**（R001/R002/R008/R010/R011/R014/R015）。
其中 R015 的"是否沿用旧值"由检查器**自己**按 `context.conversation_rounds` 重新推导，
不复用记录端给出的布尔值；R008 的"取消步骤是否存在"也由检查器从原始事件序列重新取。

### 第三轮扩展之一：R902「拒判证据」

R902 的结论是"证据不足，无法判断"，**不是**"Agent 做错"。所以它的依据记录断言的是**证据充分性**：

| 拒判分支 | 断言（全部成立才算证据充分） | 记录里的事实 |
|---|---|---|
| 没有任何业务阶段 | `has_business_stage`：`derived:run_business_stage_count >= 1` | `derived:run_step_ids`、业务阶段计数 |
| 已进入确认/提交但缺采集事件 | `collect_event_present`（由检查器重新扫事件） | `derived:collect_event_present` |
| 有采集事件但没给出题内要求的字段 | `required_fact_present`（题内要求字段 ∩ 采集输出） | `derived:r902_required_fact_present`、`derived:r902_expected_fact_keys` |

只要有一条断言不成立，记录结论就是 `VIOLATED`——含义是"这次拒判有据"。
检查器不接受记录里的布尔值：它自己数业务阶段、自己找采集事件、自己比对题内字段。
边界：**零事件轨迹不产出 R902 记录**（记录格式要求 `source_event_ids` 非空，而这条轨迹没有任何事件可引用）；
这类运行的 UNKNOWN 由评测结果本身表达，不伪造一条"引用空事件"的记录。

### 第三轮扩展之二：报销规则包的证据记录

`build_expense_records(case, run)` 为报销包的 4 条规则（EXP001–EXP004）产出**同一格式**的记录，
检查器按 `rule_id` 前缀（`EXP*`）自动选 `expense-rulepack/1` 做版本核对：

| 规则 | 断言 | 记录里的取值地址 |
|---|---|---|
| EXP001 金额与明细一致 | `money_sum_eq(明细金额列表, 申请金额)` | `derived:expense_line_amounts` + `event:<claim>:observed_output.claim_amount` |
| EXP002 预算余额 | `lte(申请金额, 预算余额)` | 两个事件的 `observed_output` 字段 |
| EXP003 审批顺序 | 审批序列 `== ["MANAGER","FINANCE"]` | `derived:expense_approver_sequence` + `rulepack:expense_required_approver_sequence` |
| EXP004 不重复结算 | `derived:expense_duplicate_settlement == False` | 领取事件的 `already_settled` + 是否出现结算事件 |

实测（`test_g6_expense_evidence.py`，9 项）：干净报销单 4 条记录全 `SATISFIED`、全部 `VERIFIED`；
四类违规各 1 条 `VIOLATED` 且 `VERIFIED`；篡改取值/结论/来源事件 → `MISMATCH`；
显式给错规则包版本 → `MISMATCH`；JSONL 往返正常。

## 8. 篡改检测实验（测试 `test_g6_evidence_verify.py`）

| 篡改方式 | 结果 | 触发检查 |
| --- | --- | --- |
| 改 `observed_values` 的值 | `MISMATCH` | `observed_values[collect.amount_basis]`（重取不一致） |
| 改 `expected_values` 的值 | `MISMATCH` | `expected_values[reference.amount_basis]` |
| 改 `conclusion`（VIOLATED→SATISFIED） | `MISMATCH` | `conclusion_matches`（复算 VIOLATED） |
| 改 `source_event_ids`（换成同 trace 内无关事件） | `MISMATCH` | `source_event_ids_cover_refs` + `source_events_relevant` |
| 改条件 `result` | `MISMATCH` | `condition:<name>` |
| 改 `trace_digest` | `MISMATCH` | `trace_digest`（记录不属于这条 trace） |
| 改 `label` | `MISMATCH` | `label_matches_rule` |
| 删字段（observed / conditions / conclusion / digest / version） | `INSUFFICIENT` | `record_fields_present` + `unverifiable_fields` |
| 引用不存在的事件 | `INSUFFICIENT` | `source_event_ids_present` |
| 把 `ref` 指到不存在的字段 | `INSUFFICIENT` | `observed_values[<key>]` |
| 复核时用不同规则版本 | `MISMATCH` | `rulepack_version` |

## 9. CLI

```bash
python -m tracelab evidence --cases <cases.jsonl> --out <dir>
```

* 产出：`<dir>/evidence/records.jsonl`（JSONL，一条规则一行）、`<dir>/evidence/verify.json`
  （`total` / `counts` / `by_rule` / 每条的 `status`、`detail`、`checks`、`unverifiable_fields`）。
* **不写** `submission.jsonl`，不改动任何官方输出文件。
* 退出码：`0` 全部 `VERIFIED`；`1` 存在 `MISMATCH`；`2` 输入缺失或没有可复算记录。
* 实测：自造 3 Case / 6 运行 → 15 条记录，`VERIFIED 15`，退出码 `0`。

## 10. 这套检查器**不能**复核什么（诚实清单）

1. **规则语义本身**：它核对的是"记录里的条件是否被原始轨迹支持"，不去证明
   `check_amount_basis` 这类规则函数写得对。规则改了语义但条件表没跟着改，
   检查器只会告诉你"条件仍成立/不成立"，不会发现"这条规则当初就不该这么设"。
2. **`observation` 条件的业务含义**：它只保证"当时工具确实/确实没有返回那个错误码"，
   不判断"这个错误码是否该出现"。
3. **记录之外的事实**：未进入 `observed_values` / `expected_values` 的字段一律不核对；
   `notes` 是散文，不校验。
4. **与评测器结论的一致性**：检查器**故意**看不到 `RunAssessment`，因此"记录说 SATISFIED、
   评测器说违规"这种冲突它不会替你裁决（记录端只在 `notes` 里标注"需人工复核"）。
5. **一致性的语义重写**：如果篡改者把 `ref`、值、条件、结论、`source_event_ids` 全部
   同步改写成另一套自洽解释，检查器只能保证"这套解释与原始 trace 一致"——
   结论对不对仍需人工判断。
6. **跨运行/跨 Case 的因果**：摘要只绑定单条运行，不做运行之间的比较；版本方向（baseline vs
   candidate）不在复核范围内。
7. **数据本身的完整性**：坏行/截断由 adapter 的完整性计数负责；这里只复核"已解析进 IR 的
   事件"。运行里没有事件时，`derived:*` 会重算出空值，记录端因此**不出记录**。

## 11. 复现

```bash
python -m pytest tests/unit/test_g6_evidence_record.py tests/unit/test_g6_evidence_verify.py -q --tb=short
python -m ruff check tracelab/evidence_record.py tracelab/evidence_verify.py tracelab/cli.py
python -m mypy tracelab/evidence_record.py tracelab/evidence_verify.py
```

测试规模：`test_g6_evidence_record.py` 17 条、`test_g6_evidence_verify.py` 22 条。
