"""P1-4：证据驱动的因果抑制测试。

关键不变量：
1. 上游必须更早（反向不抑制）；
2. 必须有**真实依赖证据**，不能因为标签共现就抑制；
3. 独立发生的错误必须保留；
4. 同一标签多事件时保留最早（首因稳定）。
"""

from __future__ import annotations

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.rule_engine import _dedupe
from tracelab.rules import Violation, evaluate_with_trace
from tracelab.state import reconstruct
from tracelab.suppression import SUPPRESSION_RULES, apply_suppressions


def _run_rules(events, *, case_kwargs=None, run_kwargs=None, prefix="C1"):
    run = make_run(f"{prefix}-A", events, **(run_kwargs or {}))
    case = make_case(**{"context": context(), "reference_result": reference(), **(case_kwargs or {})}, runs=[run])
    state = reconstruct(case, run)
    violations, suppressions = evaluate_with_trace(state, case)
    return state, case, violations, suppressions


def _labels(violations) -> list[str]:
    return [v.label for v in violations]


# ---------------------------------------------------------------- 依赖成立

def test_cancel_misread_suppresses_downstream_submit_violation():
    """取消被误判后仍去提交：确认绕过是该误判的执行后果。"""
    events = basic_flow(intent="ISSUE", final_state="FAILED", invoice_created=False,
                        submit_status="rejected", submit_error="CONFIRMATION_REQUIRED")
    _, _, violations, suppressions = _run_rules(events, case_kwargs={
        "context": context(user_confirmation_recorded=False),
        "reference_result": reference(acceptable_snapshot_state="CANCELED", invoice_should_exist=False,
                                      required_action="CANCEL"),
    })
    assert _labels(violations) == ["INTENT_CLASSIFICATION"]
    assert any(s.child_label == "CONFIRMATION_BYPASS" and s.parent_rule_id == "R008" for s in suppressions)
    assert all(s.evidence for s in suppressions), "抑制必须带依赖证据"


def test_amount_basis_suppresses_calculation_when_same_event():
    events = basic_flow(
        amount_basis="tax_inclusive",
        computed={"exclusive_amount": 943.4, "tax_amount": 56.6, "inclusive_amount": 1000.0},
    )
    _, _, violations, suppressions = _run_rules(events)
    assert _labels(violations) == ["AMOUNT_BASIS"]
    assert any(s.child_label == "AMOUNT_CALCULATION" for s in suppressions)


# ---------------------------------------------------------------- 独立保留

def test_independent_confirmation_bypass_is_kept():
    """意图误判与确认绕过**独立发生**时不能抑制。

    这里任务类型判错（RED vs BLUE），但提交参数里没有携带错误类型，
    说明确认绕过与意图误判没有参数依赖 → 两条都要保留。
    """
    events = basic_flow(task_type="BLUE")
    _, _, violations, suppressions = _run_rules(events, case_kwargs={
        "context": context(user_confirmation_recorded=False),
        "reference_result": reference(invoice_task_type="RED"),
    })
    assert "INTENT_CLASSIFICATION" in _labels(violations)
    assert "CONFIRMATION_BYPASS" in _labels(violations)
    assert not any(s.child_label == "CONFIRMATION_BYPASS" for s in suppressions)


def test_task_type_propagation_is_evidence_for_suppression():
    """反例：提交参数里确实带着被误判的类型 → 构成依赖，可以抑制。"""
    from dataclasses import replace as dc_replace

    events = basic_flow(task_type="BLUE")
    patched = []
    for e in events:
        if e.step_id == "SUBMIT_INVOICE":
            new_tools = tuple(
                dc_replace(t, arguments={**dict(t.arguments), "invoice_task_type": "BLUE"})
                for t in e.tool_calls
            )
            e = dc_replace(e, tool_calls=new_tools)
        patched.append(e)
    _, _, violations, suppressions = _run_rules(patched, case_kwargs={
        "context": context(user_confirmation_recorded=False),
        "reference_result": reference(invoice_task_type="RED"),
    })
    assert "INTENT_CLASSIFICATION" in _labels(violations)
    assert "CONFIRMATION_BYPASS" not in _labels(violations)
    assert any(s.child_label == "CONFIRMATION_BYPASS" and "invoice_task_type" in s.evidence
               for s in suppressions)


# ---------------------------------------------------------------- 顺序约束

def test_downstream_earlier_than_upstream_is_not_suppressed():
    """子错误比父错误更早时，不允许反向抑制。"""
    parent = Violation(rule_id="R008", rule_version="t", label="INTENT_CLASSIFICATION",
                       event_id="E-10", seq=10, evidence_ids=(), explanation="父", dimension="PROCESS",
                       points=40.0, confidence=1.0)
    child = Violation(rule_id="R010", rule_version="t", label="CONFIRMATION_BYPASS",
                      event_id="E-05", seq=5, evidence_ids=(), explanation="子（更早）",
                      dimension="RISK_CONTROL", points=50.0, confidence=1.0)

    run = make_run("X-A", basic_flow())
    case = make_case(context=context(), reference_result=reference(required_action="CANCEL"), runs=[run])
    state = reconstruct(case, run)
    survivors, suppressed = apply_suppressions([parent, child], state, case)
    assert len(survivors) == 2 and suppressed == []


def test_same_event_suppression_is_allowed():
    """同一事件上的口径/计算判断允许建立依赖（同一点观测）。"""
    parent = Violation(rule_id="R001", rule_version="t", label="AMOUNT_BASIS",
                       event_id="E-05", seq=5, evidence_ids=(), explanation="父", dimension="BUSINESS_RESULT",
                       points=30.0, confidence=1.0)
    child = Violation(rule_id="R002", rule_version="t", label="AMOUNT_CALCULATION",
                      event_id="E-05", seq=5, evidence_ids=(), explanation="子", dimension="BUSINESS_RESULT",
                      points=30.0, confidence=1.0)

    run = make_run("X-A", basic_flow(amount_basis="tax_inclusive"))
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    state = reconstruct(case, run)
    survivors, suppressed = apply_suppressions([parent, child], state, case)
    assert len(survivors) == 1 and len(suppressed) == 1


# ---------------------------------------------------------------- 首因稳定

def test_same_label_keeps_earliest_event():
    early = Violation(rule_id="R001", rule_version="t", label="AMOUNT_BASIS",
                      event_id="E-05", seq=5, evidence_ids=(), explanation="早", dimension="BUSINESS_RESULT",
                      points=30.0, confidence=1.0)
    late = Violation(rule_id="R001", rule_version="t", label="AMOUNT_BASIS",
                     event_id="E-09", seq=9, evidence_ids=(), explanation="晚", dimension="BUSINESS_RESULT",
                     points=30.0, confidence=1.0)
    kept = _dedupe([late, early])
    assert len(kept) == 1
    assert kept[0].event_id == "E-05"


# ---------------------------------------------------------------- 规则可审阅

def test_suppression_rules_are_declarative_and_explainable():
    assert len(SUPPRESSION_RULES) >= 8
    for rule in SUPPRESSION_RULES:
        assert rule.parent_rule_id.startswith("R")
        assert rule.child_label
        assert rule.reason, "每条抑制关系都必须写明原因"
        assert callable(rule.precondition)
    # 不允许存在"无条件的全局抑制"
    assert all(rule.precondition is not None for rule in SUPPRESSION_RULES)


# ------------------------------------------------- WS-D 反例硬化：场景事实不够
#
# 下面这些用例守住一条边界：**题面场景事实本身不足以抑制**。
# 必须再证明父错误确实把错误推进到了下游（有事件、有流程推进、有真实计算），
# 否则下游违规是独立错误，必须各自保留。

def _viol(rule_id, label, event_id, seq, *, dimension="PROCESS", points=30.0):
    """构造一条最小可复核违规，避免反例用例依赖真实规则的触发顺序。"""
    return Violation(rule_id=rule_id, rule_version="t", label=label, event_id=event_id, seq=seq,
                     evidence_ids=(), explanation="测试构造", dimension=dimension,
                     points=points, confidence=1.0)


def _state(events, *, context_kwargs=None, reference_kwargs=None, run_kwargs=None):
    """只重建状态，不跑规则：反例用例直接对 `_suppress` 喂构造好的违规。"""
    run = make_run("X-A", events, **(run_kwargs or {}))
    case = make_case(context=context(**(context_kwargs or {})),
                     reference_result=reference(**(reference_kwargs or {})), runs=[run])
    return reconstruct(case, run), case


def test_cancel_misread_without_flow_progress_is_not_suppressed():
    """反例：取消被误判，但 Agent 卡在解析阶段 → 下游违规不得算作它的后果。

    轨迹里没有提交、没有确认预览、没有完成终态、没有转人工，
    说明 Agent 根本没把"误判的取消任务"推进下去；此时 CONFIRMATION_BYPASS
    等下游违规是独立错误，必须保留。
    """

    parent = _viol("R008", "INTENT_CLASSIFICATION", "C1-04", 4)
    child = _viol("R010", "CONFIRMATION_BYPASS", "C1-08", 8, dimension="RISK_CONTROL", points=50.0)
    events = [e for e in basic_flow()
              if e.step_id not in ("CONFIRM_INVOICE", "SUBMIT_INVOICE", "WAIT_INVOICE_CALLBACK", "COMPLETE_TASK")]
    state, case = _state(events, reference_kwargs={
        "required_action": "CANCEL", "acceptable_snapshot_state": "CANCELED", "invoice_should_exist": False,
    }, run_kwargs={"final_state": "WAITING_USER", "invoice_created": False})
    assert state.parse_event is not None, "父违规可定位，但流程没有推进"
    assert not (state.submit_attempts or state.preview or state.completed or state.transferred)

    survivors, suppressed = apply_suppressions([parent, child], state, case)
    assert suppressed == [], "没有流程推进证据时不得抑制"
    assert _labels(survivors) == ["INTENT_CLASSIFICATION", "CONFIRMATION_BYPASS"]

    # 对照：同样两条违规，只要流程真的推进了（有提交），就必须抑制。
    state_ok, case_ok = _state(basic_flow(), reference_kwargs={
        "required_action": "CANCEL", "acceptable_snapshot_state": "CANCELED", "invoice_should_exist": False,
    })
    assert state_ok.submit_attempts > 0
    _, suppressed_ok = apply_suppressions([parent, child], state_ok, case_ok)
    assert len(suppressed_ok) == 1 and suppressed_ok[0].parent_rule_id == "R008"
    assert "提交" in suppressed_ok[0].evidence, "证据必须点名是哪个事实让它算'推进了流程'"


def test_basis_mismatch_without_calculation_is_not_suppressed():
    """反例：口径不符但计算没发生（amount_value is None）→ 不抑制。"""

    parent = _viol("R001", "AMOUNT_BASIS", "C1-05", 5, dimension="BUSINESS_RESULT")
    child = _viol("R002", "AMOUNT_CALCULATION", "C1-06", 6, dimension="BUSINESS_RESULT")
    # 采集(5) 早于"计算"(6)，顺序前置条件成立；唯一缺口是采集里没有算出金额。
    state, case = _state(basic_flow(amount_basis="tax_inclusive", amount_value=None))
    assert state.amount_value is None
    survivors, suppressed = apply_suppressions([parent, child], state, case)
    assert suppressed == [], "计算没有真的发生，不能说计算错误由口径导致"
    assert len(survivors) == 2

    # 对照：同一个错误口径，只要真的算出金额 → 必须抑制（跨事件 fallback）。
    state_ok, case_ok = _state(basic_flow(amount_basis="tax_inclusive"))
    assert state_ok.amount_value is not None
    _, suppressed_ok = apply_suppressions([parent, child], state_ok, case_ok)
    assert len(suppressed_ok) == 1 and suppressed_ok[0].parent_rule_id == "R001"


def test_stale_value_matching_non_superseded_round_is_not_suppressed():
    """反例：金额匹配到"没被后续轮次覆盖"的轮次金额 → 不是旧值传播，不抑制。"""

    rounds = [
        {"round": 1, "exclusive_amount": 800.0},                          # 被第 2 轮覆盖 → 旧值
        {"round": 2, "exclusive_amount": 1000.0, "supersedes_round": 1},  # 最新有效轮次
    ]
    parent = _viol("R015", "CONTEXT_UPDATE", "C1-05", 5, dimension="BUSINESS_RESULT")
    child = _viol("R002", "AMOUNT_CALCULATION", "C1-05", 5, dimension="BUSINESS_RESULT")

    # 用的是 1000.0 = 有效轮次金额：不是旧值，不得抑制。
    state, case = _state(basic_flow(amount_value=1000.0), context_kwargs={"conversation_rounds": rounds})
    assert state.amount_value == 1000.0
    survivors, suppressed = apply_suppressions([parent, child], state, case)
    assert suppressed == [], "命中未被覆盖的轮次金额，不构成旧值传播"
    assert len(survivors) == 2

    # 对照：确实用了被覆盖的旧值 800.0 → 必须抑制，且证据点名轮次。
    state_ok, case_ok = _state(basic_flow(amount_value=800.0), context_kwargs={"conversation_rounds": rounds})
    _, suppressed_ok = apply_suppressions([parent, child], state_ok, case_ok)
    assert len(suppressed_ok) == 1 and suppressed_ok[0].parent_rule_id == "R015"
    assert "旧值" in suppressed_ok[0].evidence and "800" in suppressed_ok[0].evidence


def test_child_matching_two_rules_records_only_first_by_declaration_order():
    """同一 child 命中两条规则时只记 1 条：按 SUPPRESSION_RULES 声明顺序取第一条。

    这里故意把 R015 的父违规放在 raw 最前面：如果实现按"父违规顺序"挑，
    就会记成 R015；正确实现必须按规则声明顺序记成 R001。
    """

    rounds = [
        {"round": 1, "exclusive_amount": 800.0},
        {"round": 2, "exclusive_amount": 1000.0, "supersedes_round": 1},
    ]
    stale_parent = _viol("R015", "CONTEXT_UPDATE", "C1-05", 5, dimension="BUSINESS_RESULT")
    basis_parent = _viol("R001", "AMOUNT_BASIS", "C1-05", 5, dimension="BUSINESS_RESULT")
    child = _viol("R002", "AMOUNT_CALCULATION", "C1-05", 5, dimension="BUSINESS_RESULT")
    # 口径不符 + 金额命中旧值：R001 与 R015 两条规则的前置条件同时成立。
    state, case = _state(basic_flow(amount_basis="tax_inclusive", amount_value=800.0),
                         context_kwargs={"conversation_rounds": rounds})

    survivors, suppressed = apply_suppressions([stale_parent, basis_parent, child], state, case)
    declaration = [r.parent_rule_id for r in SUPPRESSION_RULES if r.child_label == "AMOUNT_CALCULATION"]
    assert declaration[0] == "R001"
    assert declaration.index("R015") > declaration.index("R001")
    assert len(suppressed) == 1, "同一个 child 只能记一条抑制"
    assert suppressed[0].child_label == "AMOUNT_CALCULATION"
    assert suppressed[0].parent_rule_id == declaration[0], "必须取声明顺序里的第一条规则"
    assert len(survivors) == 2


def test_end_to_end_two_rules_hit_one_child_yields_single_suppression():
    """整链验证：R001 与 R015 同时成立时，AMOUNT_CALCULATION 只被抑制一次且父为 R001。"""
    events = basic_flow(
        amount_basis="tax_inclusive", amount_value=800.0,
        computed={"exclusive_amount": 800.0, "tax_amount": 48.0, "inclusive_amount": 848.0},
    )
    _, _, violations, suppressions = _run_rules(events, case_kwargs={
        "context": context(conversation_rounds=[
            {"round": 1, "exclusive_amount": 800.0},
            {"round": 2, "exclusive_amount": 1000.0, "supersedes_round": 1},
        ]),
    })
    calc = [s for s in suppressions if s.child_label == "AMOUNT_CALCULATION"]
    assert len(calc) == 1
    assert calc[0].parent_rule_id == "R001"
    assert "AMOUNT_CALCULATION" not in _labels(violations)
    assert "AMOUNT_BASIS" in _labels(violations) and "CONTEXT_UPDATE" in _labels(violations)


def test_parent_without_event_id_is_never_suppressed():
    """反例：父违规定位不到具体事件（event_id 为空）→ 无法复核因果，不抑制。"""

    parent = _viol("R008", "INTENT_CLASSIFICATION", None, 4)
    child = _viol("R010", "CONFIRMATION_BYPASS", "C1-08", 8, dimension="RISK_CONTROL", points=50.0)
    state, case = _state(basic_flow(), reference_kwargs={
        "required_action": "CANCEL", "acceptable_snapshot_state": "CANCELED", "invoice_should_exist": False,
    })
    # 流程确实推进了（有提交），唯一缺口是父事件无法定位 —— 仍然不得抑制。
    assert state.submit_attempts > 0
    survivors, suppressed = apply_suppressions([parent, child], state, case)
    assert suppressed == []
    assert len(survivors) == 2

