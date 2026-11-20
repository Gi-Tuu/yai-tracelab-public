"""第三轮审计修复的回归测试（组 2：证据口径单一来源）。

R3-01/02/03 的共同根因：判定、依据记录、独立复核各写了一份"事实/金额是否可核对"。
本文件把三端**必须一致**的契约钉住：
* 判定 PASS ⇒ 不得产出 R902 记录；
* 判定 UNKNOWN ⇒ 必有 R902 记录且独立复核 VERIFIED；
* 明细金额无法解析 ⇒ UNKNOWN（不是 FAIL）；
* 未识别步骤只在"顶替了缺失的关键阶段"时才构成拒判。
"""

from __future__ import annotations

from dataclasses import replace

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Verdict
from tracelab.evidence_record import build_records
from tracelab.evidence_verify import STATUS_VERIFIED, verify_record


def _assess(events, *, ctx=None, ref=None):
    run = make_run("R1", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case("C1", context=ctx or context(), reference_result=ref or reference(), runs=[run])
    return case, run, assess_run(case, run)


def _r902(case, run, assessment):
    return [r for r in build_records(case, run, assessment) if r.rule_id == "R902"]


def test_facts_from_context_only_keep_pass_and_produce_no_refusal_record():
    """事实只来自 context 时：判定 PASS，且不得产出"判不了"的 R902 记录。"""
    events = []
    for event in basic_flow(prefix="ctx"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            event = replace(event, observed_output={})
        events.append(event)
    ctx = context(amount_value=1000.0, buyer_id="CUST-001", item_id="ITEM-006")
    case, run, assessment = _assess(events, ctx=ctx)
    assert assessment.verdict is Verdict.PASS
    assert _r902(case, run, assessment) == [], "PASS 运行不得有 R902 拒判记录（R3-A-01）"


def test_preview_invalid_money_is_unknown_with_verified_record():
    """只有 preview 金额非法：判定 UNKNOWN，且必须有可复核的 R902 记录。"""
    events = []
    for event in basic_flow(prefix="pim"):
        if event.step_id == "CONFIRM_INVOICE":
            output = dict(event.observed_output)
            output["preview"] = {"amounts": {"exclusive_amount": "abc"}}
            event = replace(event, observed_output=output)
        events.append(event)
    case, run, assessment = _assess(events)
    assert assessment.verdict is Verdict.UNKNOWN
    records = _r902(case, run, assessment)
    assert records, "UNKNOWN 必须有 R902 记录可复核（R3-A-03）"
    assert verify_record(records[0], case, run).status == STATUS_VERIFIED


def test_pass_with_unrecognized_business_step_has_no_refusal_record():
    """关键阶段齐全时，未识别步骤只作披露；判定 PASS，不得产出 R902（R3-A-02）。"""
    events = list(basic_flow(prefix="uok")) + [make_event("X-99", 99, "AGENT_PARSE")]
    case, run, assessment = _assess(events)
    assert assessment.verdict is Verdict.PASS
    assert any("未识别步骤" in note for note in assessment.uncertainties)
    assert _r902(case, run, assessment) == []


def test_unrecognized_step_replacing_critical_stage_produces_verified_record():
    """提交步骤被改名（关键阶段缺失 + 未识别步骤）：UNKNOWN 且 R902 记录可复核。"""
    events = []
    for event in basic_flow(prefix="uren"):
        if event.step_id == "SUBMIT_INVOICE":
            event = replace(event, step_id="INVOICE_SUBMIT", module="INVOICE_SUBMIT", action="INVOICE_SUBMIT")
        events.append(event)
    case, run, assessment = _assess(events)
    assert assessment.verdict is Verdict.UNKNOWN
    records = _r902(case, run, assessment)
    assert records and verify_record(records[0], case, run).status == STATUS_VERIFIED
    names = [c["name"] for c in records[0].required_conditions or []]
    assert "business_steps_recognized" in names


def test_unparseable_line_item_amount_is_unknown_not_fail():
    """明细金额无法解析 ⇒ 不可判定，不能判成 Agent 用错金额（R3-A-03C）。"""
    good = [{"item_id": "ITEM-006", "exclusive_amount": 1000.0},
            {"item_id": "ITEM-007", "exclusive_amount": 1000.0}]
    bad = [{"item_id": "ITEM-006", "exclusive_amount": 1000.0},
           {"item_id": "ITEM-007", "exclusive_amount": "abc"}]
    events = []
    for event in basic_flow(prefix="line", task_type="BATCH"):
        output = dict(event.observed_output)
        if event.step_id == "COLLECT_INVOICE_DATA":
            output["line_items"] = good
            output["line_count"] = 2
        if event.step_id == "CONFIRM_INVOICE":
            output["preview"] = {
                "amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0},
                "line_items": bad,
                "line_count": 2,
            }
        events.append(replace(event, observed_output=output))
    case, run, assessment = _assess(
        events, ref=reference(invoice_task_type="BATCH", expected_line_items=good)
    )
    assert assessment.verdict is Verdict.UNKNOWN
    assert "MULTI_ITEM_OMISSION" not in assessment.labels
    assert _r902(case, run, assessment), "该 UNKNOWN 也必须有拒判记录"


def test_honest_refusal_record_is_not_mismatch():
    """金额全缺 + 题内金额非法：记录结论与其复算必须一致（此前会判 MISMATCH）。"""
    events = []
    for event in basic_flow(prefix="hon"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output.pop("amount_value", None)
            output.pop("computed_amounts", None)
            event = replace(event, observed_output=output)
        events.append(event)
    ref = reference(expected_amounts={"exclusive_amount": "1,00", "tax_amount": 60.0,
                                      "inclusive_amount": 1060.0})
    case, run, assessment = _assess(events, ref=ref)
    assert assessment.verdict is Verdict.UNKNOWN
    records = _r902(case, run, assessment)
    assert records
    assert verify_record(records[0], case, run).status == STATUS_VERIFIED
