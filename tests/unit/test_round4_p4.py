"""P4 收尾项的回归测试（2026-11-04 第 4 批）。

这四项都不改变判定结论，修的是"死代码 / 语义未命名 / 记录字段错位 / 版本号没人用"：

* `pack_report` 不可达分支 → 删除，改为显式分区不变量；
* 两套金额相等语义 → 命名 `state.money_identical` 并在文档写明分工；
* R008 值表 → 只在字段真实存在时写入，缺失时不再把记录判成 INSUFFICIENT；
* `__version__` → 由 `tracelab --version` 使用，三个版本面一眼可核对。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.builders import context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Verdict
from tracelab.evidence_record import build_records
from tracelab.evidence_verify import STATUS_VERIFIED, verify_record
from tracelab.pack_report import PackAssessment, compare_pack_assessments, summarise_pack
from tracelab.state import MONEY_EQUAL, MONEY_UNEQUAL, MONEY_UNKNOWN, money_compare, money_identical

ROOT = Path(__file__).resolve().parents[2]


def _pack(labels: tuple[str, ...], *, verdict=Verdict.FAIL) -> PackAssessment:
    return PackAssessment(
        pack_id="expense", pack_version="expense-rulepack/1", scenario="expense-reimbursement",
        case_id="C", run_id="R", verdict=verdict, labels=labels, quality_score=70.0, outcomes=(),
    )


# ---------------------------------------------------------------- pack_report 死分支


def _comparison(kind: str):
    ok = _pack((), verdict=Verdict.PASS)
    bad = _pack(("A",), verdict=Verdict.FAIL)
    unknown = _pack((), verdict=Verdict.UNKNOWN)
    if kind == "candidate_better":
        return compare_pack_assessments(bad, ok)
    if kind == "baseline_better":
        return compare_pack_assessments(ok, bad)
    if kind == "equal":
        return compare_pack_assessments(ok, _pack((), verdict=Verdict.PASS))
    if kind == "both_failed":
        return compare_pack_assessments(bad, _pack(("A",), verdict=Verdict.FAIL))
    if kind == "unknown":
        return compare_pack_assessments(ok, unknown)
    raise AssertionError(f"未知类型 {kind}")


def test_pack_report_counts_are_exact_and_partitioned():
    """R3-C-02：断言精确计数（含 UNKNOWN / 同错），而不是只看"是否在四个建议之内"。"""
    comparisons = [
        _comparison("candidate_better"),
        _comparison("baseline_better"),
        _comparison("equal"),
        _comparison("both_failed"),
        _comparison("unknown"),
    ]
    summary = summarise_pack(comparisons)
    counts = summary.counts
    assert summary.total == 5
    assert counts["candidate_better"] == 1
    assert counts["baseline_better"] == 1
    assert counts["insufficient_evidence"] == 1
    assert counts["both_failed"] == 1
    # 方向计数必须能覆盖全部 Case（equal 桶包含 both_failed）
    assert counts["candidate_better"] + counts["baseline_better"] + counts["equal"] \
        + counts["insufficient_evidence"] == summary.total
    assert summary.recommendation[0] in {"release", "defer", "gray", "investigate"}


def test_pack_report_reaches_all_four_recommendations():
    """四档建议都要能真实走到（旧形态只允许 4 档的子集，可能空集通过）。"""
    cases = {
        "release": [_comparison("candidate_better")],
        "defer": [_comparison("candidate_better"), _comparison("baseline_better")],
        "gray": [_comparison("candidate_better"), _comparison("unknown")],
        "investigate": [],
    }
    seen = {}
    for expected, comparisons in cases.items():
        recommendation, reason = summarise_pack(comparisons).recommendation
        assert recommendation == expected, (expected, recommendation, reason)
        seen[expected] = recommendation
    assert set(seen) == {"release", "defer", "gray", "investigate"}


def test_pack_report_partition_invariant_raises_on_unknown_direction():
    """分区不变量的 raise 分支必须被覆盖：非法 direction 不能被静默兜底。"""
    from dataclasses import replace

    bad = replace(_comparison("candidate_better"), direction="weird")
    summary = summarise_pack([bad])
    try:
        _ = summary.recommendation
    except AssertionError as exc:
        assert "方向计数未覆盖" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法 direction 必须触发分区不变量，而不是落进兜底文案")


# ---------------------------------------------------------------- 两套金额语义


def test_money_semantics_are_named_and_documented():
    # 合计口径：±0.005 容差
    assert money_compare("1.00", "1.005") == MONEY_EQUAL
    assert money_compare("1.00", "1.006") == MONEY_UNEQUAL
    # 明细口径：分位精确相等
    assert money_identical("1.00", "1.004") is True
    assert money_identical("1.00", "1.005") is False
    assert money_identical("1.00", "1.006") is False
    # 不可判定必须返回 None，不能被当成"不相等"
    assert money_identical("x", 1) is None
    assert money_compare("x", 1) == MONEY_UNKNOWN
    method = (ROOT / "docs" / "method.md").read_text(encoding="utf-8")
    assert "money_identical" in method or "分位精确相等" in method


# ---------------------------------------------------------------- R008 值表


def test_r008_record_verifies_when_required_action_missing():
    events = [
        make_event("C-1", 1, "PARSE_INTENT",
                   observed_output={"intent": "CANCEL", "invoice_task_type": "BLUE"}),
        make_event("C-2", 2, "COLLECT_INVOICE_DATA",
                   observed_output={"amount_value": 1000.0, "buyer_id": "CUST-001", "item_id": "ITEM-006"}),
        make_event("C-3", 3, "CANCEL_TASK", result_code="E100"),
    ]
    run = make_run("R1", events, final_state="CANCELED", invoice_created=False)
    ref = reference(required_action=None, acceptable_snapshot_state="CANCELED")
    case = make_case("C", context=context(), reference_result=ref, runs=[run])
    assessment = assess_run(case, run)
    records = [r for r in build_records(case, run, assessment) if r.rule_id == "R008"]
    assert records, "题内要求取消时必须产出 R008 记录"
    record = records[0]
    assert verify_record(record, case, run).status == STATUS_VERIFIED
    assert "reference.required_action" not in (record.observed_values or {}), \
        "缺失的字段不得写入值表（否则复核端取不到值会误判 INSUFFICIENT）"
    assert "reference.acceptable_snapshot_state" in (record.observed_values or {})


# ---------------------------------------------------------------- --version


def test_cli_version_matches_version_surfaces():
    proc = subprocess.run([sys.executable, "-m", "tracelab", "--version"],
                          cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-300:]
    from tracelab import __version__
    from tracelab.evaluation import EVALUATOR_VERSION
    from tracelab.rules import RULE_VERSION

    assert __version__ in proc.stdout
    assert EVALUATOR_VERSION in proc.stdout
    assert RULE_VERSION in proc.stdout
