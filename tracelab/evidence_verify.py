"""G6-B2：依据记录的**独立检查器**。

独立性契约（本模块的硬约束）
============================
1. **不调用** `attribution.assess_run` / `evaluation.evaluate_dataset`，也不读
   `RunAssessment`：结论只能由本模块从原始 `Case` / `Run` / `Event` 重新推出。
2. 记录里的 `observed_values` / `expected_values` **只被当作"从哪里取值"的线索**
   （`ref`），取值一律回到原始 `Event.observed_input/output/tool_calls` 重新取。
3. 条件运算与结论推导在本地重做：`apply_op` 只提供运算符语义（记录端与复核端共用，
   避免两边对"金额相等"的理解漂移），操作数永远是**刚取出的原始值**。

状态定义
--------
* `VERIFIED`     —— 所有检查通过：字段齐全、版本一致、摘要一致、取值可重取且与记录一致、
                    条件复算结果与记录一致、由复算结果推出的结论与记录一致。
* `MISMATCH`     —— **已证明**记录与原始轨迹/规则不符（值被改、结论被改、摘要不符、
                    引用了无关事件、规则版本不同……）。有确证冲突时优先报 `MISMATCH`。
* `INSUFFICIENT` —— 无法判定：缺字段、缺事件、取值地址失效、条件不可解析。

规则版本策略（要写清并锁定在测试里）
------------------------------------
`rulepack_version` 参数缺省取当前 `tracelab.rules.RULE_VERSION`。记录的
`rulepack_version` 与它不一致 → **`MISMATCH`**：结论是在另一套判据下推出来的，
"同一结论"不再是同一件事，不能给出 VERIFIED。调用方可以用参数显式指定期望版本
（例如复核历史记录时传入历史版本号）。
`evaluator_version` 不一致只在 `detail` 里说明，不改变状态：复核只依赖原始轨迹。

检查项
------
`record_fields_present` / `case_binding` / `run_binding` / `rule_id_supported` /
`rulepack_version` / `trace_digest` / `source_event_ids_present` /
`source_event_ids_cover_refs` / `source_events_relevant` /
`observed_values_present` / `expected_values_present` / `observed_values[<key>]` /
`expected_values[<key>]` / `required_conditions_present` / `condition:<name>` /
`conclusion_recomputed` / `conclusion_matches` / `label_matches_rule`。
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .attribution import GUARD_ERROR_CODES
from .contracts import STEP_IDS, TERMINAL_STEPS, Case, Event, Run
from .evaluation import EVALUATOR_VERSION
from .evidence_record import (
    ALL_SUPPORTED_RULES,
    CONCLUSIONS,
    MISSING,
    POLICY,
    RULE_CONDITION_PATTERNS,
    RULE_LABELS,
    EvidenceRecord,
    apply_op,
    normalize_value,
    trace_digest,
)
from .rules import RULE_VERSION
from .state import fact_available, money_field_issues, reconstruct

STATUS_VERIFIED = "VERIFIED"
STATUS_MISMATCH = "MISMATCH"
STATUS_INSUFFICIENT = "INSUFFICIENT"
STATUSES: tuple[str, ...] = (STATUS_VERIFIED, STATUS_MISMATCH, STATUS_INSUFFICIENT)

#: 检查器自己的"业务阶段"定义（用于 R902 拒判复核；刻意不 import 规则模块的同名常量）。
_BUSINESS_STAGES: tuple[str, ...] = (
    "PARSE_INTENT",
    "COLLECT_INVOICE_DATA",
    "VALIDATE_DATA",
    "CONFIRM_INVOICE",
    "SUBMIT_INVOICE",
    "WAIT_INVOICE_CALLBACK",
)

#: 检查器自己的报销场景步骤定义（用于 EXP 系列的相关事件范围）。
_EXPENSE_STEPS: tuple[str, ...] = (
    "RECEIVE_CLAIM",
    "CHECK_BUDGET",
    "APPROVE_MANAGER",
    "APPROVE_FINANCE",
    "SETTLE_EXPENSE",
)

#: 一条记录必须具备的字段（空字符串/空列表/空表都算"缺"）。
REQUIRED_RECORD_FIELDS: tuple[str, ...] = (
    "case_id",
    "run_id",
    "rule_id",
    "label",
    "rulepack_version",
    "evaluator_version",
    "source_event_ids",
    "required_conditions",
    "observed_values",
    "expected_values",
    "conclusion",
    "trace_digest",
)


# ---------------------------------------------------------------- 结果模型

@dataclass(frozen=True)
class CheckResult:
    """单项检查结果。`ok is None` 表示"无法判定"（记入 unverifiable_fields）。"""

    name: str
    kind: str
    ok: bool | None
    detail: str = ""


@dataclass(frozen=True)
class VerificationResult:
    """一条记录的复核结果。"""

    case_id: str
    run_id: str
    rule_id: str
    status: str
    checks: tuple[CheckResult, ...]
    unverifiable_fields: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "rule_id": self.rule_id,
            "status": self.status,
            "detail": self.detail,
            "unverifiable_fields": list(self.unverifiable_fields),
            "checks": [
                {"name": c.name, "kind": c.kind, "ok": c.ok, "detail": c.detail} for c in self.checks
            ],
        }


@dataclass(frozen=True)
class BatchVerification:
    """批量复核结果与统计（VERIFIED / MISMATCH / INSUFFICIENT 各多少）。"""

    results: tuple[VerificationResult, ...]
    counts: Mapping[str, int]
    by_rule: Mapping[str, Mapping[str, int]]

    @property
    def total(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "counts": dict(self.counts),
            "by_rule": {rule: dict(values) for rule, values in self.by_rule.items()},
            "records": [result.to_dict() for result in self.results],
        }


# ---------------------------------------------------------------- 取值地址

def _split_path(path: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    depth = 0
    for char in path:
        if char == "[":
            depth += 1
            buf.append(char)
        elif char == "]":
            depth = max(0, depth - 1)
            buf.append(char)
        elif char == "." and depth == 0:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    segments.append("".join(buf))
    return [segment for segment in segments if segment]


def _get(obj: Any, key: str) -> Any:
    # 审计 D-03：ref 来自（可能被篡改的）记录，不能让它遍历 dunder/私有属性，
    # 否则 `case.__init__.__globals__` 之类可以把任意进程内对象当作"证据来源"。
    if key.startswith("_"):
        return MISSING
    if isinstance(obj, Mapping):
        return obj[key] if key in obj else MISSING
    if isinstance(obj, (list, tuple)):
        return MISSING
    if hasattr(obj, key):
        return getattr(obj, key)
    return MISSING


def _navigate(obj: Any, segment: str) -> Any:
    if obj is MISSING:
        return MISSING
    if segment.endswith("]") and "[" in segment:
        base, _, inner = segment.partition("[")
        inner = inner[:-1]
        if base:
            obj = _get(obj, base)
        if obj is MISSING or not isinstance(obj, (list, tuple)):
            return MISSING
        if inner.isdigit():
            position = int(inner)
            return obj[position] if position < len(obj) else MISSING
        for item in obj:
            if getattr(item, "name", None) == inner:
                return item
        return MISSING
    return _get(obj, segment)


def _walk(obj: Any, path: str) -> Any:
    current = obj
    for segment in _split_path(path):
        current = _navigate(current, segment)
        if current is MISSING:
            return MISSING
    return current


def _submit_unknown(event: Event) -> bool:
    """提交结果是否显式不明（与规则侧同义，这里是检查器自己的实现）。"""
    if event.step_status == "TIMED_OUT":
        return True
    if (event.error_code or "").startswith("SUBMIT_TIMEOUT"):
        return True
    status = event.observed_output.get("submit_status")
    return isinstance(status, str) and status.lower() == "unknown"


def _last_step_event(events: Sequence[Event], step_id: str) -> Event | None:
    """取最后一个匹配事件：`(seq, 原始下标)`，与规则侧/记录端完全同口径（R2-03）。"""
    indexed = sorted(enumerate(events), key=lambda item: (item[1].seq, item[0]))
    found: Event | None = None
    for _, event in indexed:
        if event.step_id == step_id:
            found = event
    return found


def _derive(name: str, run: Run, case: Case | None = None) -> Any:
    """从原始事件序列重算导出值（检查器自己的实现，不复用记录端的值）。"""
    events = list(run.events)
    if name == "invoice_submit_call_count":
        return sum(1 for event in events for call in event.tool_calls if call.name.startswith("invoice.submit"))
    if name == "max_submit_retry_count":
        return max((e.retry_count for e in events if e.step_id == "SUBMIT_INVOICE"), default=0)
    if name == "submit_unknown_event_ids":
        return [e.event_id for e in events if e.step_id == "SUBMIT_INVOICE" and _submit_unknown(e)]
    if name == "submit_event_ids":
        return [e.event_id for e in events if e.step_id == "SUBMIT_INVOICE"]
    if name == "guard_error_codes":
        codes = {
            e.error_code
            for e in events
            if (e.error_code or "") in GUARD_ERROR_CODES and e.step_id not in TERMINAL_STEPS
        }
        return sorted(code for code in codes if code)
    if name == "terminal_step_ids":
        return [e.step_id for e in events if e.step_id in TERMINAL_STEPS]
    if name == "run_step_ids":
        return [e.step_id for e in events]
    if name == "submit_invoice_event_count":
        return sum(1 for e in events if e.step_id == "SUBMIT_INVOICE")
    if name == "r014_should_have_asked":
        # 红票题：缺原票引用/金额却已进入确认或提交 → 该追问而未追问
        collect = _last_step_event(events, "COLLECT_INVOICE_DATA")
        if collect is None:
            return MISSING
        original = collect.observed_output.get("original_invoice_id")
        amount = collect.observed_output.get("amount_value")
        proceeded = any(e.step_id in ("CONFIRM_INVOICE", "SUBMIT_INVOICE") for e in events)
        return bool(proceeded and (original is None or amount is None))
    if name == "r015_stale_value_reused":
        # 多轮改价：实际金额既不等于最新金额、又正好等于某条被覆盖轮次的金额
        if case is None:
            return MISSING
        rounds = case.context.get("conversation_rounds")
        if not isinstance(rounds, list) or len(rounds) < 2:
            return False
        collect = _last_step_event(events, "COLLECT_INVOICE_DATA")
        if collect is None:
            return MISSING
        actual = collect.observed_output.get("amount_value")
        expected = case.reference_result.get("amount_value")
        if actual is None or expected is None:
            return MISSING
        if apply_op("money_eq", actual, expected) is not False:
            return False
        superseded = {
            item.get("supersedes_round")
            for item in rounds
            if isinstance(item, Mapping) and item.get("supersedes_round") is not None
        }
        for item in rounds:
            if not isinstance(item, Mapping) or item.get("round") not in superseded:
                continue
            stale = item.get("exclusive_amount")
            if stale is not None and apply_op("money_eq", actual, stale) is True:
                return True
        return False
    # ---- R902 拒判证据（检查器自己重算"证据是否真的不足"） ----
    if name == "run_business_stage_count":
        return sum(1 for event in events if event.step_id in _BUSINESS_STAGES)
    if name == "collect_event_present":
        return _last_step_event(events, "COLLECT_INVOICE_DATA") is not None
    if name == "r902_expected_fact_keys":
        if case is None:
            return MISSING
        return [key for key in ("amount_value", "buyer_id", "item_id")
                if case.reference_result.get(key) is not None]
    if name == "r902_required_fact_present":
        # R3-01：与 rules / record 共用同一份判定（state.fact_available），
        # 复核端独立的是"从轨迹重取值"，不是"用另一套口径"。
        if case is None:
            return MISSING
        state = reconstruct(case, run)
        keys = [key for key in ("amount_value", "buyer_id", "item_id")
                if case.reference_result.get(key) is not None]
        return all(fact_available(state, case, key) for key in keys)
    if name == "r902_invalid_money_fields":
        if case is None:
            return MISSING
        return money_field_issues(reconstruct(case, run), case)
    if name == "r902_money_fields_parseable":
        fields = _derive("r902_invalid_money_fields", run, case)
        if fields is MISSING:
            return MISSING
        return len(fields) == 0
    if name == "r902_unrecognized_business_steps":
        hints = ("PARSE", "INTENT", "COLLECT", "VALIDATE", "CONFIRM", "SUBMIT", "PREVIEW",
                 "CANCEL", "COMPLETE", "FAIL", "TIMEOUT", "TRANSFER", "RECEIVE", "CREATE",
                 "RESOLVE", "CALLBACK", "INVOICE", "WAIT")
        return sorted({
            event.step_id for event in events
            if event.step_id and event.step_id not in STEP_IDS
            and any(hint in event.step_id.upper() for hint in hints)
        })
    # ---- 报销规则包（G6-C）：事实全部由检查器自己从事件重算 ----
    if name == "expense_line_amounts":
        claim = _last_step_event(events, "RECEIVE_CLAIM")
        if claim is None:
            return MISSING
        items = claim.observed_output.get("line_items")
        if not isinstance(items, list) or not items:
            return MISSING
        amounts: list[float] = []
        for item in items:
            if not isinstance(item, Mapping):
                return MISSING
            amount = item.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                return MISSING
            amounts.append(float(amount))
        return amounts
    if name == "expense_approver_sequence":
        return [event.step_id.replace("APPROVE_", "") for event in events
                if event.step_id in ("APPROVE_MANAGER", "APPROVE_FINANCE")]
    if name == "expense_duplicate_settlement":
        claim = _last_step_event(events, "RECEIVE_CLAIM")
        settled_before = claim.observed_output.get("already_settled") if claim is not None else None
        settle_seen = any(event.step_id == "SETTLE_EXPENSE" for event in events)
        return bool(settle_seen and settled_before is True)
    return MISSING


def resolve_ref(ref: str, case: Case, run: Run) -> Any:
    """按模块文档的 ref 语法取原始值；无法解析时返回 `MISSING`。"""
    if not isinstance(ref, str) or not ref:
        return MISSING
    if ref.startswith("case."):
        return _walk(case, ref[len("case."):])
    if ref.startswith("run."):
        return _walk(run, ref[len("run."):])
    if ref.startswith("event:"):
        event_id, _, path = ref[len("event:"):].partition(":")
        event = run.event_by_id().get(event_id)
        return MISSING if event is None else _walk(event, path)
    if ref.startswith("derived:"):
        return _derive(ref[len("derived:"):], run, case)
    if ref.startswith("rulepack:"):
        return POLICY.get(ref[len("rulepack:"):], MISSING)
    return MISSING


# ---------------------------------------------------------------- 小工具

def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, Mapping)):
        return len(value) == 0
    return False


def _as_data(record: EvidenceRecord | Mapping[str, Any]) -> tuple[Mapping[str, Any], list[str]]:
    if isinstance(record, EvidenceRecord):
        data: Mapping[str, Any] = record.to_dict()
    elif isinstance(record, Mapping):
        data = record
    else:
        data = {}
    missing = [name for name in REQUIRED_RECORD_FIELDS if _is_empty(data.get(name))]
    return data, missing


def _value_table(raw: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, Mapping)}


def _fresh_operand(
    table: Mapping[str, Mapping[str, Any]],
    keys: Any,
    case: Case,
    run: Run,
    section: str,
) -> tuple[Any, str | None]:
    """按记录里的 key → ref 回到原始轨迹取值；失败时返回原因。"""
    if isinstance(keys, (list, tuple)):
        values = []
        for key in keys:
            value, error = _fresh_operand(table, key, case, run, section)
            if error is not None:
                return MISSING, error
            values.append(value)
        return values, None
    if not isinstance(keys, str) or not keys:
        return MISSING, "条件没有指明操作数"
    entry = table.get(keys)
    if not isinstance(entry, Mapping):
        return MISSING, f"{section} 缺少 key {keys}"
    ref = entry.get("ref")
    if not isinstance(ref, str) or not ref:
        return MISSING, f"{section}[{keys}] 缺少取值地址"
    value = resolve_ref(ref, case, run)
    if value is MISSING:
        return MISSING, f"{section}[{keys}] 取值地址无法解析：{ref}"
    return value, None


def _expected_pack_version(rule_id: str) -> str:
    """按 rule_id 判定它应当来自哪个规则包（报销 EXP* / 其余开票）。"""
    if rule_id.startswith("EXP"):
        from .rulepacks import EXPENSE_RULEPACK_VERSION

        return EXPENSE_RULEPACK_VERSION
    return RULE_VERSION


def _relevant_event_ids(rule_id: str, run: Run) -> set[str] | None:
    """该规则"相关事件"的独立定义：被单一合法的 record 不该引用无关事件。"""
    events = list(run.events)
    steps: tuple[str, ...]
    if rule_id in ("R001", "R002"):
        steps = ("COLLECT_INVOICE_DATA", "SUBMIT_INVOICE", "CONFIRM_INVOICE")
        return {e.event_id for e in events if e.step_id in steps}
    if rule_id == "R010":
        steps = ("SUBMIT_INVOICE", "CONFIRM_INVOICE", *TERMINAL_STEPS)
        guards = {e.event_id for e in events if (e.error_code or "") in GUARD_ERROR_CODES}
        return {e.event_id for e in events if e.step_id in steps} | guards
    if rule_id == "R011":
        steps = ("SUBMIT_INVOICE", *TERMINAL_STEPS)
        return {e.event_id for e in events if e.step_id in steps}
    if rule_id == "R008":
        # 取消误判：只允许引用"意图解析 / 取消 / 提交"这几类事件
        steps = ("PARSE_INTENT", POLICY["required_cancel_step_id"], "SUBMIT_INVOICE")
        return {e.event_id for e in events if e.step_id in steps}
    if rule_id == "R014":
        # 红票原票：只允许引用采集、确认与提交事件
        steps = ("COLLECT_INVOICE_DATA", "CONFIRM_INVOICE", "SUBMIT_INVOICE")
        return {e.event_id for e in events if e.step_id in steps}
    if rule_id == "R015":
        # 多轮旧值：只允许引用采集与确认事件
        steps = ("COLLECT_INVOICE_DATA", "CONFIRM_INVOICE")
        return {e.event_id for e in events if e.step_id in steps}
    if rule_id == "R902":
        # 拒判：允许引用**任意**事件——"证据不足"本身就是对整条轨迹的判断
        return {e.event_id for e in events}
    if rule_id in ("EXP001", "EXP002", "EXP003", "EXP004"):
        return {e.event_id for e in events if e.step_id in _EXPENSE_STEPS}
    return None


# ---------------------------------------------------------------- 单条复核

def verify_record(
    record: EvidenceRecord | Mapping[str, Any],
    case: Case,
    run: Run,
    *,
    rulepack_version: str | None = None,
) -> VerificationResult:
    """独立复核一条依据记录。

    `record` 可以是 `EvidenceRecord`，也可以是（可能被篡改过的）dict —— 检查器不信任
    记录里的任何值，只信任 trace。版本/状态语义见模块文档。
    """
    data, missing_fields = _as_data(record)
    checks: list[CheckResult] = []
    failures: list[str] = []
    unverifiable: list[str] = []

    def add(name: str, kind: str, ok: bool | None, detail: str = "") -> None:
        checks.append(CheckResult(name=name, kind=kind, ok=ok, detail=detail))
        if ok is False:
            failures.append(f"{name}（{detail}）" if detail else name)
        elif ok is None:
            unverifiable.append(name)

    case_id = str(data.get("case_id") or "")
    run_id = str(data.get("run_id") or "")
    rule_id = str(data.get("rule_id") or "")

    # 1. 字段完整性 ------------------------------------------------------
    if missing_fields:
        checks.append(CheckResult("record_fields_present", "field", None, "缺字段：" + "、".join(missing_fields)))
        unverifiable.extend(missing_fields)
    else:
        checks.append(CheckResult("record_fields_present", "field", True, "必填字段齐全"))

    # 2. 绑定与规则支持 --------------------------------------------------
    add(
        "case_binding",
        "binding",
        (case_id == case.case_id) if case_id else None,
        f"记录={case_id or '（空）'}，数据={case.case_id}",
    )
    add(
        "run_binding",
        "binding",
        (run_id == run.run_id) if run_id else None,
        f"记录={run_id or '（空）'}，数据={run.run_id}",
    )
    if rule_id in ALL_SUPPORTED_RULES:
        add("rule_id_supported", "field", True, rule_id)
    else:
        add("rule_id_supported", "field", None, f"规则 {rule_id or '（空）'} 不在可复算规则集内")

    # 3. 规则版本（不一致 → MISMATCH，见模块文档） -----------------------
    # 记录属于哪个规则包由 rule_id 决定：EXP* 是报销规则包，其余是开票规则包。
    # 调用方仍可用 rulepack_version 显式覆盖（例如对抗性复核）。
    effective_pack = rulepack_version or _expected_pack_version(rule_id)
    recorded_pack = str(data.get("rulepack_version") or "")
    add(
        "rulepack_version",
        "version",
        (recorded_pack == effective_pack) if recorded_pack else None,
        f"记录={recorded_pack or '（空）'}，复核={effective_pack}",
    )
    evaluator_note = ""
    recorded_evaluator = str(data.get("evaluator_version") or "")
    if recorded_evaluator and recorded_evaluator != EVALUATOR_VERSION:
        evaluator_note = f"记录由 {recorded_evaluator} 产出（当前 {EVALUATOR_VERSION}）；复核只依赖原始轨迹，状态不受影响。"

    # 4. 轨迹摘要 --------------------------------------------------------
    fresh_digest = trace_digest(run)
    recorded_digest = str(data.get("trace_digest") or "")
    if not recorded_digest:
        add("trace_digest", "trace", None, "记录没有轨迹摘要，无法绑定到具体 trace")
    else:
        digest_ok = recorded_digest == fresh_digest
        add("trace_digest", "trace", digest_ok, "" if digest_ok else "记录摘要与当前轨迹不一致：记录不属于这条 trace")

    # 5. 证据事件存在性 --------------------------------------------------
    index = run.event_by_id()
    raw_ids = data.get("source_event_ids")
    ids = [str(item) for item in raw_ids] if isinstance(raw_ids, (list, tuple)) else []
    unknown_ids = [item for item in ids if item not in index]
    if not ids:
        add("source_event_ids_present", "source", None, "记录未引用任何事件")
    elif unknown_ids:
        add("source_event_ids_present", "source", None, "引用了轨迹中不存在的事件：" + "、".join(unknown_ids))
    else:
        add("source_event_ids_present", "source", True, f"{len(ids)} 个事件全部存在于轨迹")

    # 6. 取值重取（observed / expected） ---------------------------------
    observed_map = _value_table(data.get("observed_values"))
    expected_map = _value_table(data.get("expected_values"))
    for name, table in (("observed_values", observed_map), ("expected_values", expected_map)):
        if not table:
            add(f"{name}_present", "value", None, "记录缺少值表")
        else:
            add(f"{name}_present", "value", True, f"{len(table)} 个取值条目")
    cited_events: set[str] = set()
    for name, table in (("observed_values", observed_map), ("expected_values", expected_map)):
        for key in sorted(table):
            label = f"{name}[{key}]"
            entry = table[key]
            ref = entry.get("ref")
            if not isinstance(ref, str) or not ref:
                add(label, "value", None, "缺少可机读的取值地址")
                continue
            if ref.startswith("event:"):
                parts = ref.split(":", 2)
                if len(parts) > 1:
                    cited_events.add(parts[1])
            fresh = resolve_ref(ref, case, run)
            if fresh is MISSING:
                add(label, "value", None, f"取值地址无法解析：{ref}")
                continue
            if "value" not in entry:
                add(label, "value", None, "记录缺少 value")
                continue
            recorded_value = entry["value"]
            same = normalize_value(fresh) == normalize_value(recorded_value)
            add(label, "value", same, "" if same else f"记录={recorded_value!r}，原始轨迹={fresh!r}")

    # 7. 引用与规则相关度 ------------------------------------------------
    uncited = sorted(cited_events - set(ids))
    if cited_events:
        add(
            "source_event_ids_cover_refs",
            "source",
            not uncited,
            "" if not uncited else "条件引用了未列入 source_event_ids 的事件：" + "、".join(uncited),
        )
    else:
        add("source_event_ids_cover_refs", "source", True, "记录没有事件级取值地址（全部是规则常量或导出值）")
    relevant = _relevant_event_ids(rule_id, run)
    if relevant is None:
        add("source_events_relevant", "source", None, "该规则没有事件相关度定义，无法核对引用范围")
    else:
        off_topic = [item for item in ids if item in index and item not in relevant]
        add(
            "source_events_relevant",
            "source",
            not off_topic,
            "" if not off_topic else "引用了与规则无关的事件：" + "、".join(off_topic),
        )

    # 8. 条件复算（操作数一律重新取） ------------------------------------
    raw_conditions = data.get("required_conditions")
    conditions = (
        [item for item in raw_conditions if isinstance(item, Mapping)]
        if isinstance(raw_conditions, (list, tuple))
        else []
    )
    if not conditions:
        add("required_conditions_present", "condition", None, "记录没有可复算条件")
    results: dict[int, bool | None] = {}
    assertions: list[tuple[int, str]] = []
    for position, condition in enumerate(conditions):
        name = str(condition.get("name") or f"condition[{position}]")
        role = str(condition.get("role") or "assertion")
        op = str(condition.get("op") or "")
        label = f"condition:{name}"
        if role not in ("assertion", "observation"):
            add(label, "condition", None, f"未知条件角色：{role}")
            if role == "assertion":
                assertions.append((position, name))
                results[position] = None
            continue
        if role == "assertion":
            assertions.append((position, name))
        observed_value, observed_error = _fresh_operand(
            observed_map, condition.get("observed"), case, run, "observed_values"
        )
        expected_value, expected_error = _fresh_operand(
            expected_map, condition.get("expected"), case, run, "expected_values"
        )
        if observed_error or expected_error:
            add(label, "condition", None, observed_error or expected_error or "操作数无法取值")
            results[position] = None
            continue
        computed = apply_op(op, observed_value, expected_value)
        if computed is None:
            add(label, "condition", None, f"无法复算（op={op}，操作数不可解析）")
            results[position] = None
            continue
        results[position] = computed
        declared = condition.get("result")
        if not isinstance(declared, bool):
            add(label, "condition", None, "条件缺少布尔 result，无法核对")
            continue
        same = declared == computed
        add(label, "condition", same, "" if same else f"记录={declared}，复算={computed}")

    # 9. 结论重推 --------------------------------------------------------
    recomputed: str | None
    if not assertions:
        add("conclusion_recomputed", "conclusion", None, "记录没有 assertion 条件，无法独立推出结论")
        recomputed = None
    elif any(results.get(position) is None for position, _ in assertions):
        add("conclusion_recomputed", "conclusion", None, "存在无法复算的 assertion 条件")
        recomputed = None
    else:
        failed = [name for position, name in assertions if results.get(position) is False]
        recomputed = "VIOLATED" if failed else "SATISFIED"
        add(
            "conclusion_recomputed",
            "conclusion",
            True,
            f"复算结论={recomputed}" + (f"；不成立的条件：{'、'.join(failed)}" if failed else ""),
        )
    declared_conclusion = str(data.get("conclusion") or "").strip().upper()
    if declared_conclusion not in CONCLUSIONS:
        add("conclusion_matches", "conclusion", None, f"记录结论取值非法：{declared_conclusion or '（空）'}")
    elif recomputed is None:
        add("conclusion_matches", "conclusion", None, "复算结论不可得，无法核对")
    else:
        add(
            "conclusion_matches",
            "conclusion",
            declared_conclusion == recomputed,
            "" if declared_conclusion == recomputed else f"记录={declared_conclusion}，复算={recomputed}",
        )

    # 10. 标签一致性 -----------------------------------------------------
    expected_label = RULE_LABELS.get(rule_id)
    recorded_label = str(data.get("label") or "")
    if expected_label is not None and recorded_label:
        add(
            "label_matches_rule",
            "field",
            recorded_label == expected_label,
            "" if recorded_label == expected_label else f"记录={recorded_label}，应为={expected_label}",
        )

    # 10.1 规则-判据绑定（审计 F-05）------------------------------------
    # 只有"label 与自称的 rule_id 自洽"是不够的：把一条真实 R001 记录的两个字段
    # 改成 R002，其余不动，旧检查会给 VERIFIED。这里要求记录的条件集合必须属于
    # 该规则允许的判据集合（记录不能自己声明"我是哪条规则"就完事）。
    allowed_patterns = RULE_CONDITION_PATTERNS.get(rule_id)
    if allowed_patterns is not None:
        condition_names = {
            str(item.get("name") or "")
            for item in (data.get("required_conditions") or [])
            if isinstance(item, Mapping)
        }
        unexpected = sorted(
            name for name in condition_names
            if name and not any(fnmatch.fnmatchcase(name, pattern) for pattern in allowed_patterns)
        )
        add(
            "rule_conditions_match",
            "binding",
            not unexpected,
            "" if not unexpected
            else f"记录条件 {unexpected} 不属于 {rule_id} 的判据集合（疑似把别的规则记录改标）",
        )

    # 11. 汇总 -----------------------------------------------------------
    if failures:
        status = STATUS_MISMATCH
        detail = f"{rule_id or '?'}：" + "；".join(failures[:3])
    elif unverifiable:
        status = STATUS_INSUFFICIENT
        detail = f"{rule_id or '?'}：证据不足，无法复核：" + "、".join(dict.fromkeys(unverifiable))[:200]
    else:
        status = STATUS_VERIFIED
        detail = f"{rule_id}：{len(assertions)} 项断言全部复算成立，结论 {recomputed} 与原始轨迹一致。"
    if evaluator_note:
        detail = detail + " " + evaluator_note
    return VerificationResult(
        case_id=case_id,
        run_id=run_id,
        rule_id=rule_id,
        status=status,
        checks=tuple(checks),
        unverifiable_fields=tuple(dict.fromkeys(unverifiable)),
        detail=detail[:400],
    )


# ---------------------------------------------------------------- 批量复核

def _batch(results: Sequence[VerificationResult]) -> BatchVerification:
    counts = {status: 0 for status in STATUSES}
    by_rule: dict[str, dict[str, int]] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        slot = by_rule.setdefault(result.rule_id, {status: 0 for status in STATUSES})
        slot[result.status] = slot.get(result.status, 0) + 1
    return BatchVerification(tuple(results), counts, by_rule)


def _unverified(data: Mapping[str, Any], detail: str) -> VerificationResult:
    return VerificationResult(
        case_id=str(data.get("case_id") or ""),
        run_id=str(data.get("run_id") or ""),
        rule_id=str(data.get("rule_id") or ""),
        status=STATUS_INSUFFICIENT,
        checks=(),
        unverifiable_fields=("run",),
        detail=detail,
    )


def verify_records(
    records: Iterable[EvidenceRecord | Mapping[str, Any]],
    case: Case,
    run: Run | None = None,
    *,
    rulepack_version: str | None = None,
) -> BatchVerification:
    """批量复核。`run` 省略时按记录里的 `run_id` 从 `case` 里找。"""
    results: list[VerificationResult] = []
    for record in records:
        data, _ = _as_data(record)
        target = run
        if target is None:
            target = case.run(str(data.get("run_id") or ""))
        if target is None:
            results.append(_unverified(data, "找不到记录引用的运行，无法复核"))
            continue
        results.append(verify_record(record, case, target, rulepack_version=rulepack_version))
    return _batch(results)


def verify_records_for_cases(
    records: Iterable[EvidenceRecord | Mapping[str, Any]],
    cases: Iterable[Case],
    *,
    rulepack_version: str | None = None,
) -> BatchVerification:
    """跨 Case 批量复核：按 `(case_id, run_id)` 自行匹配原始轨迹。"""
    index: dict[tuple[str, str], tuple[Case, Run]] = {}
    for case in cases:
        for run in case.runs:
            index[(case.case_id, run.run_id)] = (case, run)
    results: list[VerificationResult] = []
    for record in records:
        data, _ = _as_data(record)
        key = (str(data.get("case_id") or ""), str(data.get("run_id") or ""))
        pair = index.get(key)
        if pair is None:
            results.append(_unverified(data, "找不到记录对应的 case/run，无法复核"))
            continue
        results.append(verify_record(data, pair[0], pair[1], rulepack_version=rulepack_version))
    return _batch(results)
