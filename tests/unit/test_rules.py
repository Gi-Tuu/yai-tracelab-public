"""规则引擎单元测试：每条规则都要能脱离官方数据被独立验证。"""

from __future__ import annotations

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.rules import RULE_VERSION, RULES, evaluate_rules
from tracelab.state import reconstruct


def run_rules(events, *, case_kwargs=None, run_kwargs=None, prefix="C1"):
    case_kwargs = case_kwargs or {}
    run_kwargs = run_kwargs or {}
    run = make_run(f"{prefix}-A", events, **run_kwargs)
    case = make_case(**{"context": context(), "reference_result": reference(), **case_kwargs}, runs=[run])
    state = reconstruct(case, run)
    return state, evaluate_rules(state, case)


def labels(violations):
    return [v.label for v in violations]


# ---------------------------------------------------------------- 规则契约

def test_rule_metadata_is_complete():
    assert len(RULES) >= 14
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids)), "rule_id 必须唯一"
    for spec in RULES:
        assert spec.label and spec.dimension in {"BUSINESS_RESULT", "PROCESS", "TOOL_CALL", "RISK_CONTROL"}
        assert spec.precondition and spec.assertion, f"{spec.rule_id} 缺少可追溯描述"
        assert spec.check is not None
        assert 0 <= spec.points <= 100


def test_clean_run_has_no_violation():
    _, v = run_rules(basic_flow())
    assert v == []


# ---------------------------------------------------------------- 金额规则

def test_wrong_amount_basis_is_detected_at_collect():
    events = basic_flow(amount_basis="tax_inclusive")
    _, v = run_rules(events)
    assert labels(v) == ["AMOUNT_BASIS"]
    assert v[0].event_id == "C1-05"


def test_basis_takes_precedence_over_calculation():
    """口径错了不应再叠报一个计算错误（后果不重复计）。"""
    events = basic_flow(
        amount_basis="tax_inclusive",
        computed={"exclusive_amount": 943.4, "tax_amount": 56.6, "inclusive_amount": 1000.0},
    )
    _, v = run_rules(events)
    assert labels(v) == ["AMOUNT_BASIS"]


def test_calculation_error_detected_when_basis_correct():
    events = basic_flow(computed={"exclusive_amount": 1000.0, "tax_amount": 6.0, "inclusive_amount": 1006.0})
    _, v = run_rules(events)
    assert labels(v) == ["AMOUNT_CALCULATION"]


def test_context_update_beats_calculation_error():
    rounds = [{"round": 1, "exclusive_amount": 2000.0}, {"round": 2, "exclusive_amount": 2500.0, "supersedes_round": 1}]
    events = basic_flow(amount_value=2000.0, computed={"exclusive_amount": 2000.0, "tax_amount": 120.0, "inclusive_amount": 2120.0})
    _, v = run_rules(events, case_kwargs={
        "context": context(conversation_rounds=rounds),
        "reference_result": reference(amount_value=2500.0, expected_amounts={
            "exclusive_amount": 2500.0, "tax_amount": 150.0, "inclusive_amount": 2650.0}),
    })
    assert labels(v) == ["CONTEXT_UPDATE"]
    assert v[0].event_id == "C1-05"


# ------------------------------------------------------------ 字段完整性

def test_fabricated_buyer_is_field_fabrication():
    events = basic_flow(buyer_id="CUST-GUESS")
    _, v = run_rules(events, case_kwargs={
        "context": context(buyer_candidates=[], required_missing_fields=["buyer_id"]),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER"),
    })
    assert labels(v) == ["FIELD_FABRICATION"]


def test_asking_for_buyer_is_correct():
    events = [
        e for e in basic_flow(buyer_id=None) if e.step_id not in ("VALIDATE_DATA", "CONFIRM_INVOICE", "SUBMIT_INVOICE")
    ]
    _, v = run_rules(events, case_kwargs={
        "context": context(buyer_candidates=[], required_missing_fields=["buyer_id"]),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER"),
    }, run_kwargs={"final_state": "WAITING_USER", "invoice_created": False})
    assert v == []


def test_ambiguous_buyer_selection_is_entity_resolution():
    candidates = [{"buyer_id": "C1"}, {"buyer_id": "C2"}]
    _, v = run_rules(basic_flow(buyer_id="C1"), case_kwargs={
        "context": context(buyer_candidates=candidates, buyer_disambiguation_required=True),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER_DISAMBIGUATION"),
    })
    assert labels(v) == ["ENTITY_RESOLUTION"]


def test_unspecified_amount_basis_guessed_is_fabrication():
    _, v = run_rules(basic_flow(), case_kwargs={
        "context": context(amount_basis_unspecified=True),
        "reference_result": reference(amount_basis=None, expected_amounts=None,
                                      acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_AMOUNT_BASIS"),
        }, run_kwargs={"final_state": "WAITING_USER", "invoice_created": False})
    assert labels(v) == ["FIELD_FABRICATION"]


def test_red_invoice_without_original_is_fabrication():
    events = basic_flow(task_type="RED")
    _, v = run_rules(events, case_kwargs={
        "reference_result": reference(invoice_task_type="RED", amount_value=None,
                                      expected_amounts=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False,
                                      required_action="ASK_ORIGINAL_INVOICE_AND_AMOUNT"),
    })
    assert labels(v) == ["FIELD_FABRICATION"]
    assert v[0].rule_id == "R014"


# ------------------------------------------------------------ 商品与批量

def test_wrong_item_is_product_match():
    _, v = run_rules(basic_flow(item_id="ITEM-016"))
    assert labels(v) == ["PRODUCT_MATCH"]


def test_batch_line_omission_detected():
    events = basic_flow(task_type="BATCH")
    expected = [{"item_id": "ITEM-006", "exclusive_amount": 1000.0},
                {"item_id": "ITEM-007", "exclusive_amount": 2000.0}]
    _, v = run_rules(events, case_kwargs={
        "reference_result": reference(invoice_task_type="BATCH", expected_line_count=2, expected_line_items=expected),
    })
    assert labels(v) == ["MULTI_ITEM_OMISSION"]


# ------------------------------------------------------------ 意图与门禁

def test_task_type_misclassified():
    _, v = run_rules(basic_flow(task_type="BLUE"), case_kwargs={
        "reference_result": reference(invoice_task_type="RED"),
    })
    assert labels(v) == ["INTENT_CLASSIFICATION"]
    assert v[0].event_id == "C1-04"


def test_cancel_request_not_recognised():
    events = basic_flow(intent="ISSUE", final_state="FAILED", invoice_created=False)
    _, v = run_rules(events, case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="CANCELED", invoice_should_exist=False,
                                      required_action="CANCEL"),
    })
    assert labels(v) == ["INTENT_CLASSIFICATION"]


def test_intent_error_suppresses_downstream_consequences():
    """误判"取消"后仍去提交，构成的是误判的后果，不另立 CONFIRMATION_BYPASS。"""
    events = basic_flow(intent="ISSUE", final_state="FAILED", invoice_created=False,
                        submit_status="rejected", submit_error="CONFIRMATION_REQUIRED")
    _, v = run_rules(events, case_kwargs={
        "context": context(user_confirmation_recorded=False),
        "reference_result": reference(acceptable_snapshot_state="CANCELED", invoice_should_exist=False,
                                      required_action="CANCEL"),
    })
    assert labels(v) == ["INTENT_CLASSIFICATION"]


def test_duplicate_risk_must_block():
    _, v = run_rules(basic_flow(), case_kwargs={
        "context": context(duplicate_order_id="ORD-1", risk_flags=["DUPLICATE_INVOICE"],
                           user_confirmation_recorded=False),
        "reference_result": reference(acceptable_snapshot_state="FAILED", invoice_should_exist=False,
                                      required_action="BLOCK_DUPLICATE"),
    })
    assert "DUPLICATE_CONTROL" in labels(v)
    assert v[0].event_id == "C1-06"


def test_confirmation_bypass_detected_at_submit():
    _, v = run_rules(basic_flow(), case_kwargs={"context": context(user_confirmation_recorded=False)})
    assert labels(v) == ["CONFIRMATION_BYPASS"]
    assert v[0].event_id == "C1-08"


def test_no_bypass_when_no_submit():
    events = [e for e in basic_flow() if e.step_id != "SUBMIT_INVOICE" and e.step_id != "WAIT_INVOICE_CALLBACK"]
    _, v = run_rules(events, case_kwargs={"context": context(user_confirmation_recorded=False)},
                     run_kwargs={"final_state": "WAITING_USER", "invoice_created": False})
    assert v == []


# ------------------------------------------------------------ 工具与回调

def test_retry_on_unknown_submit_is_tool_error_handling():
    events = basic_flow(submit_status="unknown", submit_status_step="TIMED_OUT", submit_error="SUBMIT_TIMEOUT_UNKNOWN_COMMIT",
                        retry_count=2, submit_attempts=3, callback_status=None,
                        final_state="FAILED", invoice_created=False)
    _, v = run_rules(events, case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN", invoice_should_exist=False,
                                      required_action="STOP_RETRY_AND_ESCALATE"),
    })
    assert labels(v) == ["TOOL_ERROR_HANDLING"]
    assert v[0].event_id == "C1-08"


def test_single_attempt_then_transfer_is_correct():
    """未知状态只发起一次提交并转人工，属于正确处理。"""
    events = basic_flow(submit_status="unknown", submit_status_step="TIMED_OUT",
                        submit_error="SUBMIT_TIMEOUT_UNKNOWN_COMMIT", callback_status=None,
                        final_state="FAILED", invoice_created=False)
    events = [e for e in events if e.step_id != "FAIL_TASK"]
    events.append(make_event("C1-10", 10, "TRANSFER_TO_HUMAN", result_code="E600"))
    _, v = run_rules(events, case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN", invoice_should_exist=False,
                                      required_action="STOP_RETRY_AND_ESCALATE"),
    }, run_kwargs={"final_state": "TRANSFERRED_TO_HUMAN", "invoice_created": False})
    assert "TOOL_ERROR_HANDLING" not in labels(v)


def test_callback_failure_claimed_as_success():
    events = basic_flow(callback_status="failed", final_state="SUCCEEDED", invoice_created=False)
    _, v = run_rules(events, case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="FAILED", invoice_should_exist=False,
                                      required_action="REPORT_CALLBACK_FAILURE"),
    })
    assert labels(v) == ["CALLBACK_MISINTERPRETATION"]
    assert v[0].event_id == "C1-09"


def test_submit_argument_drift():
    events = basic_flow(preview_amounts={"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0},
                        submit_amounts={"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1110.0})
    _, v = run_rules(events)
    assert labels(v) == ["TOOL_ARGUMENT_ERROR"]
    assert v[0].event_id == "C1-08"


# ------------------------------------------------------------------ 归因

def test_assess_run_records_guards_not_faults():
    """工具对未确认提交的保护性拒绝必须记为 guard，不能记为接口故障。"""
    events = basic_flow(submit_status="rejected", submit_status_step="FAILED",
                        submit_error="CONFIRMATION_REQUIRED", callback_status=None,
                        final_state="FAILED", invoice_created=False)
    run = make_run("C1-A", events, final_state="FAILED", invoice_created=False)
    case = make_case(context=context(user_confirmation_recorded=False),
                     reference_result=reference(acceptable_snapshot_state="WAITING_USER",
                                                invoice_should_exist=False), runs=[run])
    a = assess_run(case, run)
    assert a.agent_task_success is False
    assert [g.code for g in a.guards] == ["CONFIRMATION_REQUIRED"]
    assert "TOOL_ERROR_HANDLING" not in a.labels
    assert a.first_error is not None


def test_abstains_when_state_mismatch_but_no_rule_fires():
    """终态偏离但没有可证实根因时，必须拒判而不是编一个根因。"""
    run = make_run("C1-A", basic_flow(final_state="WAITING_USER", invoice_created=False),
                   final_state="WAITING_USER", invoice_created=False)
    case = make_case(context=context(),
                     reference_result=reference(acceptable_snapshot_state="TIMEOUT_TASK", invoice_should_exist=False),
                     runs=[run])
    a = assess_run(case, run)
    assert a.agent_task_success is False
    assert a.labels == ["INSUFFICIENT_EVIDENCE"]
    assert a.violations[0].confidence < 1.0


def test_quality_score_is_deducted_from_proven_issues():
    _, v = run_rules(basic_flow(buyer_id="CUST-GUESS"), case_kwargs={
        "context": context(buyer_candidates=[], required_missing_fields=["buyer_id"]),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER"),
    })
    assert sum(x.points for x in v) == 25.0


def test_rulepack_version_is_stable():
    assert RULE_VERSION == "rulepack-1.3"


