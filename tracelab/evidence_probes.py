"""依据记录的规则探针与报销记录构建（从 evidence_record 拆出）。

本模块只负责“从 Case/Run 取事实并生成 _Probe”；数据模型、规范化、IO 和公开构建器
仍留在 `evidence_record.py`，避免循环依赖。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .attribution import GUARD_ERROR_CODES
from .contracts import Case, Event, Run
from .evaluation import EVALUATOR_VERSION
from .evidence_record import (
    BUSINESS_STAGES,
    CONCLUSION_SATISFIED,
    CONCLUSION_VIOLATED,
    EXPENSE_SUPPORTED_RULES,
    POLICY,
    RULE_LABELS,
    TERMINAL_STEPS,
    EvidenceRecord,
    _cond,
    _dedupe,
    _event_ref,
    _value,
    apply_op,
    money_equal,
    trace_digest,
)
from .state import BusinessState, reconstruct

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
    from .state import fact_available

    state = reconstruct(case, run)
    return all(fact_available(state, case, key) for key in keys)


def _invalid_money_fields(case: Case, run: Run) -> list[str]:
    """"声称是金额但无法解析"的字段（唯一实现在 state，审计 R3-03）。"""
    from .state import money_field_issues

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
    from .state import missing_critical_stages, money_fields_present

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


