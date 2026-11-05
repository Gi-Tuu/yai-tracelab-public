"""第三轮审计修复的回归测试（变形层 4 项）。

R3-B-02 摘要必须覆盖业务事实；R3-B-04 等价表示变换不得改动金额；
R3-B-06 空运行 Case 必须显式计为 skipped；R3-B-10 id_map 不得在 after 侧二次映射。
"""

from __future__ import annotations

from dataclasses import replace

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.attribution import assess_run
from tracelab.metamorphic import (
    TransformedCase,
    cases_digest,
    compare_invariant,
    money_equivalent_representation,
    rename_event_ids,
    run_invariance,
)


def _case(case_id: str = "M1", events=None, ref=None):
    events = events if events is not None else basic_flow(prefix=case_id)
    run = make_run(f"{case_id}-A", events, final_state="SUCCEEDED", invoice_created=True)
    return make_case(case_id, context=context(), reference_result=ref or reference(), runs=[run])


# ------------------------------------------------------------------ 摘要内容


def test_cases_digest_covers_business_facts():
    base = _case("D1")
    assert cases_digest([base]) == cases_digest([_case("D1")])  # 同构造 → 稳定

    ref_changed = _case("D1", ref=reference(amount_basis="tax_inclusive"))
    assert cases_digest([ref_changed]) != cases_digest([base]), "改了题内口径，摘要必须变"

    events = basic_flow(prefix="D1")
    patched = []
    for event in events:
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["amount_value"] = 999.0
            event = replace(event, observed_output=output)
        patched.append(event)
    assert cases_digest([_case("D1", events=patched)]) != cases_digest([base]), "改了观察输出，摘要必须变"

    ctx_changed = make_case("D1", context=context(amount_value=1.0), reference_result=reference(),
                            runs=base.runs)
    assert cases_digest([_case("D1")]) != cases_digest([ctx_changed]), "改了 context，摘要必须变"


# ------------------------------------------------------------------ 等价表示


def _amounts_case(exclusive: float):
    events = []
    for event in basic_flow(prefix="M2"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["computed_amounts"] = {"exclusive_amount": exclusive, "tax_amount": 60.0,
                                          "inclusive_amount": 1060.0}
            event = replace(event, observed_output=output)
        events.append(event)
    return _case("M2", events=events)


def test_money_transform_keeps_values_exact_at_two_decimals():
    case = _amounts_case(1000.0)
    before = assess_run(case, case.runs[0])
    transformed = money_equivalent_representation(case)
    after = assess_run(transformed.case, transformed.case.runs[0])
    assert compare_invariant(before, after, id_map=transformed.id_map) == {}
    # 2 位小数可精确表示 → 允许改写成字符串
    collected = transformed.case.runs[0].events[4].observed_output.get("computed_amounts")
    if collected is not None:
        assert isinstance(collected["exclusive_amount"], str)


def test_money_transform_never_changes_half_cent_values():
    """半分边界（1000.005）必须原样保留：等价变换不能改业务金额。"""
    case = _amounts_case(1000.005)
    transformed = money_equivalent_representation(case)
    for before_event, after_event in zip(case.runs[0].events, transformed.case.runs[0].events,
                                          strict=True):
        before_amounts = before_event.observed_output.get("computed_amounts") or {}
        after_amounts = after_event.observed_output.get("computed_amounts") or {}
        if "exclusive_amount" in before_amounts:
            assert after_amounts["exclusive_amount"] == before_amounts["exclusive_amount"]
    before = assess_run(case, case.runs[0])
    after = assess_run(transformed.case, transformed.case.runs[0])
    assert compare_invariant(before, after, id_map=transformed.id_map) == {}


# ------------------------------------------------------------------ 0-run 分母


def test_invariance_counts_unrunnable_cases_as_skipped():
    good = _case("V1")
    empty = make_case("V2", context=context(), reference_result=reference(), runs=[])
    report = run_invariance([good, empty])
    assert report.cases_total == 2
    assert len(report.skipped) == 1
    assert report.skipped[0]["case_id"] == "V2"
    assert report.cases_tested == 1
    # 分母只算被检验的对；同时 skipped 必须让读者看到"有 Case 没被检验"
    assert report.total == len(report.outcomes) > 0
    assert report.consistency_ratio == 1.0
    assert "runs 为空" in report.skipped[0]["reason"]


# ------------------------------------------------------------------ id_map 单向


def test_rename_transform_does_not_double_map_after_side():
    """旧编号本身形如 EV-0002/EV-0001 且位置置换时，纯改名不得报"证据不一致"。"""
    events = list(basic_flow(prefix="R1"))
    original_ids = ["EV-0002", "EV-0001"] + [f"EV-{i:04d}" for i in range(3, len(events) + 1)]
    renamed_events = [replace(event, event_id=str(new_id))
                      for event, new_id in zip(events, original_ids, strict=True)]
    case = _case("R1", events=renamed_events,
                 ref=reference(required_action="ASK_ORIGINAL_INVOICE_AND_AMOUNT"))
    transformed: TransformedCase = rename_event_ids(case)
    before = assess_run(case, case.runs[0])
    after = assess_run(transformed.case, transformed.case.runs[0])
    diffs = compare_invariant(before, after, id_map=transformed.id_map)
    assert diffs == {}, diffs
