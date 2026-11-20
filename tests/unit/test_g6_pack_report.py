"""规则包的统一版本对比与报告（对应赛题加分项 #2/#3）。

证明两件事：
1. 同一套"比较 + 聚合 + 报告"层可以同时服务开票包与报销包（不改核心模块）；
2. 三态语义（UNKNOWN）与"两版同错"在跨业务场景下同样成立。
"""

from __future__ import annotations

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.contracts import Case, Verdict
from tracelab.pack_report import (
    compare_case_pair,
    compare_pack_assessments,
    render_pack_report,
    summarise_pack,
)
from tracelab.rulepacks import (
    EXPENSE_PACK,
    EXPENSE_RULEPACK_VERSION,
    INVOICE_PACK,
    evaluate_with_pack,
)

# ------------------------------------------------------------------ 费用样本


def _expense_events(
    *,
    claim: float = 1000.0,
    lines: list[dict[str, object]] | None = None,
    budget: float = 5000.0,
    approvers: tuple[str, ...] = ("MANAGER", "FINANCE"),
    already_settled: bool = False,
    settle: bool = True,
) -> list:
    items = lines if lines is not None else [{"amount": 600.0}, {"amount": 400.0}]
    events = [
        make_event("E-01", 1, "RECEIVE_CLAIM", observed_output={
            "claim_amount": claim, "line_items": items, "already_settled": already_settled,
        }),
        make_event("E-02", 2, "CHECK_BUDGET", observed_output={"budget_remaining": budget}),
    ]
    seq = 3
    for approver in approvers:
        events.append(make_event(f"E-A{seq}", seq, f"APPROVE_{approver}"))
        seq += 1
    if settle:
        events.append(make_event("E-09", seq, "SETTLE_EXPENSE", observed_output={"settled": True}))
    return events


def _expense_case(case_id: str, **base_kwargs) -> Case:
    run = make_run(f"{case_id}-A", _expense_events(**base_kwargs),
                   final_state="COMPLETED", invoice_created=False)
    return make_case(case_id, context={}, reference_result={}, runs=[run])


def _pair(case_id: str, *, base: dict, cand: dict) -> tuple[Case, object, object]:
    base_run = make_run(f"{case_id}-BASE", _expense_events(**base),
                        final_state="COMPLETED", invoice_created=False, agent_version="v1")
    cand_run = make_run(f"{case_id}-CAND", _expense_events(**cand),
                        final_state="COMPLETED", invoice_created=False, agent_version="v2")
    case = make_case(case_id, context={}, reference_result={}, runs=[base_run, cand_run])
    return (case,
            evaluate_with_pack(EXPENSE_PACK, case, base_run),
            evaluate_with_pack(EXPENSE_PACK, case, cand_run))


# ------------------------------------------------------------------ 单 Case 比较


def test_budget_fix_is_candidate_better():
    case, base, cand = _pair(
        "EXP-FIX",
        base={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 1000.0},
        cand={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 20000.0},
    )
    comparison = compare_pack_assessments(base, cand)
    assert base.verdict is Verdict.FAIL and cand.verdict is Verdict.PASS
    assert comparison.direction == "candidate_better"
    assert comparison.recommendation == "release"
    assert "EXPENSE_BUDGET_EXCEEDED" in comparison.base_labels


def test_reversed_order_flips_direction():
    case, base, cand = _pair(
        "EXP-FIX2",
        base={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 20000.0},
        cand={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 1000.0},
    )
    comparison = compare_pack_assessments(base, cand)
    assert comparison.direction == "baseline_better"
    assert comparison.recommendation == "defer"


def test_unknown_side_never_produces_version_conclusion():
    case, base, cand = _pair(
        "EXP-UNK",
        base={},
        cand={},
    )
    # 去掉预算事件，让两侧都缺关键事实 → UNKNOWN
    base_run = make_run("EXP-UNK-B", [e for e in _expense_events() if e.step_id != "CHECK_BUDGET"],
                        final_state="COMPLETED", invoice_created=False)
    cand_run = make_run("EXP-UNK-C", list(base_run.events),
                        final_state="COMPLETED", invoice_created=False)
    case = make_case("EXP-UNK", context={}, reference_result={}, runs=[base_run, cand_run])
    b = evaluate_with_pack(EXPENSE_PACK, case, base_run)
    c = evaluate_with_pack(EXPENSE_PACK, case, cand_run)
    assert b.verdict is Verdict.UNKNOWN and c.verdict is Verdict.UNKNOWN
    comparison = compare_pack_assessments(b, c)
    assert comparison.direction == "insufficient_evidence"
    assert comparison.recommendation == "investigate"


def test_both_fail_same_rules_is_equal_but_flagged():
    case, base, cand = _pair(
        "EXP-SAME",
        base={"claim": 1000.0, "lines": [{"amount": 600.0}, {"amount": 300.0}]},
        cand={"claim": 1000.0, "lines": [{"amount": 600.0}, {"amount": 300.0}]},
    )
    comparison = compare_pack_assessments(base, cand)
    assert comparison.direction == "equal"
    assert comparison.both_failed is True
    assert comparison.recommendation == "investigate"


# ------------------------------------------------------------------ 聚合与报告


def test_aggregate_all_both_fail_is_not_release():
    summary = summarise_pack([
        compare_pack_assessments(*_pair(
            f"EXP-ALL{i}",
            base={"claim": 1000.0, "lines": [{"amount": 600.0}, {"amount": 300.0}]},
            cand={"claim": 1000.0, "lines": [{"amount": 600.0}, {"amount": 300.0}]},
        )[1:])
        for i in range(2)
    ])
    recommendation, reason = summary.recommendation
    assert recommendation in ("defer", "investigate")
    assert "同错" in reason or "无差异" in reason


def test_aggregate_mixed_improvement_and_same_failure_is_defer():
    fixed = compare_pack_assessments(*_pair(
        "EXP-MIX-FIX",
        base={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 1000.0},
        cand={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 20000.0},
    )[1:])
    same = compare_pack_assessments(*_pair(
        "EXP-MIX-SAME",
        base={"claim": 1000.0, "lines": [{"amount": 600.0}, {"amount": 300.0}]},
        cand={"claim": 1000.0, "lines": [{"amount": 600.0}, {"amount": 300.0}]},
    )[1:])
    recommendation, _ = summarise_pack([fixed, same]).recommendation
    assert recommendation == "defer"


def test_clean_improvement_is_release():
    fixed = compare_pack_assessments(*_pair(
        "EXP-RELEASE",
        base={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 1000.0},
        cand={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 20000.0},
    )[1:])
    recommendation, reason = summarise_pack([fixed]).recommendation
    assert recommendation == "release"
    assert "1/1" in reason


def test_report_contains_numerators_denominators_and_recommendation():
    fixed = compare_pack_assessments(*_pair(
        "EXP-REPORT",
        base={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 1000.0},
        cand={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 20000.0},
    )[1:])
    report = render_pack_report(
        "expense", summarise_pack([fixed]),
        scenario=EXPENSE_PACK.scenario, pack_version=EXPENSE_RULEPACK_VERSION,
    )
    assert "规则包版本评测报告" in report
    assert "Case 数：1" in report
    assert "`release`" in report
    assert "自造场景" in report, "必须声明样本性质，不能暗示真实泛化能力"
    assert "| `EXP-REPORT` |" in report


# ------------------------------------------------------------------ 与开票链路一致


def _invoice_case(case_id: str, *, basis: str) -> tuple[Case, object]:
    run = make_run(f"{case_id}-A", basic_flow(prefix=case_id, amount_basis=basis),
                   final_state="SUCCEEDED", invoice_created=True, agent_version="v1")
    case = make_case(case_id, context=context(), reference_result=reference(), runs=[run])
    return case, run


def test_invoice_pack_comparison_matches_existing_semantics():
    """开票包走同一比较层时，方向必须与既有 `compare_versions` 一致（统一口径）。"""
    from dataclasses import replace

    from tracelab.attribution import assess_run
    from tracelab.evaluation import compare_versions

    base_case, base_run = _invoice_case("INV-CMP", basis="tax_inclusive")
    cand_case, cand_run = _invoice_case("INV-CMP", basis="tax_exclusive")
    cand_run = replace(cand_run, agent_version="v2")   # 两版版本号必须不同，否则评测器拒绝比较
    paired = make_case("INV-CMP", context=context(), reference_result=reference(),
                       runs=[base_run, cand_run])

    base = evaluate_with_pack(INVOICE_PACK, paired, base_run)
    cand = evaluate_with_pack(INVOICE_PACK, paired, cand_run)
    unified = compare_pack_assessments(base, cand)
    existing = compare_versions(paired, assess_run(paired, base_run), assess_run(paired, cand_run))
    assert unified.direction == existing.direction == "candidate_better"


def test_compare_case_pair_helper_uses_pack_engine():
    case, base, cand = _pair(
        "EXP-HELPER",
        base={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 1000.0},
        cand={"claim": 9000.0, "lines": [{"amount": 9000.0}], "budget": 20000.0},
    )
    comparison = compare_case_pair("expense", case, case.runs[0], case.runs[1])
    assert comparison.direction == "candidate_better"
