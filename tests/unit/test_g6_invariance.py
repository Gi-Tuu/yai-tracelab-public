"""G6-A1：语义保持实验（invariance）。

6 个自造 Case × 6 类语义保持变换 = 36 组样本对；全部应保持判定、业务结果、规则集合、
质量分与首因（按事件 ID 映射后）一致。同时断言固定种子下结果可复现。
"""

from __future__ import annotations

import json

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.contracts import Case
from tracelab.metamorphic import (
    INVARIANCE_TRANSFORMS,
    cases_digest,
    compare_invariant,
    run_invariance,
)


def _case(case_id: str, *, context_overrides=None, reference_overrides=None, flow_overrides=None) -> Case:
    ctx = context(**(context_overrides or {}))
    ref = reference(**(reference_overrides or {}))
    flow = basic_flow(prefix=case_id, **(flow_overrides or {}))
    run = make_run(f"{case_id}-A", flow, final_state="SUCCEEDED", invoice_created=True)
    return make_case(case_id, context=ctx, reference_result=ref, runs=[run])


def _corpus() -> list[Case]:
    """6 个自造 Case：一个干净通过 + 五个不同规则违规，覆盖面广便于观察变换敏感性。"""
    return [
        _case("PASS"),
        _case("BASIS", flow_overrides={"amount_basis": "tax_inclusive"}),
        _case("CONFIRM", context_overrides={"user_confirmation_recorded": False}),
        _case("PRODUCT", flow_overrides={"item_id": "ITEM-999"}),
        _case("BATCH", reference_overrides={
            "expected_line_items": [
                {"item_id": "ITEM-006", "exclusive_amount": 1000.0},
                {"item_id": "ITEM-007", "exclusive_amount": 500.0},
            ],
        }),
        _case("INTENT", flow_overrides={"task_type": "RED"}),
    ]


def test_six_transforms_are_declared():
    assert set(INVARIANCE_TRANSFORMS) == {
        "rename_event_ids",
        "add_unrelated_metadata",
        "edit_descriptive_fields",
        "money_equivalent_representation",
        "reorder_event_array",
        "reorder_mapping_keys",
    }


def test_invariance_holds_for_all_pairs():
    """核心断言：语义保持变换不得改变任何判定要素。"""
    report = run_invariance(_corpus())
    assert report.total >= 12, "样本对数量不足"
    assert report.total == len(_corpus()) * len(INVARIANCE_TRANSFORMS)
    assert report.inconsistent == 0, json.dumps(
        [item.as_dict() for item in report.outcomes if not item.consistent], ensure_ascii=False
    )
    assert report.consistency_ratio == 1.0


def test_refusal_verdict_is_invariant_under_semantic_preserving_changes():
    """拒判（UNKNOWN）结论也必须稳定：语义保持变换不得把"证据不足"变成 PASS/FAIL。"""
    from tests.builders import make_case, make_event, make_run

    shell = [
        make_event("U-01", 1, "RECEIVE_REQUEST"),
        make_event("U-02", 2, "CREATE_TASK"),
        make_event("U-03", 3, "COMPLETE_TASK", result_code="E200"),
    ]
    run = make_run("UNKNOWN-A", shell, final_state="SUCCEEDED", invoice_created=True)
    case = make_case("UNKNOWN", context=context(), reference_result=reference(), runs=[run])

    report = run_invariance([case])
    assert report.total == len(INVARIANCE_TRANSFORMS)
    assert report.inconsistent == 0, json.dumps(
        [item.as_dict() for item in report.outcomes if not item.consistent], ensure_ascii=False
    )
    from tracelab.attribution import assess_run

    assert assess_run(case, case.runs[0]).verdict.value == "UNKNOWN"


def test_event_id_rename_maps_first_fault_and_evidence():
    """改名变换必须给出 id 映射，且映射后首因/证据仍然一致（不是"编号没变"才一致）。"""
    case = _case("BASIS", flow_overrides={"amount_basis": "tax_inclusive"})
    from tracelab.attribution import assess_run
    from tracelab.metamorphic import rename_event_ids

    before = assess_run(case, case.runs[0])
    transformed = rename_event_ids(case)
    after = assess_run(transformed.case, transformed.case.runs[0])

    assert transformed.id_map, "改名变换必须记录 id 映射"
    assert before.first_error is not None and after.first_error is not None
    assert transformed.id_map[before.first_error.event_id] == after.first_error.event_id
    assert before.first_error.event_id != after.first_error.event_id, "编号本身必须真的变了"
    assert compare_invariant(before, after, id_map=transformed.id_map) == {}


def test_money_representation_transform_really_changes_text():
    """等价金额表示变换必须真的改写表示（否则这条实验是空转）。"""
    from tracelab.metamorphic import money_equivalent_representation

    case = _case("PASS")
    transformed = money_equivalent_representation(case)
    before = case.runs[0].events[4].observed_output.get("amount_value")
    after = transformed.case.runs[0].events[4].observed_output.get("amount_value")
    assert isinstance(before, (int, float))
    assert isinstance(after, str) and after.endswith(".00")


def test_reproducible_with_fixed_seed():
    """固定种子 + 同一输入 → 两次实验完全一致。"""
    first = run_invariance(_corpus(), seed=20261104)
    second = run_invariance(_corpus(), seed=20261104)
    assert first.as_dict() == second.as_dict()
    assert cases_digest(_corpus()) == cases_digest(_corpus())


def test_change_records_are_kept():
    """每组实验都要保留"输入改了什么"的记录。"""
    report = run_invariance(_corpus())
    for item in report.outcomes:
        assert item.record.get("transform") == item.transform
        assert isinstance(item.record.get("detail"), dict)
