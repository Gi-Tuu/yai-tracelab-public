"""规则硬化回归测试：C-01/02/03/10/12/27/28/29/30/31/32/33。

每条用例都对应一个已复现的缺陷：**这些断言在修复前的实现上必须失败**。
修复方向统一是"没有证据就不许判通过"：
* 零可识别证据 / 无判断依据 → UNKNOWN（R900/R902），不是 PASS；
* 因果抑制必须有真实依赖证据，不能退化成"同事件共现"；
* 三层明细（采集 / 预览 / 提交）各自守恒，非法金额不得折叠成"相等"；
* "无法判断" ≠ "确定不等"，"字段缺失" ≠ "状态不明"。
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from decimal import Decimal

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import ToolCall, Verdict
from tracelab.rules import (
    Violation,
    evaluate_rules,
)
from tracelab.state import (
    MONEY_INVALID,
    MONEY_UNKNOWN,
    money_compare,
    parse_money,
    reconstruct,
)
from tracelab.suppression import (
    SUPPRESSION_RULES,
    SuppressionRule,
    apply_suppressions,
)

TWO_LINES = [
    {"item_id": "ITEM-006", "exclusive_amount": 1000.0},
    {"item_id": "ITEM-007", "exclusive_amount": 2000.0},
]


# ------------------------------------------------------------------ 构造工具

def _assess(events, *, case_kwargs=None, run_kwargs=None, prefix="C1"):
    run = make_run(f"{prefix}-A", events, **(run_kwargs or {}))
    case = make_case(
        **{"context": context(), "reference_result": reference(), **(case_kwargs or {})},
        runs=[run],
    )
    state = reconstruct(case, run)
    return state, case, evaluate_rules(state, case)


def _labels(violations) -> list[str]:
    return [v.label for v in violations]


def _patch_output(events, step_id, **fields):
    out = []
    for e in events:
        if e.step_id == step_id:
            e = dc_replace(e, observed_output={**dict(e.observed_output), **fields})
        out.append(e)
    return out


def _patch_input(events, step_id, **fields):
    out = []
    for e in events:
        if e.step_id == step_id:
            e = dc_replace(e, observed_input={**dict(e.observed_input), **fields})
        out.append(e)
    return out


def _viol(rule_id, label, event_id, seq, *, dimension="PROCESS", points=30.0):
    return Violation(rule_id=rule_id, rule_version="t", label=label, event_id=event_id, seq=seq,
                     evidence_ids=(), explanation="测试构造", dimension=dimension,
                     points=points, confidence=1.0)


# ------------------------------------------------ C-01 零可识别证据 → UNKNOWN

def test_c01_empty_run_is_unknown_not_pass():
    state, case, v = _assess([], run_kwargs={"final_state": "SUCCEEDED", "invoice_created": False})
    assert state.has_recognizable_evidence is False
    assert [x.rule_id for x in v] == ["R902"]
    assert v[0].label == "INSUFFICIENT_EVIDENCE"
    assert "没有任何业务阶段证据" in v[0].explanation

    run = make_run("C1-A", [], final_state="SUCCEEDED", invoice_created=False)
    a = assess_run(case, run)
    assert a.verdict is Verdict.UNKNOWN
    assert a.agent_task_success is False


def test_c01_all_step_ids_unrecognized_is_unknown_not_pass():
    events = [
        make_event("X-01", 1, "OPAQUE_STEP_ONE", observed_output={"anything": 1}),
        make_event("X-02", 2, "OPAQUE_STEP_TWO", observed_output={"anything": 2}),
    ]
    state, case, v = _assess(events, run_kwargs={"final_state": "SUCCEEDED", "invoice_created": True})
    assert state.has_recognizable_evidence is False
    assert _labels(v) == ["INSUFFICIENT_EVIDENCE"]
    run = make_run("C1-A", events, final_state="SUCCEEDED", invoice_created=True)
    assert assess_run(case, run).verdict is Verdict.UNKNOWN


def test_c01_shell_only_run_without_business_stage_is_unknown_not_pass():
    """审计 C-01 变体：只有外壳阶段（RECEIVE/CREATE/COMPLETE）、没有任何业务阶段证据。

    这种轨迹的 step_id 全部"可识别"，旧版 `has_recognizable_evidence` 会返回 True，
    于是所有业务规则无事可判 → PASS/100。零业务证据同样不能证明 Agent 做对了。
    """
    events = [
        make_event("S-01", 1, "RECEIVE_REQUEST"),
        make_event("S-02", 2, "CREATE_TASK"),
        make_event("S-03", 3, "COMPLETE_TASK", result_code="E200",
                   observed_output={"invoice_created": True}),
    ]
    state, case, v = _assess(events, run_kwargs={"final_state": "SUCCEEDED", "invoice_created": True})
    assert state.has_recognizable_evidence is True  # 外壳阶段是可识别的
    assert _labels(v) == ["INSUFFICIENT_EVIDENCE"], "没有业务阶段证据时必须拒判"
    run = make_run("C1-A", events, final_state="SUCCEEDED", invoice_created=True)
    a = assess_run(case, run)
    assert a.verdict is Verdict.UNKNOWN
    assert a.agent_task_success is False


# ------------------------------ C-02 抑制不得退化成"同事件共现"（真因果保留）

def test_c02_fabricated_buyer_plus_wrong_tax_keeps_both_root_causes():
    events = basic_flow(
        buyer_id="CUST-GUESS",
        computed={"exclusive_amount": 900.0, "tax_amount": 54.0, "inclusive_amount": 954.0},
    )
    state, case, v = _assess(events, case_kwargs={
        "context": context(buyer_candidates=[], required_missing_fields=["buyer_id"]),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER"),
    })
    assert sorted(_labels(v)) == ["AMOUNT_CALCULATION", "FIELD_FABRICATION"]
    assert sum(x.points for x in v) == 55.0

    run = make_run("C1-A", events, final_state="SUCCEEDED")
    a = assess_run(case, run)
    assert a.quality_score == 45.0


def test_c02_cooccurrence_relations_are_gone_but_real_causality_kept():
    assert not any(r.parent_rule_id == "R003" and r.child_label == "AMOUNT_CALCULATION"
                   for r in SUPPRESSION_RULES), "编造购方与税额算错是两件独立事实"
    assert not any(r.parent_rule_id == "R007" and r.child_label == "TOOL_ARGUMENT_ERROR"
                   for r in SUPPRESSION_RULES), "无法证明漂移字段就是被误判类型改写的那一项"
    # 真因果（口径 → 计算 / 旧值 → 计算）必须保留
    assert any(r.parent_rule_id == "R001" and r.child_label == "AMOUNT_CALCULATION"
               for r in SUPPRESSION_RULES)
    assert any(r.parent_rule_id == "R015" and r.child_label == "AMOUNT_CALCULATION"
               for r in SUPPRESSION_RULES)


# ------------------------------ C-03 R006 三层校验（采集 / 预览 / 提交）

def _batch_args():
    return {"reference_result": reference(invoice_task_type="BATCH", expected_line_count=2,
                                          expected_line_items=TWO_LINES)}


def test_c03_preview_layer_omission_is_reported():
    events = basic_flow(task_type="BATCH")
    events = _patch_output(events, "COLLECT_INVOICE_DATA", line_items=TWO_LINES, line_count=2)
    events = _patch_output(events, "CONFIRM_INVOICE",
                           preview={"amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0,
                                                "inclusive_amount": 1060.0},
                                    "line_items": TWO_LINES[:1], "line_count": 1})
    _, _, v = _assess(events, case_kwargs=_batch_args())
    omission = [x for x in v if x.label == "MULTI_ITEM_OMISSION"]
    assert omission, "采集层 2 条、预览层只剩 1 条必须报缺项"
    assert "确认预览" in omission[0].explanation


def test_c03_submit_layer_omission_is_reported():
    events = basic_flow(task_type="BATCH")
    events = _patch_output(events, "COLLECT_INVOICE_DATA", line_items=TWO_LINES, line_count=2)
    events = _patch_output(events, "CONFIRM_INVOICE",
                           preview={"amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0,
                                                "inclusive_amount": 1060.0},
                                    "line_items": TWO_LINES, "line_count": 2})
    events = _patch_input(events, "SUBMIT_INVOICE", line_items=TWO_LINES[:1])
    _, _, v = _assess(events, case_kwargs=_batch_args())
    omission = [x for x in v if x.label == "MULTI_ITEM_OMISSION"]
    assert omission, "提交层少一条必须报缺项"
    assert "提交参数" in omission[0].explanation


def test_c03_extra_line_item_is_reported():
    events = basic_flow(task_type="BATCH")
    extra = TWO_LINES + [{"item_id": "ITEM-009", "exclusive_amount": 500.0}]
    events = _patch_output(events, "COLLECT_INVOICE_DATA", line_items=extra, line_count=3)
    _, _, v = _assess(events, case_kwargs=_batch_args())
    assert "MULTI_ITEM_OMISSION" in _labels(v), "多出一条同样是条目守恒被破坏"


def test_c03_unparseable_amount_is_not_silently_equal():
    bad_lines = [
        {"item_id": "ITEM-006", "exclusive_amount": 1000.0},
        {"item_id": "ITEM-007", "exclusive_amount": "abc"},
    ]
    assert money_compare("abc", 2000.0) == MONEY_UNKNOWN
    events = basic_flow(task_type="BATCH")
    events = _patch_output(events, "COLLECT_INVOICE_DATA", line_items=TWO_LINES, line_count=2)
    events = _patch_output(events, "CONFIRM_INVOICE",
                           preview={"amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0,
                                                "inclusive_amount": 1060.0},
                                    "line_items": bad_lines, "line_count": 2})
    _, case, v = _assess(events, case_kwargs=_batch_args())
    # R3-03：明细金额无法解析时"差异不可判定"，不能判成 Agent 用错金额；
    # 改由 R902 统一按证据不足处理（结论 UNKNOWN，仍不是静默通过）。
    from tracelab.attribution import assess_run

    assessment = assess_run(case, case.runs[0])
    assert assessment.verdict.value == "UNKNOWN"
    assert "INSUFFICIENT_EVIDENCE" in assessment.labels


def test_c03_all_three_layers_consistent_is_clean():
    events = basic_flow(task_type="BATCH")
    events = _patch_output(events, "COLLECT_INVOICE_DATA", line_items=TWO_LINES, line_count=2)
    events = _patch_output(events, "CONFIRM_INVOICE",
                           preview={"amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0,
                                                "inclusive_amount": 1060.0},
                                    "line_items": TWO_LINES, "line_count": 2})
    events = _patch_input(events, "SUBMIT_INVOICE", line_items=TWO_LINES)
    _, _, v = _assess(events, case_kwargs=_batch_args())
    assert "MULTI_ITEM_OMISSION" not in _labels(v)


# ------------------------- C-10 没有 acceptable_snapshot_state → UNKNOWN

def test_c10_missing_acceptable_state_is_unknown_not_pass():
    run = make_run("C1-A", basic_flow())
    case = make_case(context=context(), reference_result={"invoice_task_type": "BLUE"}, runs=[run])
    a = assess_run(case, run)
    assert a.verdict is Verdict.UNKNOWN
    assert a.labels == ["INSUFFICIENT_EVIDENCE"]
    assert a.agent_task_success is False
    assert a.violations[0].rule_id == "R900"
    assert a.violations[0].confidence < 1.0


# -------------------- C-12 "无法判断" 不得当成 "确定不等" / 尾零合法

def test_c12_math_equivalent_trailing_zeros_are_valid_money():
    parsed = parse_money("1000.0000000")
    assert parsed.ok and parsed.value == Decimal("1000")
    assert parse_money("0.0000000").ok is True
    assert parse_money(Decimal("1.2345678")).status == MONEY_INVALID


def test_c12_unparseable_preview_amount_is_not_argument_drift():
    events = basic_flow(
        preview_amounts={"exclusive_amount": 1000.0, "tax_amount": "abc", "inclusive_amount": 1060.0},
        submit_amounts={"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0},
    )
    _, _, v = _assess(events)
    assert "TOOL_ARGUMENT_ERROR" not in _labels(v), "预览金额无法解析属于'无法判断'，不是参数漂移"

    drift = basic_flow(
        preview_amounts={"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0},
        submit_amounts={"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1110.0},
    )
    _, _, v_drift = _assess(drift)
    assert "TOOL_ARGUMENT_ERROR" in _labels(v_drift), "确定不等的漂移仍必须报"


def test_c12_unparseable_current_amount_is_not_context_update():
    rounds = [{"round": 1, "exclusive_amount": 800.0},
              {"round": 2, "exclusive_amount": 1000.0, "supersedes_round": 1}]
    events = basic_flow(amount_value="abc")
    _, _, v = _assess(events, case_kwargs={"context": context(conversation_rounds=rounds)})
    assert "CONTEXT_UPDATE" not in _labels(v)


# ------------------------- C-29 缺字段 ≠ 状态不明（逐事件判定）

def _drop_submit_status(events):
    out = []
    for e in events:
        if e.step_id == "SUBMIT_INVOICE":
            e = dc_replace(e, observed_output={k: v for k, v in e.observed_output.items()
                                               if k != "submit_status"})
        out.append(e)
    return out


def test_c29_missing_submit_status_field_is_not_unknown_state():
    events = _drop_submit_status(basic_flow())
    _, _, v = _assess(events, case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN",
                                      invoice_should_exist=False),
    })
    assert "TOOL_ERROR_HANDLING" not in _labels(v), "没写 submit_status 不等于'结果不明'"


def test_c29_explicit_unknown_status_still_reports():
    _, _, v = _assess(basic_flow(submit_status="unknown"), case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN",
                                      invoice_should_exist=False),
    })
    assert "TOOL_ERROR_HANDLING" in _labels(v)


def test_c29_timeout_step_status_still_reports():
    _, _, v = _assess(basic_flow(submit_status="accepted", submit_status_step="TIMED_OUT"), case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN",
                                      invoice_should_exist=False),
    })
    assert "TOOL_ERROR_HANDLING" in _labels(v)


# ------------------------- C-30 R015 只在真正沿用旧值时触发

def test_c30_context_update_only_when_superseded_value_is_used():
    rounds = [{"round": 1, "exclusive_amount": 800.0},
              {"round": 2, "exclusive_amount": 1000.0, "supersedes_round": 1}]
    other = basic_flow(amount_value=900.0,
                       computed={"exclusive_amount": 900.0, "tax_amount": 54.0,
                                 "inclusive_amount": 954.0})
    _, _, v = _assess(other, case_kwargs={"context": context(conversation_rounds=rounds)})
    assert "CONTEXT_UPDATE" not in _labels(v), "900 既不是旧值也不是最新值 → 交 R002"
    assert "AMOUNT_CALCULATION" in _labels(v)

    stale = basic_flow(amount_value=800.0,
                       computed={"exclusive_amount": 800.0, "tax_amount": 48.0,
                                 "inclusive_amount": 848.0})
    _, _, v_stale = _assess(stale, case_kwargs={"context": context(conversation_rounds=rounds)})
    assert _labels(v_stale) == ["CONTEXT_UPDATE"]


# ------------------------- C-31 未追问也未编造却出票 → FIELD_FABRICATION

def test_c31_continuing_without_asking_is_field_fabrication():
    events = basic_flow(buyer_id=None)
    _, _, v = _assess(events, case_kwargs={
        "context": context(buyer_candidates=[], required_missing_fields=["buyer_id"]),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER"),
    }, run_kwargs={"final_state": "SUCCEEDED", "invoice_created": True})
    assert _labels(v) == ["FIELD_FABRICATION"]
    assert "追问" in v[0].explanation


def test_c31_asking_user_supplement_is_correct():
    events = basic_flow(buyer_id=None)
    events = list(events) + [make_event("C1-11", 11, "WAIT_USER_SUPPLEMENT")]
    _, _, v = _assess(events, case_kwargs={
        "context": context(buyer_candidates=[], required_missing_fields=["buyer_id"]),
        "reference_result": reference(buyer_id=None, acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False, required_action="ASK_BUYER"),
    }, run_kwargs={"final_state": "WAITING_USER", "invoice_created": False})
    assert "FIELD_FABRICATION" not in _labels(v), "有'交回用户'证据就不能算编造"


# ------------------------------- C-33 R009 读工具 risk_flags / R004 读 required_action

def test_c33_duplicate_flag_from_validate_tool_output_blocks():
    _, _, v = _assess(basic_flow(risk_flags=["DUPLICATE_INVOICE"]), case_kwargs={
        "reference_result": reference(acceptable_snapshot_state="FAILED", invoice_should_exist=False),
    })
    assert "DUPLICATE_CONTROL" in _labels(v), "风险标记只出现在 VALIDATE_DATA 输出时也必须阻断"


def test_c33_disambiguation_required_action_alone_triggers_entity_resolution():
    _, _, v = _assess(basic_flow(), case_kwargs={
        "context": context(buyer_candidates=None),
        "reference_result": reference(buyer_id=None, required_action="ASK_BUYER_DISAMBIGUATION",
                                      acceptable_snapshot_state="WAITING_USER",
                                      invoice_should_exist=False),
    })
    assert _labels(v) == ["ENTITY_RESOLUTION"]


# --------------------------------- C-27 乱序 seq 按 (seq, 原始顺序) 稳定排序

def test_c27_out_of_order_seq_is_sorted_by_seq_not_file_order():
    late = make_event("C1-06", 6, "PARSE_INTENT",
                      observed_output={"intent": "ISSUE", "invoice_task_type": "RED"})
    early = make_event("C1-04", 4, "PARSE_INTENT",
                       observed_output={"intent": "ISSUE", "invoice_task_type": "BLUE"})
    rest = [e for e in basic_flow() if e.step_id != "PARSE_INTENT"]
    events = [late] + rest + [early]
    state, _, v = _assess(events)
    assert state.task_type == "BLUE", "必须取 seq 最小的 PARSE_INTENT，而不是文件里先出现的"
    assert "INTENT_CLASSIFICATION" not in _labels(v)


def test_c27_same_seq_keeps_original_order():
    first = make_event("C1-04a", 4, "PARSE_INTENT",
                       observed_output={"intent": "ISSUE", "invoice_task_type": "BLUE"})
    second = make_event("C1-04b", 4, "PARSE_INTENT",
                        observed_output={"intent": "ISSUE", "invoice_task_type": "RED"})
    rest = [e for e in basic_flow() if e.step_id != "PARSE_INTENT"]
    state, _, _ = _assess([first, second] + rest)
    assert state.first_of("PARSE_INTENT").event_id == "C1-04a"
    assert state.last_of("PARSE_INTENT").event_id == "C1-04b"


# ------------------------------- C-28 submit_attempts 只数真正的提交调用

def test_c28_submit_attempts_ignores_non_submit_tool_calls():
    extra = ToolCall(name="customer.lookup", arguments={}, result={}, attempt=None, raw={})
    events = basic_flow(submit_attempts=1)
    patched = []
    for e in events:
        if e.step_id == "SUBMIT_INVOICE":
            e = dc_replace(e, tool_calls=tuple(e.tool_calls) + (extra,))
        patched.append(e)
    state, _, _ = _assess(patched)
    assert state.submit_attempts == 1, "夹带的查询工具不是一次提交尝试"


def test_c28_submit_step_without_tool_call_counts_once():
    events = basic_flow(submit_attempts=1)
    patched = []
    for e in events:
        if e.step_id == "SUBMIT_INVOICE":
            e = dc_replace(e, tool_calls=())
        patched.append(e)
    state, _, _ = _assess(patched)
    assert state.submit_attempts == 1


def test_c28_no_submit_step_means_zero_attempts():
    events = [e for e in basic_flow()
              if e.step_id not in ("SUBMIT_INVOICE", "WAIT_INVOICE_CALLBACK")]
    state, _, _ = _assess(events, run_kwargs={"final_state": "WAITING_USER", "invoice_created": False})
    assert state.submit_attempts == 0


# ------------------------------- C-32 被抑制的父不得再作为父（无孤儿父）

def test_c32_suppressed_parent_is_not_used_as_parent(monkeypatch):
    import tracelab.suppression as suppression_mod

    def _evidence(_p, _c):
        return "测试依赖证据"

    def _no_evidence(_p, _c):
        return None

    rule_g = SuppressionRule("R015", "AMOUNT_CALCULATION", "G 抑制 P",
                             lambda p, c, s, case: _evidence(p, c))
    rule_p = SuppressionRule("R002", "CONFIRMATION_BYPASS", "P 抑制 C",
                             lambda p, c, s, case: _evidence(p, c))
    assert _no_evidence(None, None) is None
    monkeypatch.setattr(suppression_mod, "SUPPRESSION_RULES", (rule_g, rule_p))

    grand = _viol("R015", "CONTEXT_UPDATE", "E-01", 1, dimension="BUSINESS_RESULT")
    parent = _viol("R002", "AMOUNT_CALCULATION", "E-05", 5, dimension="BUSINESS_RESULT")
    child = _viol("R010", "CONFIRMATION_BYPASS", "E-08", 8, dimension="RISK_CONTROL", points=50.0)

    # 故意把 child 放在 parent 前面：先抑制的子，父随后自己被抑制 → 必须撤销子抑制
    run = make_run("X-A", basic_flow())
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    state = reconstruct(case, run)

    survivors, suppressed = apply_suppressions([grand, child, parent], state, case)
    assert survivors == [grand, child], "父被抑制后，子必须恢复为独立根因"
    assert [s.child_label for s in suppressed] == ["AMOUNT_CALCULATION"]
    assert all(s.parent_label == "CONTEXT_UPDATE" for s in suppressed)
    assert all(s.parent_label in [v.label for v in survivors] for s in suppressed), "不允许孤儿父"


def test_c32_assessment_exposes_auditable_suppressions():
    events = basic_flow(intent="ISSUE", final_state="FAILED", invoice_created=False,
                        submit_status="rejected", submit_error="CONFIRMATION_REQUIRED")
    run = make_run("C1-A", events, final_state="FAILED", invoice_created=False)
    case = make_case(context=context(user_confirmation_recorded=False),
                     reference_result=reference(acceptable_snapshot_state="CANCELED",
                                                invoice_should_exist=False,
                                                required_action="CANCEL"),
                     runs=[run])
    a = assess_run(case, run)
    assert a.suppressions, "抑制记录必须挂到 RunAssessment 上供报告审计"
    assert any(s.child_label == "CONFIRMATION_BYPASS" and s.parent_rule_id == "R008"
               for s in a.suppressions)
    assert all(s.parent_label in a.labels for s in a.suppressions), "父必须是最终保留的违规"
    assert all(s.evidence for s in a.suppressions)
