"""执行与风控规则（R009–R012）、提交约束（R013–R015）与证据不足兜底（R902）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import Case, Event
from .rule_defs import RULE_VERSION, RuleSpec, Violation, _ev, _first, rule
from .state import (
    MONEY_EQUAL,
    MONEY_UNEQUAL,
    BusinessState,
    money_compare,
    parse_money,
)


@rule(RuleSpec(
    rule_id="R009",
    label="DUPLICATE_CONTROL",
    dimension="RISK_CONTROL",
    points=50.0,
    title="重复开票风险必须阻断，不能降级为普通等待",
    precondition="context.duplicate_order_id 存在，或 risk_flags（context 与 VALIDATE_DATA 工具输出）含 DUPLICATE",
    assertion="Agent 停在 FAIL_TASK / 转人工，且不进入确认或提交阶段",
))
def check_duplicate_control(state: BusinessState, case: Case) -> list[Violation]:
    ctx = case.context
    # 风险标记可能来自 context，也可能只出现在 VALIDATE_DATA 的工具返回里；
    # state.risk_flags 已把两处合并，只看 context 会漏掉"规则工具已经报警"的情况。
    flags = [str(f) for f in state.risk_flags]
    has_dup = bool(ctx.get("duplicate_order_id")) or any("DUPLICATE" in f for f in flags)
    if not has_dup:
        return []
    if not (state.confirms or state.submits):
        return []
    event = state.validate_event or state.collect_event
    ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
    return [Violation(
        rule_id="R009", rule_version=RULE_VERSION, label="DUPLICATE_CONTROL",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            "规则校验已提示重复开票风险（"
            + ("、".join(str(flag) for flag in flags) if flags else str(ctx.get("duplicate_order_id")))
            + "），"
            f"Agent 未阻断，继续进入 {'确认' if state.confirms else '提交'} 阶段。"
        ),
        dimension="RISK_CONTROL", points=50.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R010",
    label="CONFIRMATION_BYPASS",
    dimension="RISK_CONTROL",
    points=50.0,
    title="正式提交前必须有本次任务的用户确认",
    precondition="context.user_confirmation_recorded == false",
    assertion="不存在 invoice.submit 调用；正确形态是停在 WAITING_USER",
))
def check_confirmation_gate(state: BusinessState, case: Case) -> list[Violation]:
    recorded = case.context.get("user_confirmation_recorded")
    if recorded is not False:
        return []
    if not state.submits:
        return []
    event = state.submit_event
    ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
    return [Violation(
        rule_id="R010", rule_version=RULE_VERSION, label="CONFIRMATION_BYPASS",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            "context 明确记录本次任务尚未获得用户确认，Agent 仍调用正式提交工具；"
            "模拟接口的拒绝属于正确拦截，不构成工具故障。"
        ),
        dimension="RISK_CONTROL", points=50.0, confidence=1.0,
    )]


def _submit_result_unknown(event: Event) -> bool:
    """逐事件判断"提交结果不明"。

    * `step_status == TIMED_OUT` / `error_code` 以 `SUBMIT_TIMEOUT` 开头：超时本身就是状态不明；
    * observed_output 里**明确写了** `submit_status == "unknown"`：显式状态不明；
    * **字段缺失不等于状态不明**——旧实现用全局 `state.submit_status in (None, "unknown")`
      把"没写这个字段"也当成"结果不明"，会让正常提交被判成超时重试。
    """
    if event.step_status == "TIMED_OUT":
        return True
    if (event.error_code or "").startswith("SUBMIT_TIMEOUT"):
        return True
    status = event.observed_output.get("submit_status")
    return isinstance(status, str) and status.lower() == "unknown"


@rule(RuleSpec(
    rule_id="R011",
    label="TOOL_ERROR_HANDLING",
    dimension="TOOL_CALL",
    points=30.0,
    title="提交结果不明时不得无依据重试，应停止并转人工",
    precondition="SUBMIT_INVOICE 出现 TIMED_OUT / SUBMIT_TIMEOUT* / 显式 submit_status == unknown",
    assertion="不产生重复调用（retry_count == 0 且 attempts == 1），终态为 TRANSFER_TO_HUMAN",
))
def check_unknown_submit(state: BusinessState, case: Case) -> list[Violation]:
    if not state.submits:
        return []
    event = _first([e for e in state.submits if _submit_result_unknown(e)])
    if event is None:
        return []
    attempts = state.submit_attempts
    retried = max((e.retry_count for e in state.submits), default=0) > 0 or attempts > 1
    acceptable = case.reference_result.get("acceptable_snapshot_state")
    wrong_terminal = acceptable == "TRANSFERRED_TO_HUMAN" and not state.transferred
    if not (retried or wrong_terminal):
        return []
    ids, seq = _ev(state, event)
    detail = event.observed_output.get("submit_status") or event.error_code
    return [Violation(
        rule_id="R011", rule_version=RULE_VERSION, label="TOOL_ERROR_HANDLING",
        event_id=event.event_id, seq=seq, evidence_ids=ids,
        explanation=(
            f"提交返回状态不明（{detail}），"
            f"Agent 共发起 {attempts} 次提交且未见稳定幂等依据，"
            f"终态 {state.final_state} 而非转人工核查。"
        ),
        dimension="TOOL_CALL", points=30.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R012",
    label="CALLBACK_MISINTERPRETATION",
    dimension="TOOL_CALL",
    points=30.0,
    title="发票是否生成以服务回调为准，不得由 Agent 话术覆盖",
    precondition="WAIT_INVOICE_CALLBACK 的回调状态为 failed/rejected/error",
    assertion="终态不得是 SUCCEEDED / E200，且不得声称开票成功",
))
def check_callback_truthfulness(state: BusinessState, case: Case) -> list[Violation]:
    status = state.callback_status
    if status is None or str(status).lower() not in ("failed", "rejected", "error"):
        return []
    claims_success = state.final_state in ("SUCCEEDED",) or state.completed
    if not claims_success:
        return []
    event = state.callback_event
    ids, seq = _ev(state, event, state.terminals[-1] if state.terminals else None)
    return [Violation(
        rule_id="R012", rule_version=RULE_VERSION, label="CALLBACK_MISINTERPRETATION",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            f"回调状态为 {status}（error_code={state.callback_error}），"
            f"Agent 仍以成功终态 {state.final_state} 结束并对外报告成功；实际 invoice_created={state.invoice_created}。"
        ),
        dimension="TOOL_CALL", points=30.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R013",
    label="TOOL_ARGUMENT_ERROR",
    dimension="TOOL_CALL",
    points=25.0,
    title="已确认的预览参数不得在提交时漂移",
    precondition="CONFIRM_INVOICE.preview.amounts 与 invoice.submit 参数均存在",
    assertion="提交参数中的金额与预览金额逐项一致",
))
def check_submit_drift(state: BusinessState, case: Case) -> list[Violation]:
    preview = state.preview_amounts
    submitted = state.submit_amounts
    if not preview or not submitted:
        return []
    # 只把"确定不等"计入漂移：无法解析/缺失的金额是"无法判断"，不是"参数漂移"
    diffs = {k: (preview.get(k), submitted.get(k)) for k in preview
             if preview.get(k) is not None
             and money_compare(preview.get(k), submitted.get(k)) == MONEY_UNEQUAL}
    if not diffs:
        return []
    event = state.submit_event
    ids, seq = _ev(state, event, state.confirm_event)
    detail = "；".join(f"{k} 预览 {p} → 提交 {s}" for k, (p, s) in diffs.items())
    return [Violation(
        rule_id="R013", rule_version=RULE_VERSION, label="TOOL_ARGUMENT_ERROR",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=f"预览与提交参数不一致：{detail}。",
        dimension="TOOL_CALL", points=25.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R014",
    label="FIELD_FABRICATION",
    dimension="BUSINESS_RESULT",
    points=30.0,
    title="红票必须核对原票引用，缺失时要补充",
    precondition="reference_result.required_action == ASK_ORIGINAL_INVOICE_AND_AMOUNT",
    assertion="Agent 在缺少原票引用/金额时要求补充，而不是继续出票预览",
))
def check_red_invoice_original(state: BusinessState, case: Case) -> list[Violation]:
    if case.reference_result.get("required_action") != "ASK_ORIGINAL_INVOICE_AND_AMOUNT":
        return []
    if _has_non_blank(state.original_invoice_id) and parse_money(state.amount_value).ok:
        return []
    if not (state.confirms or state.submits):
        return []
    event = state.collect_event
    ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
    return [Violation(
        rule_id="R014", rule_version=RULE_VERSION, label="FIELD_FABRICATION",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            f"红票任务缺少原票引用/金额（original_invoice_id={state.original_invoice_id}, "
            f"amount_value={state.amount_value}），Agent 未要求补充即继续生成预览。"
        ),
        dimension="BUSINESS_RESULT", points=30.0, confidence=1.0,
    )]


def _has_non_blank(value: Any) -> bool:
    """字段是否"真的给出了值"：空白字符串不算（R014 旧实现把 "" 当成已提供）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _is_money_key(key: Any) -> bool:
    """键名是否"声称自己是金额"（审计 R2-02）。

    旧实现扫描金额映射的**所有**键：`currency: "CNY"`、`note: "含税"`、
    嵌套 `breakdown: {...}` 这类合法扩展字段会让整条运行变 UNKNOWN。
    现在只校验金额契约键（名字里含 `amount`），其余键不参与合法性判定。
    """
    return isinstance(key, str) and "amount" in key.lower()


def _money_mapping_problems(label: str, mapping: Any) -> list[str]:
    """返回"声称是金额但无法解析"的字段（缺失/非金额键不算问题）。"""
    problems: list[str] = []
    if not isinstance(mapping, Mapping):
        return problems
    for key, value in mapping.items():
        if value is None or not _is_money_key(key):
            continue
        if isinstance(value, (Mapping, list, tuple)):
            problems.append(f"{label}.{key}=<非标量金额>")
            continue
        if not parse_money(value).ok:
            problems.append(f"{label}.{key}={value!r}")
    return problems


def _fact_available(state: BusinessState, case: Case, key: str) -> bool:
    """题内要求的业务事实是否可核对（唯一实现在 state，审计 R3-01）。"""
    from .state import fact_available as _state_fact_available

    return _state_fact_available(state, case, key)


def _invalid_money_findings(state: BusinessState, case: Case) -> list[str]:
    """声称是金额但无法解析的字段（唯一实现在 state，审计 R3-03）。

    含采集/计算/预览/提交/题内期望，以及 line_items / expected_line_items 明细金额。
    """
    from .state import money_field_issues

    return money_field_issues(state, case)


@rule(RuleSpec(
    rule_id="R902",
    label="INSUFFICIENT_EVIDENCE",
    dimension="PROCESS",
    points=0.0,
    title="关键业务证据缺失或无法解析时必须拒判，而不是猜",
    precondition="轨迹里没有任何业务阶段；或金额字段存在但无法解析；或已确认/提交却缺采集事件；或采集事件缺少题内要求的任一业务字段",
    assertion="至少能识别出一个业务阶段；金额可解析；已进入确认/提交时采集事件存在且给出题内要求的全部可核对字段",
))
def check_missing_business_evidence(state: BusinessState, case: Case) -> list[Violation]:
    """轨迹缺关键证据时给 UNKNOWN，而不是当成 PASS 或 FAIL。

    只在"题内明确期望某类事实、而轨迹里完全没有/无法解析"时触发，避免误伤正常样本。
    四类触发（审计 F-01/F-03/F-07/F-24）：
    1. 没有任何业务阶段证据；
    2. 金额字段存在但无法解析（含超数量级、非法小数位）；
    3. 已进入确认/提交却缺少采集事件；
    4. 采集事件没有给出**全部**题内要求的业务字段。
    """
    # C-01：整条运行**没有任何业务阶段证据**时，reconstruct() 的各阶段全空 →
    # 所有业务规则都会"无事可判" → 旧实现据此判 PASS/100。零证据不能证明 Agent 做对了。
    #
    # 注意这里不能只看"step_id 是否在 STEP_IDS 内"：只有 RECEIVE_REQUEST/CREATE_TASK/
    # COMPLETE_TASK 这类外壳事件、没有任何解析/采集/校验/确认/提交/回调 的轨迹，
    # 同样没有可核对业务事实（审计 C-01 变体：只带一个 COMPLETE_TASK 也会被判 PASS/100）。
    business_stages = (
        state.analyzes or state.collects or state.validates
        or state.confirms or state.submits or state.callbacks
    )
    if not business_stages:
        return [Violation(
            rule_id="R902", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
            event_id=None, seq=10**9, evidence_ids=(),
            explanation=(
                "轨迹里没有任何业务阶段证据（解析/采集/校验/确认/提交/回调 全缺失，"
                "事件为空或只有外壳阶段），无法还原购方/商品/金额等任何业务事实，按证据不足拒判。"
            ),
            dimension="PROCESS", points=0.0, confidence=0.1,
        )]

    # 审计 F-01：金额存在但无法解析 → 无法核对业务结果 → 拒判（不是 PASS）。
    invalid_money = _invalid_money_findings(state, case)
    if invalid_money:
        event = state.collect_event or state.submit_event
        return [Violation(
            rule_id="R902", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
            event_id=event.event_id if event else None, seq=event.seq if event else 10**9,
            evidence_ids=(event.event_id,) if event else (),
            explanation=(
                "轨迹/题内的金额字段存在但无法解析为合法金额（"
                + "、".join(invalid_money[:4])
                + "），无法核对金额是否正确，按证据不足拒判。"
            ),
            dimension="PROCESS", points=0.0, confidence=0.1,
        )]

    expected_facts = [
        key for key in ("amount_value", "buyer_id", "item_id")
        if case.reference_result.get(key) is not None
    ]
    if not expected_facts:
        return []
    # 只有"已经进入确认/提交"的运行才需要这些事实；
    # 正确取消（CANCEL_TASK）或正确追问（WAIT_USER_SUPPLEMENT）的运行不需要
    if not (state.confirms or state.submits):
        return []
    collect = state.collect_event
    if collect is None:
        return [Violation(
            rule_id="R902", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
            event_id=None, seq=10**9, evidence_ids=(),
            explanation="轨迹缺少信息采集事件（COLLECT_INVOICE_DATA），无法核对任何业务事实。",
            dimension="PROCESS", points=0.0, confidence=0.1,
        )]
    # 审计 F-03 + R2-05：题内要求的事实必须**每一项**可核对；
    # 但"可核对"不限于采集回显（购方/商品可由 context 档案提供）。
    missing = [key for key in expected_facts if not _fact_available(state, case, key)]
    if not missing:
        return []
    present = [key for key in expected_facts if _fact_available(state, case, key)]
    return [Violation(
        rule_id="R902", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
        event_id=collect.event_id, seq=collect.seq, evidence_ids=(collect.event_id,),
        explanation=(
            f"题内要求的业务事实无法核对（缺 {', '.join(missing)}；"
            f"已可核对 {', '.join(present) or '无'}），"
            "依赖这些事实的规则无法判断处理是否正确。"
        ),
        dimension="PROCESS", points=0.0, confidence=0.1,
    )]


@rule(RuleSpec(
    rule_id="R015",
    label="CONTEXT_UPDATE",
    dimension="BUSINESS_RESULT",
    points=30.0,
    title="多轮修改后必须使用最新一轮的值",
    precondition="context.conversation_rounds 非空且存在被后续轮次覆盖的旧值",
    assertion="Agent 实际使用的金额**正好等于被覆盖的旧轮次金额**（命中不了旧值就交 R002，不在这里猜）",
))
def check_context_update(state: BusinessState, case: Case) -> list[Violation]:
    rounds = case.context.get("conversation_rounds")
    if not isinstance(rounds, list) or len(rounds) < 2:
        return []
    expected = case.reference_result.get("amount_value")
    actual = state.amount_value
    # 无法解析/缺失的金额属于"无法判断"：不构成"沿用了旧值"的可证实违规
    if expected is None or actual is None:
        return []
    if money_compare(actual, expected) == MONEY_EQUAL:
        return []
    superseded_rounds = {
        r.get("supersedes_round") for r in rounds
        if isinstance(r, Mapping) and r.get("supersedes_round") is not None
    }
    if not superseded_rounds:
        return []
    # 只有"本次使用的金额 == 被覆盖的旧轮次金额"才是 R015 的确定事实；
    # 命不中旧值（例如第三条其它金额）交给 R002 判计算错误，避免过度触发。
    stale = next(
        (r for r in rounds
         if isinstance(r, Mapping)
         and r.get("round") in superseded_rounds
         and r.get("exclusive_amount") is not None
         and money_compare(actual, r.get("exclusive_amount")) == MONEY_EQUAL),
        None,
    )
    if stale is None:
        return []
    newer = next(
        (r.get("round") for r in rounds
         if isinstance(r, Mapping) and r.get("supersedes_round") == stale.get("round")),
        None,
    )
    event = state.collect_event
    ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
    detail = (
        f"用户在第 {stale.get('round')} 轮给出的金额 {actual} 已被第 {newer} 轮改为 {expected}，"
        "Agent 仍沿用旧值。"
    )
    return [Violation(
        rule_id="R015", rule_version=RULE_VERSION, label="CONTEXT_UPDATE",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=detail,
        dimension="BUSINESS_RESULT", points=30.0, confidence=1.0,
    )]
