"""G6-B1：可被第三方独立复算的「依据记录」（evidence record）。

为什么要这一层
--------------
`submission.jsonl` 只允许官方 Schema 的字段，语义是"结论 + 首因 + 证据事件 ID"。
它**不能**回答："这个结论是怎么从原始事件一步步推出来的？" 本模块把推导过程
落成**另一份独立产物**（JSONL），不写进 submission、不改动官方输出结构。

一条记录只允许三类可机读实体，禁止自然语言口号
--------------------------------------------
1. `observed_values` / `expected_values`：**值 + 值的出处**
   `{"ref": <取值地址>, "value": <规范化值>}`。取值地址是可机读的（见下），
   复核端据此回到原始 trace 重新取值，而不是相信记录里写的值。
2. `required_conditions`：**确定性条件**
   `{"name", "op", "role", "observed", "expected", "result", "detail"}`。
   `observed` / `expected` 是上面两张表的 **key**（不是值本身），`op` 是
   `apply_op` 定义的运算，`result` 是记录端算出的布尔值。
3. `trace_digest`：整条运行事件序列的规范化 sha256，把记录绑定到唯一一条 trace。

取值地址（ref）语法
-------------------
    case.<path>                 题内数据，如 case.reference_result.amount_basis
    run.<path>                  运行级字段，如 run.invoice_created
    event:<event_id>:<path>     事件字段，如 event:C1-05:observed_output.amount_basis
    derived:<name>              从事件序列重算的导出值，如 derived:guard_error_codes
    rulepack:<name>             规则包策略常量，如 rulepack:max_submit_retry_count

`<path>` 以 `.` 分段；`tool_calls[invoice.submit]` 按工具名取第一条调用，
`tool_calls[0]` 按下标取。`derived:*` 与 `rulepack:*` 的名字集合见下文的
`DERIVED_REFS`、`POLICY`；复核端自己实现一遍同名重算，不复用记录里的值。

条件角色（role）
----------------
* `assertion`：规则**必须成立**的断言。全部成立 → `SATISFIED`；任一不成立 → `VIOLATED`。
  这正是复核端重推结论的唯一依据。
* `observation`：只记录"当时还发生了什么"（例如工具是否真的拦下了这次提交），
  **不参与**结论推导，但同样要复算——被篡改一样会被判 `MISMATCH`。

guard 语义（正常拒绝 vs 错误执行）
----------------------------------
同一份数据里，工具返回 `CONFIRMATION_REQUIRED` 之类的保护性错误码是**正确防护**，
不是故障；而 Agent 在未确认时发起提交（或结果不明时盲目重试）才是故障。
记录用 `observation` 条件把"工具当时有没有拦"与"Agent 有没有绕过"分开表达，
两者不会互相顶替。

版本
----
`rulepack_version` / `evaluator_version` 逐条记录。复核端只在规则版本一致时承认
"同一套判据下的复算"；版本不一致的处理见 `evidence_verify` 模块文档。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .attribution import GUARD_ERROR_CODES, RunAssessment
from .contracts import Case, Event, Run
from .evaluation import EVALUATOR_VERSION
from .rules import RULE_VERSION, Violation
from .state import BusinessState, parse_money, reconstruct

# ---------------------------------------------------------------- 版本与常量

#: 依据记录的独立格式版本（与官方 submission Schema 无关，也不进 submission）
EVIDENCE_SCHEMA_VERSION = "tracelab-evidence/1"

#: 目前具备"确定性复算路径"的规则。其余规则不产出依据记录——
#: 没有可复算路径就不发记录，绝不用自然语言充当证据。
SUPPORTED_RULES: tuple[str, ...] = ("R001", "R002", "R008", "R010", "R011", "R014", "R015", "R902")

#: 报销规则包（G6-C）的证据规则；与开票规则共用同一套记录格式与检查器。
EXPENSE_SUPPORTED_RULES: tuple[str, ...] = ("EXP001", "EXP002", "EXP003", "EXP004")

#: 检查器接受的全部规则（开票 + 报销）。
ALL_SUPPORTED_RULES: tuple[str, ...] = SUPPORTED_RULES + EXPENSE_SUPPORTED_RULES

#: 业务阶段：出现任意一个才说明"这条轨迹里有可核对业务事实"（R902 用）。
BUSINESS_STAGES: tuple[str, ...] = (
    "PARSE_INTENT",
    "COLLECT_INVOICE_DATA",
    "VALIDATE_DATA",
    "CONFIRM_INVOICE",
    "SUBMIT_INVOICE",
    "WAIT_INVOICE_CALLBACK",
)

#: 报销场景的步骤 ID（与 `tracelab/rulepacks.py` 的 EXPENSE_PACK 事实抽取一致）。
EXPENSE_STEPS: tuple[str, ...] = (
    "RECEIVE_CLAIM",
    "CHECK_BUDGET",
    "APPROVE_MANAGER",
    "APPROVE_FINANCE",
    "SETTLE_EXPENSE",
)

#: 每条规则**允许出现的条件名**（规则-判据绑定，审计 F-05）。
#: 复核端用它检查"记录的条件集合是否属于该规则"，防止只改 rule_id/label 就把
#: 一条别的规则的记录冒充成本规则（旧实现只看 label 与 rule_id 是否自洽）。
#: 用 glob 而不是穷举：R002 的条件名按 `expected_amounts` 的键动态生成
#: （`exclusive_amount_matches_reference` 等），穷举会随数据键漂移。
RULE_CONDITION_PATTERNS: dict[str, tuple[str, ...]] = {
    "R001": ("amount_basis_matches_reference",),
    "R002": ("amounts_close", "*_amount_matches_reference", "exclusive_matches_reference", "tax_matches_reference", "inclusive_matches_reference"),
    "R008": ("cancel_task_present", "no_submit_after_cancel"),
    "R010": ("no_submit_without_confirmation", "tool_guard_rejected_submit"),
    "R011": ("submit_calls_after_unknown", "submit_retry_count_zero", "reached_transfer_to_human"),
    "R014": ("agent_asked_before_invoicing",),
    "R015": ("uses_latest_round_value",),
    "R902": ("has_business_stage", "collect_event_present", "required_fact_present",
             "money_fields_parseable", "business_steps_recognized"),
    "EXP001": ("claim_amount_equals_line_sum",),
    "EXP002": ("claim_within_budget",),
    "EXP003": ("approval_order_matches_process",),
    "EXP004": ("no_duplicate_settlement",),
}

#: 规则 ID → 官方根因标签。复核端用它核对 label 是否被改。
RULE_LABELS: dict[str, str] = {
    "R001": "AMOUNT_BASIS",
    "R002": "AMOUNT_CALCULATION",
    "R008": "INTENT_CLASSIFICATION",
    "R010": "CONFIRMATION_BYPASS",
    "R011": "TOOL_ERROR_HANDLING",
    "R014": "FIELD_FABRICATION",
    "R015": "CONTEXT_UPDATE",
    "R902": "INSUFFICIENT_EVIDENCE",
    # 报销规则包（自造场景，标签与开票标签同构但独立）
    "EXP001": "EXPENSE_AMOUNT_MISMATCH",
    "EXP002": "EXPENSE_BUDGET_EXCEEDED",
    "EXP003": "EXPENSE_APPROVAL_ORDER",
    "EXP004": "EXPENSE_DUPLICATE_SETTLEMENT",
}

CONCLUSION_VIOLATED = "VIOLATED"
CONCLUSION_SATISFIED = "SATISFIED"
CONCLUSIONS: tuple[str, ...] = (CONCLUSION_VIOLATED, CONCLUSION_SATISFIED)

#: 金额容差。与 `tracelab.state.money_compare` 的默认值保持一致。
MONEY_TOLERANCE = "0.005"

#: 规则包内的策略常量。记录里以 `rulepack:<name>` 引用，复核端持有同名副本。
POLICY: dict[str, Any] = {
    "max_submit_calls_without_confirmation": 0,
    "max_submit_calls_after_unknown": 1,
    "max_submit_retry_count": 0,
    "required_guard_code_confirmation": "CONFIRMATION_REQUIRED",
    # 终态**步骤 ID**（事件里出现的取值）与题内 `acceptable_snapshot_state` 的取值不同名，
    # R011 的前置判断用的是后者，这里两个都显式登记，避免字符串混用。
    "required_terminal_step_id": "TRANSFER_TO_HUMAN",
    "acceptable_state_transfer": "TRANSFERRED_TO_HUMAN",
    "money_tolerance": MONEY_TOLERANCE,
    # R008：取消类请求必须在轨迹里出现这个步骤，且取消后不得再有提交事件
    "required_cancel_step_id": "CANCEL_TASK",
    "max_submit_invoice_events_after_cancel": 0,
    # R014：缺原票引用/金额时不得继续出票（False = 不允许"未追问就继续"）
    "should_not_proceed_without_required_fields": False,
    # R015：多轮多价必须使用最新一轮的值（False = 不允许沿用被覆盖的旧值）
    "uses_latest_round_value": False,
    # R902：至少要有一个业务阶段，才能核对"Agent 到底做了什么"
    "min_business_stage_count": 1,
    # R902 分支二/三：已进入确认/提交时必须存在采集事件，且采集事件至少给出一个题内要求的字段
    "collect_event_required": True,
    "required_fact_present": True,
    "no_invalid_money": True,
    "no_unrecognized_business_steps": True,
    # 报销场景（自造）：审批顺序必须严格是 经理 → 财务
    "expense_required_approver_sequence": ["MANAGER", "FINANCE"],
    # 报销场景：已结算过的申请不允许再次结算（False = 不允许）
    "duplicate_settlement_disallowed": False,
}

#: `derived:*` 可用的名字（复核端 evidence_verify 自行重算同名值）。
DERIVED_REFS: tuple[str, ...] = (
    "invoice_submit_call_count",
    "max_submit_retry_count",
    "submit_unknown_event_ids",
    "submit_event_ids",
    "guard_error_codes",
    "terminal_step_ids",
    # 第二轮的三个规则（R008 / R014 / R015）
    "run_step_ids",
    "submit_invoice_event_count",
    "r014_should_have_asked",
    "r015_stale_value_reused",
    # R902 拒判证据
    "run_business_stage_count",
    "collect_event_present",
    "r902_expected_fact_keys",
    "r902_required_fact_present",
    "r902_invalid_money_fields",
    "r902_money_fields_parseable",
    "r902_unrecognized_business_steps",
    # 报销规则包（G6-C）
    "expense_line_amounts",
    "expense_approver_sequence",
    "expense_duplicate_settlement",
)

#: 终态步骤。与 `tracelab.contracts.TERMINAL_STEPS` 同义，这里保留一份显式常量，
#: 避免把"终态"定义散落在多个模块的字符串字面量里。
TERMINAL_STEPS: tuple[str, ...] = (
    "COMPLETE_TASK",
    "FAIL_TASK",
    "TIMEOUT_TASK",
    "TRANSFER_TO_HUMAN",
    "CANCEL_TASK",
)

#: 取值失败哨兵：与"取到的值就是 None"严格区分。
MISSING: Any = object()


# ---------------------------------------------------------------- 值规范化

def normalize_value(value: Any) -> Any:
    """把任意值规范化成可比较的形态（用于 `eq` 与"记录值 vs 原始值"核对）。

    金额与整数/浮点/Decimal 统一走 `parse_money`，因此 `60`、`60.0`、`"60.00"`
    等价，而 `True` 与 `"True"` 不等（布尔不是金额，字符串也不是布尔）。
    审计 F-41：字符串也要尝试按金额解析（旧实现只对数字走 parse_money，
    `"60.00"` 与 `60` 不等，与上面的承诺相反）。
    """
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((normalize_value(item) for item in value), key=repr)
    if isinstance(value, bool):
        return "bool:true" if value else "bool:false"
    if isinstance(value, (int, float, Decimal)):
        parsed = parse_money(value)
        if parsed.ok and parsed.value is not None:
            return "num:" + str(parsed.value.normalize())
        return "raw:" + repr(value)
    if isinstance(value, str):
        parsed = parse_money(value)
        if parsed.ok and parsed.value is not None:
            return "num:" + str(parsed.value.normalize())
        return value
    if isinstance(value, Mapping):
        return {str(k): normalize_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    return str(value)


# ---------------------------------------------------------------- 条件运算

def apply_op(op: str, observed: Any, expected: Any) -> bool | None:
    """执行一个确定性条件运算。

    返回 `True` / `False` 表示"断言成立 / 不成立"；返回 `None` 表示
    **无法判定**（操作数缺失、非法金额、未知运算符）——调用方必须把它当成
    证据不足，而不是当成通过或失败。
    """
    if observed is MISSING or expected is MISSING:
        return None
    if op == "eq":
        return normalize_value(observed) == normalize_value(expected)
    if op == "money_eq":
        return money_equal(observed, expected)
    if op == "money_sum_eq":
        operands = observed if isinstance(observed, (list, tuple)) else [observed]
        if not operands:
            return None
        total = Decimal("0")
        for item in operands:
            parsed = parse_money(item)
            if not parsed.ok or parsed.value is None:
                return None
            total += parsed.value
        want = parse_money(expected)
        if not want.ok or want.value is None:
            return None
        return abs(total - want.value) <= Decimal(MONEY_TOLERANCE)
    if op == "contains":
        if isinstance(observed, (list, tuple, set, frozenset)):
            target = normalize_value(expected)
            return any(normalize_value(item) == target for item in observed)
        if isinstance(observed, str):
            return str(expected) in observed
        return None
    if op in ("lte", "gte", "lt", "gt"):
        left, right = parse_money(observed), parse_money(expected)
        if not (left.ok and right.ok) or left.value is None or right.value is None:
            return None
        span = Decimal(MONEY_TOLERANCE)
        if op == "lte":
            return left.value <= right.value + span
        if op == "gte":
            return left.value >= right.value - span
        if op == "lt":
            return left.value < right.value
        return left.value > right.value
    return None


def money_equal(left: Any, right: Any) -> bool | None:
    """三态金额相等：任一侧非法/缺失 → `None`（不是"相等"，也不是"不等"）。"""
    a, b = parse_money(left), parse_money(right)
    if not (a.ok and b.ok) or a.value is None or b.value is None:
        return None
    return abs(a.value - b.value) <= Decimal(MONEY_TOLERANCE)


# ---------------------------------------------------------------- 小工具

def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _event_ref(event_id: str, *path: str) -> str:
    return "event:" + event_id + ":" + ".".join(path)


def _value(ref: str, value: Any) -> dict[str, Any]:
    return {"ref": ref, "value": value}


def _cond(
    name: str,
    op: str,
    role: str,
    observed: str | list[str],
    expected: str,
    result: bool,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "op": op,
        "role": role,
        "observed": observed,
        "expected": expected,
        "result": bool(result),
        "detail": detail,
    }


def _json_safe(value: Any) -> Any:
    """把任意值折叠成可 JSON 序列化的形态（保持确定性）。"""
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=repr)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------- 轨迹摘要

def _fingerprint(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "step_id": event.step_id,
        "step_status": event.step_status,
        "result_code": event.result_code,
        "error_code": event.error_code,
        "retry_count": event.retry_count,
        "observed_input": _json_safe(event.observed_input),
        "observed_output": _json_safe(event.observed_output),
        "tool_calls": [
            {
                "name": call.name,
                "arguments": _json_safe(call.arguments),
                "result": _json_safe(call.result),
                "attempt": call.attempt,
            }
            for call in event.tool_calls
        ],
    }


def trace_digest(run: Run) -> str:
    """对运行的事件序列做**确定性**摘要。

    规范化字段（而非原始字节）：事件按 `(seq, event_id)` 排序，每项只取业务字段，
    `observed_input/output` 与 `tool_calls` 递归排序键名后序列化，再整体 sha256。
    因此同一输入（同事件、同字段值）稳定得到同一摘要；任何字段值变化都会变号。
    时间戳/耗时等易漂移字段不参与摘要。
    """
    payload = [_fingerprint(event) for event in sorted(run.events, key=lambda e: (e.seq, e.event_id))]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 记录模型

def _value_table(raw: Any) -> dict[str, dict[str, Any]]:
    """把"key → {ref, value}"表折叠成纯 dict；非法条目直接丢弃（复核端会报缺字段）。"""
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


@dataclass(frozen=True)
class EvidenceRecord:
    """一条规则的依据记录。字段语义见模块文档。"""

    case_id: str
    run_id: str
    rule_id: str
    rulepack_version: str
    evaluator_version: str
    source_event_ids: tuple[str, ...]
    required_conditions: tuple[dict[str, Any], ...]
    observed_values: dict[str, dict[str, Any]]
    expected_values: dict[str, dict[str, Any]]
    conclusion: str
    trace_digest: str
    label: str = ""
    schema_version: str = EVIDENCE_SCHEMA_VERSION
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "rule_id": self.rule_id,
            "label": self.label,
            "rulepack_version": self.rulepack_version,
            "evaluator_version": self.evaluator_version,
            "source_event_ids": list(self.source_event_ids),
            "required_conditions": [_json_safe(dict(c)) for c in self.required_conditions],
            "observed_values": {k: _json_safe(dict(v)) for k, v in self.observed_values.items()},
            "expected_values": {k: _json_safe(dict(v)) for k, v in self.expected_values.items()},
            "conclusion": self.conclusion,
            "trace_digest": self.trace_digest,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceRecord:
        """宽松反序列化：缺字段取默认值，由复核端判定"证据不足"而不是在这里抛异常。"""
        raw_conditions = data.get("required_conditions")
        conditions = raw_conditions if isinstance(raw_conditions, (list, tuple)) else []
        return cls(
            case_id=str(data.get("case_id") or ""),
            run_id=str(data.get("run_id") or ""),
            rule_id=str(data.get("rule_id") or ""),
            rulepack_version=str(data.get("rulepack_version") or ""),
            evaluator_version=str(data.get("evaluator_version") or ""),
            source_event_ids=tuple(str(x) for x in (data.get("source_event_ids") or [])),
            required_conditions=tuple(dict(c) for c in conditions if isinstance(c, Mapping)),
            observed_values=_value_table(data.get("observed_values")),
            expected_values=_value_table(data.get("expected_values")),
            conclusion=str(data.get("conclusion") or ""),
            trace_digest=str(data.get("trace_digest") or ""),
            label=str(data.get("label") or ""),
            schema_version=str(data.get("schema_version") or EVIDENCE_SCHEMA_VERSION),
            notes=tuple(str(x) for x in (data.get("notes") or [])),
        )


# ---------------------------------------------------------------- 事件侧事实

def _unknown_submit(event: Event) -> bool:
    """提交结果是否**显式不明**（超时 / SUBMIT_TIMEOUT* / submit_status == unknown）。

    与 `tracelab.rules_control._submit_result_unknown` 同义：字段缺失不等于结果不明。
    这里保留一份独立实现，让记录端不依赖规则模块的私有函数。
    """
    if event.step_status == "TIMED_OUT":
        return True
    if (event.error_code or "").startswith("SUBMIT_TIMEOUT"):
        return True
    status = event.observed_output.get("submit_status")
    return isinstance(status, str) and status.lower() == "unknown"


def _submit_event_ids(run: Run) -> list[str]:
    return [e.event_id for e in run.events if e.step_id == "SUBMIT_INVOICE"]


def _terminal_event_ids(run: Run) -> list[str]:
    return [e.event_id for e in run.events if e.step_id in TERMINAL_STEPS]


def _confirm_event_ids(run: Run) -> list[str]:
    return [e.event_id for e in run.events if e.step_id == "CONFIRM_INVOICE"]


def _guard_event_ids(run: Run) -> list[str]:
    ids: list[str] = []
    for event in run.events:
        if event.step_id in ("FAIL_TASK", "TIMEOUT_TASK", "CANCEL_TASK"):
            continue
        if (event.error_code or "") in GUARD_ERROR_CODES:
            ids.append(event.event_id)
    return ids


def _guard_codes(run: Run) -> list[str]:
    codes = {
        e.error_code
        for e in run.events
        if (e.error_code or "") in GUARD_ERROR_CODES and e.step_id not in TERMINAL_STEPS
    }
    return sorted(code for code in codes if code)


def _submit_call_count(run: Run) -> int:
    return sum(1 for event in run.events for call in event.tool_calls if call.name.startswith("invoice.submit"))


def _max_submit_retry(run: Run) -> int:
    return max((e.retry_count for e in run.events if e.step_id == "SUBMIT_INVOICE"), default=0)


def _terminal_steps(run: Run) -> list[str]:
    return [e.step_id for e in run.events if e.step_id in TERMINAL_STEPS]


# ---------------------------------------------------------------- 规则探针

@dataclass(frozen=True)
class _Probe:
    """某条规则在一条运行上的复算结果（记录端视角）。"""

    conditions: list[dict[str, Any]]
    observed: dict[str, dict[str, Any]]
    expected: dict[str, dict[str, Any]]
    source_ids: tuple[str, ...]
    notes: list[str] = field(default_factory=list)

    @property
    def conclusion(self) -> str | None:
        """按 assertion 条件推出结论；任一无法复算则不出结论。"""
        assertions = [c for c in self.conditions if c["role"] == "assertion"]
        if not assertions:
            return None
        if any(not isinstance(c["result"], bool) for c in assertions):
            return None
        return CONCLUSION_VIOLATED if any(c["result"] is False for c in assertions) else CONCLUSION_SATISFIED


def _probe_amount_basis(case: Case, state: BusinessState) -> _Probe | None:
    expected_basis = case.reference_result.get("amount_basis")
    collect = state.collect_event
    actual = state.amount_basis
    if not expected_basis or collect is None or actual is None:
        return None
    result = apply_op("eq", actual, expected_basis)
    if result is None:
        return None
    observed = {
        "collect.amount_basis": _value(_event_ref(collect.event_id, "observed_output", "amount_basis"), actual),
    }
    expected = {
        "reference.amount_basis": _value("case.reference_result.amount_basis", expected_basis),
    }
    conditions = [
        _cond(
            "amount_basis_matches_reference",
            "eq",
            "assertion",
            "collect.amount_basis",
            "reference.amount_basis",
            result,
            f"实跑口径={actual}，题内口径={expected_basis}",
        )
    ]
    return _Probe(conditions, observed, expected, _dedupe([collect.event_id]))


def _amounts_source(
    state: BusinessState,
) -> tuple[dict[str, Any], Callable[[str], str], str] | None:
    """R002 的金额来源与取值地址：优先采集事件的 computed_amounts，其次提交参数。

    与 `check_amount_calculation` 的取值优先级一致（computed_amounts → submit_amounts），
    但每条金额都绑定到**具体事件字段**，复核端可原地重取。
    """
    collect = state.collect_event
    computed = state.computed_amounts
    if collect is not None and computed:
        return (
            computed,
            lambda key: _event_ref(collect.event_id, "observed_output", "computed_amounts", key),
            "collect.computed",
        )
    submit = state.submit_event
    if submit is None:
        return None
    amounts = state.submit_amounts
    if not amounts:
        return None
    if state.submit_tool_calls():
        return (
            amounts,
            lambda key: _event_ref(submit.event_id, "tool_calls[invoice.submit]", "arguments", "amounts", key),
            "submit.amounts",
        )
    return (
        amounts,
        lambda key: _event_ref(submit.event_id, "observed_input", "amounts", key),
        "submit.amounts",
    )


def _probe_amount_calculation(case: Case, state: BusinessState) -> _Probe | None:
    expected_amounts = case.reference_result.get("expected_amounts")
    if not isinstance(expected_amounts, Mapping) or not expected_amounts:
        return None
    # R002 的前置是"R001 未触发"：口径已经错了就不再重复记一次计算错误
    expected_basis = case.reference_result.get("amount_basis")
    actual_basis = state.amount_basis
    if expected_basis and actual_basis is not None and str(actual_basis) != str(expected_basis):
        return None
    source = _amounts_source(state)
    if source is None:
        return None
    values, ref_of, origin = source
    keys = [key for key, value in expected_amounts.items() if value is not None]
    if not keys or any(key not in values for key in keys):
        # 字段不全就不出记录：宁可没有记录，也不给一份"看似通过"的半份证据
        return None
    observed = {f"{origin}.{key}": _value(ref_of(key), values[key]) for key in keys}
    expected = {
        f"reference.expected_amounts.{key}": _value(
            f"case.reference_result.expected_amounts.{key}", expected_amounts[key]
        )
        for key in keys
    }
    conditions: list[dict[str, Any]] = []
    closure_keys = ("exclusive_amount", "tax_amount", "inclusive_amount")
    if all(key in values for key in closure_keys):
        exclusive_key, tax_key, inclusive_key = closure_keys
        closure = apply_op("money_sum_eq", [values[exclusive_key], values[tax_key]], values[inclusive_key])
        if closure is not None:
            # 闭合条件两侧都取自同一事件（内部一致性）：右边挂到 expected 表的独立 key 上，
            # 复核端同样从原始轨迹重取，不看记录里的值。
            expected["closure.inclusive_amount"] = _value(ref_of(inclusive_key), values[inclusive_key])
            conditions.append(
                _cond(
                    "amounts_close",
                    "money_sum_eq",
                    "assertion",
                    [f"{origin}.{exclusive_key}", f"{origin}.{tax_key}"],
                    "closure.inclusive_amount",
                    closure,
                    "不含税金额 + 税额 应等于含税金额（按 Decimal 复算）",
                )
            )
    for key in keys:
        matched = apply_op("money_eq", values[key], expected_amounts[key])
        if matched is None:
            return None
        conditions.append(
            _cond(
                f"{key}_matches_reference",
                "money_eq",
                "assertion",
                f"{origin}.{key}",
                f"reference.expected_amounts.{key}",
                matched,
                f"实跑 {values[key]}，题内 {expected_amounts[key]}",
            )
        )
    if not conditions:
        return None
    source_ids = [value["ref"].split(":", 2)[1] for value in observed.values() if value["ref"].startswith("event:")]
    return _Probe(conditions, observed, expected, _dedupe(source_ids))


def _probe_confirmation(case: Case, run: Run) -> _Probe | None:
    recorded = case.context.get("user_confirmation_recorded")
    if recorded is not False:
        return None
    count = _submit_call_count(run)
    guards = _guard_codes(run)
    required_guard = POLICY["required_guard_code_confirmation"]
    observed = {
        "case.user_confirmation_recorded": _value("case.context.user_confirmation_recorded", recorded),
        "run.invoice_submit_call_count": _value("derived:invoice_submit_call_count", count),
        "run.guard_error_codes": _value("derived:guard_error_codes", guards),
    }
    expected = {
        "policy.max_submit_calls_without_confirmation": _value(
            "rulepack:max_submit_calls_without_confirmation", POLICY["max_submit_calls_without_confirmation"]
        ),
        "policy.guard_code_confirmation": _value("rulepack:required_guard_code_confirmation", required_guard),
    }
    assertion = apply_op(
        "eq", count, POLICY["max_submit_calls_without_confirmation"]
    )
    observation = apply_op("contains", guards, required_guard)
    if assertion is None or observation is None:
        return None
    conditions = [
        _cond(
            "no_submit_without_confirmation",
            "eq",
            "assertion",
            "run.invoice_submit_call_count",
            "policy.max_submit_calls_without_confirmation",
            assertion,
            f"未确认时正式提交调用次数={count}，允许值={POLICY['max_submit_calls_without_confirmation']}",
        ),
        _cond(
            "tool_guard_rejected_submit",
            "contains",
            "observation",
            "run.guard_error_codes",
            "policy.guard_code_confirmation",
            observation,
            "工具是否真的以保护性错误码拦下了这次提交（拦截是正确防护，不是故障）",
        ),
    ]
    notes: list[str] = []
    if count and observation:
        notes.append(f"工具以 {required_guard} 正确拒绝：属于保护性拦截，不是新故障；被记为违规的是 Agent 在未确认时发起提交。")
    elif count and not observation:
        notes.append("未见保护性错误码：提交被当成正式请求执行，属于真实绕过，不是工具正常拒绝。")
    sources = _dedupe(
        _submit_event_ids(run) + _guard_event_ids(run) + _confirm_event_ids(run) + _terminal_event_ids(run)
    )
    return _Probe(conditions, observed, expected, sources, notes)


def _probe_tool_error(case: Case, run: Run) -> _Probe | None:
    unknown = [e.event_id for e in run.events if e.step_id == "SUBMIT_INVOICE" and _unknown_submit(e)]
    if not unknown:
        return None
    count = _submit_call_count(run)
    retry = _max_submit_retry(run)
    terminals = _terminal_steps(run)
    required_terminal = POLICY["required_terminal_step_id"]
    need_transfer = case.reference_result.get("acceptable_snapshot_state") == POLICY["acceptable_state_transfer"]
    observed = {
        "run.invoice_submit_call_count": _value("derived:invoice_submit_call_count", count),
        "run.max_submit_retry_count": _value("derived:max_submit_retry_count", retry),
        "run.terminal_step_ids": _value("derived:terminal_step_ids", terminals),
        "submit.unknown_event_ids": _value("derived:submit_unknown_event_ids", unknown),
    }
    expected = {
        "policy.max_submit_calls_after_unknown": _value(
            "rulepack:max_submit_calls_after_unknown", POLICY["max_submit_calls_after_unknown"]
        ),
        "policy.max_submit_retry_count": _value("rulepack:max_submit_retry_count", POLICY["max_submit_retry_count"]),
        "policy.required_terminal_transfer": _value(
            "rulepack:required_terminal_step_id", required_terminal
        ),
    }
    calls_ok = apply_op("lte", count, POLICY["max_submit_calls_after_unknown"])
    retry_ok = apply_op("lte", retry, POLICY["max_submit_retry_count"])
    if calls_ok is None or retry_ok is None:
        return None
    conditions = [
        _cond(
            "submit_calls_after_unknown",
            "lte",
            "assertion",
            "run.invoice_submit_call_count",
            "policy.max_submit_calls_after_unknown",
            calls_ok,
            f"结果不明后提交调用次数={count}，上限={POLICY['max_submit_calls_after_unknown']}",
        ),
        _cond(
            "submit_retry_count_zero",
            "lte",
            "assertion",
            "run.max_submit_retry_count",
            "policy.max_submit_retry_count",
            retry_ok,
            f"提交步骤 retry_count 最大值={retry}，上限={POLICY['max_submit_retry_count']}",
        ),
    ]
    notes: list[str] = []
    if need_transfer:
        transfer_ok = apply_op("contains", terminals, required_terminal)
        if transfer_ok is None:
            return None
        conditions.append(
            _cond(
                "reached_transfer_to_human",
                "contains",
                "assertion",
                "run.terminal_step_ids",
                "policy.required_terminal_transfer",
                transfer_ok,
                f"终态步骤={terminals}，题内要求 {required_terminal}",
            )
        )
    violated = any(c["result"] is False for c in conditions if c["role"] == "assertion")
    notes.append(
        "工具返回结果不明属于接口侧异常；Agent 未重试并转人工，属于正确处理，不计故障。"
        if not violated
        else "结果不明时 Agent 继续重试并自行宣称成功：这是对工具错误的处理不当，不是工具拒绝。"
    )
    sources = _dedupe(_submit_event_ids(run) + _terminal_event_ids(run))
    return _Probe(conditions, observed, expected, sources, notes)


def _ordered_events(run: Run) -> tuple[Event, ...]:
    """按 **(seq, 原始下标)** 稳定排序的事件序列。

    审计 F-15 / R2-03：证据端曾用"数组里的最后一个"，上一轮改成 `(seq, event_id)`；
    但规则侧 `state.ordered_events` 用的是稳定排序（同 seq 保持**数组原序**），
    同 seq 且 id 字典序与数组序相反时，两端仍指向不同事件，出现
    "依据记录结论与判定相反却复核 VERIFIED"。这里改为与规则端完全同口径：
    `(seq, 事件在 run.events 里的下标)`。
    """
    indexed = sorted(enumerate(run.events), key=lambda item: (item[1].seq, item[0]))
    return tuple(event for _, event in indexed)


def _last_collect_event(run: Run) -> Event | None:
    found: Event | None = None
    for event in _ordered_events(run):
        if event.step_id == "COLLECT_INVOICE_DATA":
            found = event
    return found


def _submit_invoice_event_count(run: Run) -> int:
    return sum(1 for event in run.events if event.step_id == "SUBMIT_INVOICE")


def _stale_round_value_reused(rounds: Iterable[Any], actual: Any, expected: Any) -> bool:
    """实际使用的金额是否正好等于"被后续轮次覆盖"的旧轮次金额。

    只有 `money_equal` 明确为 False（确实不等）且明确命中某条被覆盖轮次时才为 True；
    任何一侧无法判断都返回 False（不构成"沿用旧值"的可证实事实）。
    """
    if money_equal(actual, expected) is not False:
        return False
    superseded = {
        round_item.get("supersedes_round")
        for round_item in rounds
        if isinstance(round_item, Mapping) and round_item.get("supersedes_round") is not None
    }
    if not superseded:
        return False
    for round_item in rounds:
        if not isinstance(round_item, Mapping):
            continue
        if round_item.get("round") not in superseded:
            continue
        if round_item.get("exclusive_amount") is None:
            continue
        if money_equal(actual, round_item.get("exclusive_amount")) is True:
            return True
    return False


def _probe_cancel_intent(case: Case, run: Run) -> _Probe | None:
    """R008：题内要求取消时，轨迹里必须出现取消步骤，且取消后不得再有提交。"""
    action = case.reference_result.get("required_action")
    acceptable = case.reference_result.get("acceptable_snapshot_state")
    if action != "CANCEL" and acceptable != "CANCELED":
        return None
    steps = [event.step_id for event in run.events]
    cancel_step = POLICY["required_cancel_step_id"]
    submit_count = _submit_invoice_event_count(run)
    cancel_result = apply_op("contains", steps, cancel_step)
    submit_result = apply_op("eq", submit_count, POLICY["max_submit_invoice_events_after_cancel"])
    if cancel_result is None or submit_result is None:
        return None
    observed = {
        "run.step_ids": _value("derived:run_step_ids", steps),
        "run.submit_invoice_event_count": _value("derived:submit_invoice_event_count", submit_count),
    }
    # 审计 D-09 / P4：两个字段各用真实键，且**只在存在时**写入值表。
    # 旧实现总写（值为 None 或错位），复核端按 ref 取不到值 → 诚实记录被判 INSUFFICIENT。
    if isinstance(action, str):
        observed["reference.required_action"] = _value("case.reference_result.required_action", action)
    if isinstance(acceptable, str):
        observed["reference.acceptable_snapshot_state"] = _value(
            "case.reference_result.acceptable_snapshot_state", acceptable
        )
    expected = {
        "policy.cancel_step_id": _value("rulepack:required_cancel_step_id", cancel_step),
        "policy.max_submit_invoice_events_after_cancel": _value(
            "rulepack:max_submit_invoice_events_after_cancel",
            POLICY["max_submit_invoice_events_after_cancel"],
        ),
    }
    conditions = [
        _cond(
            "cancel_task_present", "contains", "assertion", "run.step_ids", "policy.cancel_step_id",
            cancel_result, f"轨迹里是否出现 {cancel_step} 事件",
        ),
        _cond(
            "no_submit_after_cancel", "eq", "assertion",
            "run.submit_invoice_event_count", "policy.max_submit_invoice_events_after_cancel",
            submit_result, f"提交事件数={submit_count}，取消任务允许值="
                          f"{POLICY['max_submit_invoice_events_after_cancel']}",
        ),
    ]
    sources = _dedupe([
        event.event_id for event in run.events
        if event.step_id in ("PARSE_INTENT", cancel_step, "SUBMIT_INVOICE")
    ])
    return _Probe(conditions, observed, expected, sources)


def _probe_red_invoice_original(case: Case, run: Run) -> _Probe | None:
    """R014：红票题缺原票引用/金额时，Agent 应追问而不是继续出票。"""
    if case.reference_result.get("required_action") != "ASK_ORIGINAL_INVOICE_AND_AMOUNT":
        return None
    collect = _last_collect_event(run)
    if collect is None:
        return None
    original = collect.observed_output.get("original_invoice_id")
    amount = collect.observed_output.get("amount_value")
    proceeded = any(
        event.step_id in ("CONFIRM_INVOICE", "SUBMIT_INVOICE") for event in run.events
    )
    should_ask = bool(proceeded and (original is None or amount is None))
    result = apply_op("eq", should_ask, POLICY["should_not_proceed_without_required_fields"])
    if result is None:
        return None
    observed = {
        "collect.original_invoice_id": _value(
            _event_ref(collect.event_id, "observed_output", "original_invoice_id"), original
        ),
        "collect.amount_value": _value(
            _event_ref(collect.event_id, "observed_output", "amount_value"), amount
        ),
        "run.r014_should_have_asked": _value("derived:r014_should_have_asked", should_ask),
    }
    expected = {
        "policy.should_not_proceed_without_required_fields": _value(
            "rulepack:should_not_proceed_without_required_fields",
            POLICY["should_not_proceed_without_required_fields"],
        ),
        "reference.required_action": _value(
            "case.reference_result.required_action", "ASK_ORIGINAL_INVOICE_AND_AMOUNT"
        ),
    }
    conditions = [
        _cond(
            "agent_asked_before_invoicing", "eq", "assertion",
            "run.r014_should_have_asked", "policy.should_not_proceed_without_required_fields",
            result,
            f"原票引用={original!r}，金额={amount!r}，是否已进入确认/提交={proceeded}",
        )
    ]
    sources = _dedupe([collect.event_id] + _confirm_event_ids(run) + _submit_event_ids(run))
    return _Probe(conditions, observed, expected, sources)


def _probe_context_update(case: Case, run: Run, state: BusinessState) -> _Probe | None:
    """R015：多轮改价后，实际使用的金额恰好是被覆盖的旧轮次金额。"""
    rounds = case.context.get("conversation_rounds")
    if not isinstance(rounds, list) or len(rounds) < 2:
        return None
    superseded = {
        round_item.get("supersedes_round")
        for round_item in rounds
        if isinstance(round_item, Mapping) and round_item.get("supersedes_round") is not None
    }
    if not superseded:
        return None
    collect = _last_collect_event(run)
    expected_amount = case.reference_result.get("amount_value")
    actual = collect.observed_output.get("amount_value") if collect is not None else None
    if collect is None or expected_amount is None or actual is None:
        return None
    reused = _stale_round_value_reused(rounds, actual, expected_amount)
    result = apply_op("eq", reused, POLICY["uses_latest_round_value"])
    if result is None:
        return None
    observed = {
        "collect.amount_value": _value(
            _event_ref(collect.event_id, "observed_output", "amount_value"), actual
        ),
        "reference.amount_value": _value("case.reference_result.amount_value", expected_amount),
        "run.r015_stale_value_reused": _value("derived:r015_stale_value_reused", reused),
    }
    expected = {
        "policy.uses_latest_round_value": _value(
            "rulepack:uses_latest_round_value", POLICY["uses_latest_round_value"]
        ),
    }
    conditions = [
        _cond(
            "uses_latest_round_value", "eq", "assertion",
            "run.r015_stale_value_reused", "policy.uses_latest_round_value", result,
            f"实际金额={actual}，题内最新金额={expected_amount}，是否命中被覆盖的旧轮次={reused}",
        )
    ]
    sources = _dedupe([collect.event_id] + _confirm_event_ids(run))
    return _Probe(conditions, observed, expected, sources)


def _last_step_event_of(run: Run, step_id: str) -> Event | None:
    found: Event | None = None
    for event in _ordered_events(run):
        if event.step_id == step_id:
            found = event
    return found


def _business_stage_count(run: Run) -> int:
    return sum(1 for event in run.events if event.step_id in BUSINESS_STAGES)


def _expense_line_amounts(run: Run) -> list[float] | None:
    event = _last_step_event_of(run, "RECEIVE_CLAIM")
    if event is None:
        return None
    items = event.observed_output.get("line_items")
    if not isinstance(items, list) or not items:
        return None
    amounts: list[float] = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        amount = item.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        amounts.append(float(amount))
    return amounts


def _expense_approver_sequence(run: Run) -> list[str]:
    return [event.step_id.replace("APPROVE_", "") for event in run.events
            if event.step_id in ("APPROVE_MANAGER", "APPROVE_FINANCE")]


def _expense_duplicate_settlement(run: Run) -> bool:
    settle_seen = any(event.step_id == "SETTLE_EXPENSE" for event in run.events)
    claim = _last_step_event_of(run, "RECEIVE_CLAIM")
    settled_before = claim.observed_output.get("already_settled") if claim is not None else None
    return bool(settle_seen and settled_before is True)


#: 步骤名含这些词说明"可能承载业务事实"（与 adapters/diagnose 的口径一致）
_BUSINESS_STEP_HINTS: tuple[str, ...] = (
    "PARSE", "INTENT", "COLLECT", "VALIDATE", "CONFIRM", "SUBMIT", "PREVIEW",
    "CANCEL", "COMPLETE", "FAIL", "TIMEOUT", "TRANSFER", "RECEIVE", "CREATE",
    "RESOLVE", "CALLBACK", "INVOICE", "WAIT",
)


def _facts_available(case: Case, run: Run, keys: Iterable[str]) -> bool:
    """题内要求的业务事实是否**全部**可核对（唯一实现在 state，审计 R3-01/03）。"""
    from .state import fact_available, reconstruct

    state = reconstruct(case, run)
    return all(fact_available(state, case, key) for key in keys)


def _invalid_money_fields(case: Case, run: Run) -> list[str]:
    """"声称是金额但无法解析"的字段（唯一实现在 state，审计 R3-03）。"""
    from .state import money_field_issues, reconstruct

    return money_field_issues(reconstruct(case, run), case)


def _unrecognized_business_steps(run: Run) -> list[str]:
    from .contracts import STEP_IDS

    return sorted({
        event.step_id for event in run.events
        if event.step_id and event.step_id not in STEP_IDS
        and any(hint in event.step_id.upper() for hint in _BUSINESS_STEP_HINTS)
    })


def _probe_insufficient_evidence(case: Case, run: Run) -> _Probe | None:
    """R902：记录"为什么这条运行只能拒判"，而不是给出一个 Agent 失败结论。

    只有拒判条件成立时才产出记录（记录文档的是**证据缺口**），覆盖四条判据
    （与 `rules.check_missing_business_evidence` / 归因层的拒判口径一致）：
    1. 没有任何业务阶段事件；
    2. 金额字段"声称是金额"却无法解析；
    3. 已进入确认/提交却缺采集事件；
    4. 题内要求的业务事实没有**全部**可核对。
    """
    events = list(run.events)
    if not events:
        # 零事件轨迹没有任何事件可引用；记录格式要求 source_event_ids 非空，
        # 因此这里不产出记录（这一情形由评测结果本身表达）。见 docs/g6-evidence.md 的边界说明。
        return None
    stage_count = _business_stage_count(run)
    expected_keys = [
        key for key in ("amount_value", "buyer_id", "item_id")
        if case.reference_result.get(key) is not None
    ]
    expected_facts_available = _facts_available(case, run, expected_keys)
    proceeded = any(event.step_id in ("CONFIRM_INVOICE", "SUBMIT_INVOICE") for event in events)
    collect = _last_collect_event(run)
    from .state import missing_critical_stages, money_fields_present, reconstruct

    replay_state = reconstruct(case, run)
    invalid_money = _invalid_money_fields(case, run)
    unknown_steps = _unrecognized_business_steps(run)
    critical_missing = missing_critical_stages(replay_state, case)
    has_money = money_fields_present(replay_state, case)

    observed = {
        "run.step_ids": _value("derived:run_step_ids", [event.step_id for event in events]),
        "run.business_stage_count": _value("derived:run_business_stage_count", stage_count),
        "run.collect_event_present": _value("derived:collect_event_present", collect is not None),
        "run.r902_expected_fact_keys": _value("derived:r902_expected_fact_keys", expected_keys),
        "run.r902_invalid_money_fields": _value(
            "derived:r902_invalid_money_fields", invalid_money
        ),
        "run.r902_unrecognized_business_steps": _value(
            "derived:r902_unrecognized_business_steps", unknown_steps
        ),
    }
    expected = {
        "policy.min_business_stage_count": _value(
            "rulepack:min_business_stage_count", POLICY["min_business_stage_count"]
        ),
    }
    stage_ok = apply_op("gte", stage_count, POLICY["min_business_stage_count"])
    if stage_ok is None:
        return None
    conditions = [
        _cond(
            "has_business_stage", "gte", "assertion",
            "run.business_stage_count", "policy.min_business_stage_count", stage_ok,
            f"业务阶段数={stage_count}，至少需要={POLICY['min_business_stage_count']}",
        )
    ]

    # 判据 2：金额存在但无法解析（只有真的存在金额字段时才入表）
    money_ok = not invalid_money
    if has_money:
        observed["run.money_fields_parseable"] = _value("derived:r902_money_fields_parseable", money_ok)
        expected["policy.no_invalid_money"] = _value("rulepack:no_invalid_money", True)
        conditions.append(_cond(
            "money_fields_parseable", "eq", "assertion",
            "run.money_fields_parseable", "policy.no_invalid_money", money_ok,
            "金额字段存在但无法解析：" + ("、".join(invalid_money[:4]) if invalid_money else "无"),
        ))

    # 判据 3/4：已进入确认/提交时，采集事件与题内事实必须齐备
    if expected_keys and proceeded:
        collect_ok = apply_op("eq", collect is not None, True)
        expected["policy.collect_event_required"] = _value("rulepack:collect_event_required", True)
        if collect_ok is not None:
            conditions.append(_cond(
                "collect_event_present", "eq", "assertion",
                "run.collect_event_present", "policy.collect_event_required", collect_ok,
                f"已进入确认/提交，但采集事件是否存在={collect is not None}",
            ))
        fact_ok = apply_op("eq", expected_facts_available, True)
        observed["run.r902_required_fact_present"] = _value(
            "derived:r902_required_fact_present", expected_facts_available
        )
        expected["policy.required_fact_present"] = _value("rulepack:required_fact_present", True)
        if fact_ok is not None:
            conditions.append(_cond(
                "required_fact_present", "eq", "assertion",
                "run.r902_required_fact_present", "policy.required_fact_present", fact_ok,
                f"题内要求事实={expected_keys}，是否全部可核对={expected_facts_available}",
            ))

    # 判据 1b：未识别步骤只有在"顶替了缺失的关键业务阶段"时才构成拒判理由；
    # 关键阶段齐全时仅作 observation 披露（与 attribution 的 R2-01 判据一致）。
    if unknown_steps and critical_missing:
        observed["run.r902_unrecognized_business_steps_present"] = _value(
            "derived:r902_unrecognized_business_steps", unknown_steps
        )
        expected["policy.no_unrecognized_business_steps"] = _value(
            "rulepack:no_unrecognized_business_steps", True
        )
        conditions.append(_cond(
            "business_steps_recognized", "eq", "assertion",
            "run.r902_unrecognized_business_steps_present",
            "policy.no_unrecognized_business_steps",
            False,
            "关键阶段缺失（" + "、".join(critical_missing) + "）且存在未识别步骤："
            + "、".join(unknown_steps[:4]),
        ))
    elif unknown_steps:
        observed["run.r902_unrecognized_business_steps_disclosed"] = _value(
            "derived:r902_unrecognized_business_steps", unknown_steps
        )
        expected.setdefault("policy.no_unrecognized_business_steps", _value(
            "rulepack:no_unrecognized_business_steps", True
        ))
        conditions.append(_cond(
            "unrecognized_steps_disclosed", "observation", "observation",
            "run.r902_unrecognized_business_steps_disclosed",
            "policy.no_unrecognized_business_steps",
            True,
            "关键业务阶段齐全，未识别步骤仅作披露：" + "、".join(unknown_steps[:4]),
        ))
    else:
        observed.pop("run.r902_unrecognized_business_steps", None)

    if not expected_keys:
        observed.pop("run.r902_expected_fact_keys", None)

    if all(condition["result"] for condition in conditions):
        return None  # 证据充分：这不是一次拒判，不产出 R902 记录
    notes = ["R902 记录的是**拒判理由**：条件不成立说明证据不足，该运行被判 UNKNOWN，而不是 Agent 做错。"]
    return _Probe(conditions, observed, expected, _dedupe([event.event_id for event in events]), notes)


def _expense_probe(rule_id: str, case: Case, run: Run) -> _Probe | None:
    if rule_id == "EXP001":
        claim_event = _last_step_event_of(run, "RECEIVE_CLAIM")
        amounts = _expense_line_amounts(run)
        if claim_event is None or amounts is None:
            return None
        claim = claim_event.observed_output.get("claim_amount")
        result = apply_op("money_sum_eq", amounts, claim)
        if result is None:
            return None
        observed = {"derived.expense_line_amounts": _value("derived:expense_line_amounts", amounts)}
        expected = {
            "expense.claim_amount": _value(
                _event_ref(claim_event.event_id, "observed_output", "claim_amount"), claim
            )
        }
        conditions = [_cond(
            "claim_amount_equals_line_sum", "money_sum_eq", "assertion",
            "derived.expense_line_amounts", "expense.claim_amount", result,
            f"明细合计={round(sum(amounts), 2)}，申请金额={claim}",
        )]
        return _Probe(conditions, observed, expected, _dedupe([claim_event.event_id]))
    if rule_id == "EXP002":
        claim_event = _last_step_event_of(run, "RECEIVE_CLAIM")
        budget_event = _last_step_event_of(run, "CHECK_BUDGET")
        if claim_event is None or budget_event is None:
            return None
        claim = claim_event.observed_output.get("claim_amount")
        budget = budget_event.observed_output.get("budget_remaining")
        result = apply_op("lte", claim, budget)
        if result is None:
            return None
        observed = {
            "expense.claim_amount": _value(
                _event_ref(claim_event.event_id, "observed_output", "claim_amount"), claim
            )
        }
        expected = {
            "expense.budget_remaining": _value(
                _event_ref(budget_event.event_id, "observed_output", "budget_remaining"), budget
            )
        }
        conditions = [_cond(
            "claim_within_budget", "lte", "assertion",
            "expense.claim_amount", "expense.budget_remaining", result,
            f"申请金额={claim}，剩余预算={budget}",
        )]
        return _Probe(conditions, observed, expected,
                      _dedupe([claim_event.event_id, budget_event.event_id]))
    if rule_id == "EXP003":
        sequence = _expense_approver_sequence(run)
        if not sequence:
            return None
        required = POLICY["expense_required_approver_sequence"]
        result = apply_op("eq", sequence, required)
        if result is None:
            return None
        observed = {"derived.expense_approver_sequence": _value("derived:expense_approver_sequence", sequence)}
        expected = {
            "policy.expense_required_approver_sequence": _value(
                "rulepack:expense_required_approver_sequence", required
            )
        }
        conditions = [_cond(
            "approval_order_matches_process", "eq", "assertion",
            "derived.expense_approver_sequence", "policy.expense_required_approver_sequence", result,
            f"实际审批序列={sequence}，要求={required}",
        )]
        sources = _dedupe([event.event_id for event in run.events
                           if event.step_id in ("APPROVE_MANAGER", "APPROVE_FINANCE")])
        return _Probe(conditions, observed, expected, sources)
    if rule_id == "EXP004":
        claim_event = _last_step_event_of(run, "RECEIVE_CLAIM")
        settle_event = _last_step_event_of(run, "SETTLE_EXPENSE")
        settled_before = claim_event.observed_output.get("already_settled") if claim_event else None
        if settle_event is None and settled_before is not True:
            return None  # 没有结算动作，规则不适用
        duplicate = _expense_duplicate_settlement(run)
        result = apply_op("eq", duplicate, POLICY["duplicate_settlement_disallowed"])
        if result is None:
            return None
        observed = {
            "run.expense_settled_before": _value(
                _event_ref(claim_event.event_id, "observed_output", "already_settled"), settled_before
            ) if claim_event is not None else _value("derived:expense_duplicate_settlement", duplicate),
            "run.expense_duplicate_settlement": _value("derived:expense_duplicate_settlement", duplicate),
        }
        expected = {
            "policy.duplicate_settlement_disallowed": _value(
                "rulepack:duplicate_settlement_disallowed", POLICY["duplicate_settlement_disallowed"]
            )
        }
        conditions = [_cond(
            "no_duplicate_settlement", "eq", "assertion",
            "run.expense_duplicate_settlement", "policy.duplicate_settlement_disallowed", result,
            f"是否重复结算={duplicate}",
        )]
        sources = _dedupe([event.event_id for event in run.events
                           if event.step_id in ("RECEIVE_CLAIM", "SETTLE_EXPENSE")])
        return _Probe(conditions, observed, expected, sources)
    return None


def build_expense_records(case: Case, run: Run) -> list[EvidenceRecord]:
    """报销规则包的依据记录（与开票记录同一格式、同一检查器）。

    报销包是独立业务：不需要开票的 `BusinessState`，事实直接从事件里取。
    """
    from .rulepacks import EXPENSE_RULEPACK_VERSION

    digest = trace_digest(run)
    records: list[EvidenceRecord] = []
    for rule_id in EXPENSE_SUPPORTED_RULES:
        probe = _expense_probe(rule_id, case, run)
        if probe is None:
            continue
        conclusion = probe.conclusion
        if conclusion is None or not probe.conditions or not probe.source_ids:
            continue
        records.append(
            EvidenceRecord(
                case_id=case.case_id,
                run_id=run.run_id,
                rule_id=rule_id,
                rulepack_version=EXPENSE_RULEPACK_VERSION,
                evaluator_version=EVALUATOR_VERSION,
                source_event_ids=probe.source_ids,
                required_conditions=tuple(probe.conditions),
                observed_values=probe.observed,
                expected_values=probe.expected,
                conclusion=conclusion,
                trace_digest=digest,
                label=RULE_LABELS[rule_id],
                notes=("报销规则包（自造场景）：判定与开票规则无关，仅用于演示跨业务规则包。",),
            )
        )
    return records


def _probe_rule(rule_id: str, case: Case, run: Run, state: BusinessState) -> _Probe | None:
    if rule_id == "R001":
        return _probe_amount_basis(case, state)
    if rule_id == "R002":
        return _probe_amount_calculation(case, state)
    if rule_id == "R008":
        return _probe_cancel_intent(case, run)
    if rule_id == "R010":
        return _probe_confirmation(case, run)
    if rule_id == "R011":
        return _probe_tool_error(case, run)
    if rule_id == "R014":
        return _probe_red_invoice_original(case, run)
    if rule_id == "R015":
        return _probe_context_update(case, run, state)
    if rule_id == "R902":
        return _probe_insufficient_evidence(case, run)
    return None


# ---------------------------------------------------------------- 记录构建

def build_records(case: Case, run: Run, assessment: RunAssessment) -> list[EvidenceRecord]:
    """为一条运行生成受支持规则的依据记录（按 `SUPPORTED_RULES` 顺序）。

    只有"条件可复算 + 有可引用事件"时才产出记录；其余情况**不出记录**，
    因为拿不出复算路径的记录会把"无法判断"伪装成"已核实"。
    """
    state = assessment.state
    if state is None or assessment.case_id != case.case_id or assessment.run_id != run.run_id:
        state = reconstruct(case, run)

    violations: dict[str, Violation] = {}
    for violation in assessment.violations:
        violations.setdefault(violation.rule_id, violation)

    digest = trace_digest(run)
    records: list[EvidenceRecord] = []
    for rule_id in SUPPORTED_RULES:
        probe = _probe_rule(rule_id, case, run, state)
        if probe is None:
            continue
        conclusion = probe.conclusion
        if conclusion is None or not probe.conditions or not probe.source_ids:
            continue
        matched = violations.get(rule_id)
        notes = list(probe.notes)
        if matched is not None and conclusion == CONCLUSION_SATISFIED:
            notes.append("评测器把该规则判为违规，但依据记录复算未发现不成立的条件，需人工复核。")
        elif matched is None and conclusion == CONCLUSION_VIOLATED:
            notes.append("依据记录复算发现条件不成立，但评测器未给出该规则违规，需人工复核。")
        records.append(
            EvidenceRecord(
                case_id=case.case_id,
                run_id=run.run_id,
                rule_id=rule_id,
                rulepack_version=RULE_VERSION,
                evaluator_version=EVALUATOR_VERSION,
                source_event_ids=probe.source_ids,
                required_conditions=tuple(probe.conditions),
                observed_values=probe.observed,
                expected_values=probe.expected,
                conclusion=conclusion,
                trace_digest=digest,
                label=RULE_LABELS[rule_id],
                notes=tuple(notes),
            )
        )
    return records


def build_records_for_result(result: Any) -> list[EvidenceRecord]:
    """批量入口：`EvaluationResult` → 全部依据记录（CLI 只做薄封装时用）。"""
    record: list[EvidenceRecord] = []
    for case_result in getattr(result, "results", []):
        case = case_result.case
        for assessment in getattr(case_result, "assessments", []):
            run = _run_of(case, assessment)
            if run is None:
                continue
            record += build_records(case, run, assessment)
    return record


def _run_of(case: Case, assessment: RunAssessment) -> Run | None:
    state = getattr(assessment, "state", None)
    candidate = getattr(state, "run", None)
    if isinstance(candidate, Run):
        return candidate
    return case.run(assessment.run_id)


# ---------------------------------------------------------------- 独立产物 IO

def write_records(records: Iterable[EvidenceRecord], path: Path | str) -> Path:
    """把依据记录写成 JSONL（**独立产物**，不是 submission）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) for record in records]
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
    return target


def read_records(path: Path | str) -> list[EvidenceRecord]:
    """读回 JSONL 依据记录（空行忽略）。"""
    text = Path(path).read_text(encoding="utf-8")
    return [EvidenceRecord.from_dict(json.loads(line)) for line in text.splitlines() if line.strip()]
