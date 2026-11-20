"""G6-C：同一个评测引擎加载两个业务规则包（开票 + 报销）。

验收点：
1. 两个包都通过同一个入口 `evaluate_with_pack` 评测，输出同一个 `PackAssessment` 结构；
2. Invoice 包是**委托**既有评测器（行为零变化）：其 verdict/labels/quality 必须与 `assess_run` 完全一致；
3. Expense 包是独立实现的 4 条规则，不依赖开票规则；
4. 事实不足 → UNKNOWN（不猜）；干净样本 → PASS。
"""

from __future__ import annotations

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Case, Verdict
from tracelab.experiments import SENSITIVITY_VARIANTS
from tracelab.rulepacks import (
    EXPENSE_PACK,
    INVOICE_PACK,
    RULEPACKS,
    evaluate_case_with,
    evaluate_with_pack,
)

# ------------------------------------------------------------------ Invoice 侧


def _invoice_case(case_id: str, *, flow_overrides=None, context_overrides=None) -> Case:
    run = make_run(
        f"{case_id}-A", basic_flow(prefix=case_id, **(flow_overrides or {})),
        final_state="SUCCEEDED", invoice_created=True,
    )
    return make_case(case_id, context=context(**(context_overrides or {})),
                     reference_result=reference(), runs=[run])


def test_invoice_pack_delegates_without_changing_behavior():
    """委托模式下结果必须与既有评测器逐字段一致（开票规则语义零变化）。"""
    cases = [
        _invoice_case("OK"),
        _invoice_case("BASIS", flow_overrides={"amount_basis": "tax_inclusive"}),
        _invoice_case("CONF", context_overrides={"user_confirmation_recorded": False}),
    ]
    for case in cases:
        run = case.runs[0]
        baseline = assess_run(case, run)
        packed = evaluate_with_pack(INVOICE_PACK, case, run)
        assert packed.verdict is baseline.verdict
        assert packed.labels == tuple(baseline.labels)
        assert packed.quality_score == baseline.quality_score
        assert packed.pack_version == INVOICE_PACK.version


def test_invoice_pack_matches_every_declared_sensitivity_variant():
    """用 G6-A 的 12 个变体做交叉验证：委托路径与直接评测必须一致。"""
    base = _invoice_case("SENS")
    for spec in SENSITIVITY_VARIANTS:
        mutated = spec.mutate(base)
        run = mutated.runs[0]
        direct = assess_run(mutated, run)
        packed = evaluate_with_pack(INVOICE_PACK, mutated, run)
        assert packed.verdict is direct.verdict, spec.name
        assert packed.labels == tuple(direct.labels), spec.name


# ------------------------------------------------------------------ Expense 侧


def _expense_events(
    *,
    claim: float = 1000.0,
    lines: list[dict[str, object]] | None = None,
    budget: float = 5000.0,
    approvers: tuple[str, ...] = ("MANAGER", "FINANCE"),
    already_settled: bool = False,
    settle: bool = True,
) -> list:
    events = [
        make_event("E-01", 1, "RECEIVE_CLAIM", observed_output={
            "claim_amount": claim,
            "line_items": lines if lines is not None else [{"amount": 600.0}, {"amount": 400.0}],
            "already_settled": already_settled,
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


def _expense_case(case_id: str, **kwargs) -> Case:
    run = make_run(f"{case_id}-A", _expense_events(**kwargs), final_state="COMPLETED", invoice_created=False)
    return make_case(case_id, context={}, reference_result={}, runs=[run])


def test_expense_clean_case_passes():
    case = _expense_case("EXP-OK")
    result = evaluate_with_pack(EXPENSE_PACK, case, case.runs[0])
    assert result.verdict is Verdict.PASS
    assert result.labels == ()
    assert result.quality_score == 100.0
    assert len(result.outcomes) == len(EXPENSE_PACK.rules)


def test_expense_amount_mismatch_triggers_exp001():
    case = _expense_case("EXP-AMT", claim=1000.0, lines=[{"amount": 600.0}, {"amount": 300.0}])
    result = evaluate_with_pack(EXPENSE_PACK, case, case.runs[0])
    assert result.verdict is Verdict.FAIL
    assert "EXPENSE_AMOUNT_MISMATCH" in result.labels
    assert "EXP001" in {o.rule_id for o in result.outcomes if o.violated is True}


def test_expense_budget_exceeded_triggers_exp002():
    case = _expense_case("EXP-BUD", claim=9000.0, budget=1000.0,
                         lines=[{"amount": 9000.0}])
    result = evaluate_with_pack(EXPENSE_PACK, case, case.runs[0])
    assert result.verdict is Verdict.FAIL
    assert "EXPENSE_BUDGET_EXCEEDED" in result.labels


def test_expense_approval_order_triggers_exp003():
    case = _expense_case("EXP-ORD", approvers=("FINANCE", "MANAGER"))
    result = evaluate_with_pack(EXPENSE_PACK, case, case.runs[0])
    assert result.verdict is Verdict.FAIL
    assert "EXPENSE_APPROVAL_ORDER" in result.labels


def test_expense_duplicate_settlement_triggers_exp004():
    case = _expense_case("EXP-DUP", already_settled=True)
    result = evaluate_with_pack(EXPENSE_PACK, case, case.runs[0])
    assert result.verdict is Verdict.FAIL
    assert "EXPENSE_DUPLICATE_SETTLEMENT" in result.labels


def test_expense_missing_fact_yields_unknown_not_guess():
    """缺关键事实（没有预算事件）→ UNKNOWN，而不是默认通过/默认违规。"""
    events = [e for e in _expense_events() if e.step_id != "CHECK_BUDGET"]
    run = make_run("EXP-MISS-A", events, final_state="COMPLETED", invoice_created=False)
    case = make_case("EXP-MISS", context={}, reference_result={}, runs=[run])
    result = evaluate_with_pack(EXPENSE_PACK, case, run)
    assert result.verdict is Verdict.UNKNOWN
    missing = [o for o in result.outcomes if o.violated is None and o.rule_id == "EXP002"]
    assert missing, "缺事实的规则必须显式标记为无法判定"


# ------------------------------------------------------------------ 统一结构 / 注册表


def test_both_packs_share_unified_output_structure():
    invoice = evaluate_case_with("invoice", _invoice_case("U-INV"), _invoice_case("U-INV").runs[0])
    expense_case = _expense_case("U-EXP")
    expense = evaluate_case_with("expense", expense_case, expense_case.runs[0])
    for payload in (invoice.to_dict(), expense.to_dict()):
        assert set(payload) == {
            "pack_id", "pack_version", "scenario", "case_id", "run_id",
            "verdict", "labels", "quality_score", "outcomes",
        }
        assert payload["verdict"] in {"PASS", "FAIL", "UNKNOWN"}
    assert invoice.pack_id != expense.pack_id
    assert invoice.scenario == "invoice" and expense.scenario == "expense-reimbursement"


def test_registry_lists_both_packs_and_rejects_unknown():
    assert set(RULEPACKS) == {"invoice", "expense"}
    assert RULEPACKS["invoice"].version == INVOICE_PACK.version
    try:
        evaluate_case_with("no-such-pack", _expense_case("X"), _expense_case("X").runs[0])
    except KeyError as exc:
        assert "no-such-pack" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知规则包必须报错")


def test_expense_pack_does_not_depend_on_invoice_rules():
    """报销包只依赖 Rulepack 引擎与 contracts；关掉开票规则也应照常工作。"""
    case = _expense_case("EXP-INDEP")
    result = evaluate_with_pack(EXPENSE_PACK, case, case.runs[0])
    invoice_labels = {"AMOUNT_BASIS", "AMOUNT_CALCULATION", "CONFIRMATION_BYPASS"}
    assert not (set(result.labels) & invoice_labels)
