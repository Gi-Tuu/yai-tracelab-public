"""审计可读性拆分后的分支覆盖测试（compare_versions / build_manifest / summarise）。

这些测试锁定拆分后每个 helper 的行为分支，避免"拆完看起来更短、语义却漂移"。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Dataset, Verdict
from tracelab.evaluation import (
    _build_integrity,
    _resolve_evaluated,
    _resolve_integrity_counts,
    build_manifest,
    evaluate_dataset,
    summarise,
)
from tracelab.summary import _avg_quality, _pairwise_outcomes, _root_cause_counts
from tracelab.version_compare import _both_failed, _both_passed, _comparison_guard

ROOT = Path(__file__).resolve().parents[2]


def _pair(ok_a: bool, ok_b: bool, case_id: str = "PAIR"):
    """构造一个 A/B 对；ok_x 控制该版本是否 PASS。"""
    runs = []
    for index, ok in enumerate((ok_a, ok_b)):
        events = basic_flow(prefix=f"p{index}")
        if not ok:
            # 把金额算错，制造一条可证实 FAIL（R002）
            events = [
                replace(event, observed_output={
                    **dict(event.observed_output),
                    "computed_amounts": {"exclusive_amount": 999.0, "tax_amount": 1.0,
                                         "inclusive_amount": 1000.0},
                }) if event.step_id == "COLLECT_INVOICE_DATA" else event
                for event in events
            ]
        runs.append(make_run(f"{case_id}-{index}", events, final_state="SUCCEEDED",
                             invoice_created=True, agent_version=f"v{index}"))
    case = make_case(
        case_id, context=context(),
        reference_result=reference(invoice_task_type="BATCH",
                                   expected_amounts={"exclusive_amount": 1000.0,
                                                     "tax_amount": 60.0,
                                                     "inclusive_amount": 1060.0}),
        runs=tuple(runs),
    )
    return case, assess_run(case, runs[0]), assess_run(case, runs[1])


def test_comparison_guard_branches():
    case, base, cand = _pair(True, True)
    assert _comparison_guard(case, base, cand) is None

    unpaired = replace(case, runs=(case.runs[0],))
    guard = _comparison_guard(unpaired, base, cand)
    assert guard is not None and guard.direction == "insufficient_evidence"

    same_version = replace(case, runs=(
        replace(case.runs[0], agent_version="same"),
        replace(case.runs[1], agent_version="same"),
    ))
    guard = _comparison_guard(same_version, base, cand)
    assert guard is not None and "agent_version" in guard.reason

    from dataclasses import replace as _replace
    unknown_base = _replace(base, verdict=Verdict.UNKNOWN)
    guard = _comparison_guard(case, unknown_base, cand)
    assert guard is not None and guard.quality_delta == 0.0


def test_both_passed_and_failed_branches():
    ids = {"base_run_id": "a", "cand_run_id": "b"}
    equal = _both_passed(0.0, ids)
    assert equal.direction == "equal" and equal.release_recommendation == "release"
    better = _both_passed(5.0, ids)
    assert better.direction == "candidate_better" and better.release_recommendation == "release"
    worse = _both_passed(-5.0, ids)
    assert worse.direction == "baseline_better" and worse.release_recommendation == "defer"

    class A:
        def __init__(self, labels):
            self.labels = labels

    same = _both_failed(A(["X"]), A(["X"]), 0.0, ids)
    assert same.direction == "equal" and same.release_recommendation == "investigate"
    assert "两版同错" in same.reason

    same_delta = _both_failed(A(["X"]), A(["Y"]), 0.0, ids)
    assert same_delta.direction == "equal" and "扣分相同" in same_delta.reason

    moved = _both_failed(A(["X"]), A(["Y"]), 5.0, ids)
    assert moved.direction == "candidate_better" and moved.release_recommendation == "investigate"


def test_manifest_helpers_preserve_tristate_and_counts():
    case, _, _ = _pair(True, True)
    dataset = Dataset(cases=[case], issues=[], source="t", sha256="",
                      stats={"lines_total": 1, "cases_parsed": 1, "skipped": 0, "truncated": 0})

    assert _resolve_evaluated({}, None) is False
    assert _resolve_evaluated({}, True) is True
    assert _resolve_evaluated({"degraded_runs": 1}, None) is True
    assert _resolve_evaluated({"evaluated": False, "unknown_runs": 1}, None) is False

    unknown, degraded, empty = _resolve_integrity_counts(dataset, {}, False)
    assert unknown is None and degraded is None and empty == 0
    unknown, degraded, empty = _resolve_integrity_counts(dataset, {"unknown_runs": 2}, True)
    assert unknown == 2 and degraded == 0

    integrity = _build_integrity(dataset, False, None, None, 0, None)
    assert integrity["complete"] is None and integrity["evaluated"] is False
    integrity = _build_integrity(dataset, True, 0, 0, 0, "assert")
    assert integrity["complete"] is True

    # 显式 evaluated=False 时即便带计数也必须 null
    manifest = build_manifest(dataset, extra={"unknown_runs": 3}, evaluated=False)
    assert manifest["integrity"]["unknown_runs"] is None


def test_summarise_helpers_keep_denominators():
    case, _, _ = _pair(True, False)
    result = evaluate_dataset(Dataset(cases=[case], issues=[], source="t", sha256="",
                                      stats={"lines_total": 1, "cases_parsed": 1,
                                             "skipped": 0, "truncated": 0}))
    summary = summarise(result)
    outcomes = _pairwise_outcomes(result)
    assert summary["equal_both_fail"] == outcomes["equal_both_fail"]
    assert summary["unpaired_cases"] == outcomes["unpaired_cases"]
    runs = [a for cr in result.results for a in cr.assessments]
    assert _root_cause_counts(runs)
    avg, scored = _avg_quality([a for a in runs if not a.is_unknown])
    assert avg is not None and scored == 2
    # UNKNOWN 不进平均质量分分母
    unknown_runs = [replace(a, verdict=Verdict.UNKNOWN) for a in runs]
    avg_unknown, scored_unknown = _avg_quality([a for a in unknown_runs if not a.is_unknown])
    assert avg_unknown is None and scored_unknown == 0
