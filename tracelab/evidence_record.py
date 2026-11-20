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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .attribution import RunAssessment
from .contracts import Case, Event, Run
from .evaluation import EVALUATOR_VERSION
from .rules import RULE_VERSION, Violation
from .state import parse_money, reconstruct

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

# 探针拆到 evidence_probes，公开名在此 re-export。
from .evidence_probes import _probe_rule, build_expense_records  # noqa: E402,F401
