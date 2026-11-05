"""指标口径与评测完整性：UNKNOWN 排除、覆盖率上限、A/B/C 三档降级。

全部使用**合成对象**（`tests/builders.py` 的构造器 + 直接构造的 RunAssessment），
不依赖官方资料包，因此在公开镜像里也能跑。

核心不变量：
1. UNKNOWN 既不是"判对"也不是"判错"，进 macro_f1 或 label_coverage 分子都是污染；
2. 分子分母必须同口径，label_coverage 恒 ≤ 1.0，无有效分母时返回 None（不得伪造 0）；
3. manifest.integrity 不完整（A 读取 / B 契约 / C 评测）时，聚合建议不得为 release。
"""

from __future__ import annotations

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.attribution import RunAssessment
from tracelab.contracts import Dataset, IntegrityIssue, Verdict
from tracelab.evaluation import (
    CaseResult,
    EvaluationResult,
    VersionComparison,
    build_manifest,
    evaluate_dataset,
    score_against_labels,
)
from tracelab.reports import aggregate_recommendation
from tracelab.rules import Violation

# ------------------------------------------------------------------ 构造工具


def _violation(label: str, event_id: str | None = "C1-A-01") -> Violation:
    return Violation(
        rule_id="R999", rule_version="test", label=label, event_id=event_id, seq=1,
        evidence_ids=(event_id,) if event_id else (), explanation="测试根因",
        dimension="PROCESS", points=10.0, confidence=1.0,
    )


def _assessment(
    verdict: Verdict,
    labels: tuple[str, ...] = (),
    *,
    run_id: str = "C1-A",
    case_id: str = "C1",
    score: float = 50.0,
) -> RunAssessment:
    if verdict is Verdict.UNKNOWN and not labels:
        labels = ("INSUFFICIENT_EVIDENCE",)
    return RunAssessment(
        case_id=case_id, run_id=run_id, agent_version="test-v1", verdict=verdict,
        business_invoice_created=False, quality_score=score,
        violations=[_violation(lab) for lab in labels],
    )


def _clean_case(case_id: str = "C1") -> object:
    """一对干净运行：用于 evaluate_dataset 级别的完整性测试。"""
    return make_case(
        case_id,
        context=context(),
        reference_result=reference(),
        runs=[make_run(f"{case_id}-A", basic_flow(f"{case_id}A")),
              make_run(f"{case_id}-B", basic_flow(f"{case_id}B"))],
    )


def _paired_case(case_id: str = "C1") -> object:
    return make_case(
        case_id,
        runs=[make_run(f"{case_id}-A", basic_flow(f"{case_id}A")),
              make_run(f"{case_id}-B", basic_flow(f"{case_id}B"))],
    )


def _gold(run_id: str, labels: list[str], *, task_success: bool = False) -> dict:
    return {
        "run_id": run_id,
        "agent_task_success": task_success,
        "business_invoice_created": False,
        # first_fault_event_id=None：本文件不校验首因 Exact Match，只校验标签口径
        "root_causes": [{"label": lab, "first_fault_event_id": None} for lab in labels],
    }


def _labels_entry(runs: list[dict], direction: str | None = None) -> dict:
    entry: dict = {"run_assessments": runs}
    if direction is not None:
        entry["version_direction_on_this_case"] = direction
    return entry


def _result(cr: CaseResult, manifest: dict | None = None) -> EvaluationResult:
    return EvaluationResult(dataset=Dataset(cases=[cr.case]), results=[cr], manifest=manifest or {})


def _synthetic_dataset(*, read_ok: bool = True, schema_ok: bool = True) -> Dataset:
    """构造可控 A/B 两项完整性的数据集（C 项由 extra 显式传入）。"""
    issues: list[IntegrityIssue] = []
    stats = {"lines_total": 1, "cases_parsed": 1, "runs_total": 2, "skipped": 0, "truncated": 0}
    if not read_ok:
        stats["skipped"] = 1
        issues.append(IntegrityIssue(scope="dataset", code="BAD_JSON", detail="坏行", severity="error",
                                     category="read"))
    if not schema_ok:
        issues.append(IntegrityIssue(scope="case:C1", code="SCHEMA_TYPE", detail="契约错误",
                                     severity="error", category="schema"))
    return Dataset(
        cases=[_clean_case()], issues=issues, source="synthetic.jsonl", sha256="0" * 64,
        stats=stats, adapter_name="official", adapter_version="test",
    )


def _result_with_integrity(
    *, read_ok: bool = True, schema_ok: bool = True, evaluation_complete: bool = True,
) -> EvaluationResult:
    """方向分布只有 candidate_better：完整性全通过时聚合建议本应是 release。"""
    dataset = _synthetic_dataset(read_ok=read_ok, schema_ok=schema_ok)
    case_result = CaseResult(
        case=dataset.cases[0],
        assessments=[
            _assessment(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-A"),
            _assessment(Verdict.PASS, (), run_id="C1-B"),
        ],
        comparison=VersionComparison("candidate_better", "release", "测试"),
    )
    manifest = build_manifest(
        dataset,
        extra={"evaluation_complete": evaluation_complete, "unknown_runs": 0, "degraded_runs": 0},
    )
    return EvaluationResult(dataset=dataset, results=[case_result], manifest=manifest)


# --------------------------------------------------- macro_f1 不吃 UNKNOWN


def _macro_f1_scenario() -> tuple[EvaluationResult, dict]:
    """C1-A 判错（AMOUNT_BASIS，与金标一致）；C1-B 是 UNKNOWN（标签只有 INSUFFICIENT_EVIDENCE）。

    旧实现把 C1-B 当作"漏报 AMOUNT_BASIS"计入 FN → macro_f1 ≈ 0.6667（被污染）；
    正确口径只看已评分运行 → AMOUNT_BASIS 全对 → 1.0。
    """
    case = _paired_case()
    cr = CaseResult(
        case=case,
        assessments=[
            _assessment(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-A"),
            _assessment(Verdict.UNKNOWN, ("INSUFFICIENT_EVIDENCE",), run_id="C1-B"),
        ],
        comparison=VersionComparison("insufficient_evidence", "investigate", "测试"),
    )
    labels = {"C1": _labels_entry([
        _gold("C1-A", ["AMOUNT_BASIS"]),
        _gold("C1-B", ["AMOUNT_BASIS"]),
    ])}
    return _result(cr), labels


def test_unknown_run_not_counted_in_macro_f1():
    result, labels = _macro_f1_scenario()
    metrics = score_against_labels(result, labels).values
    assert metrics["runs_scored"] == 1
    assert metrics["runs_unknown"] == 1
    # 旧口径 ≈ 0.6667：UNKNOWN 被当成"漏报"制造了假 FN
    assert metrics["root_cause_label_macro_f1"] == 1.0


# ------------------------------------ label_coverage 分子不吃 UNKNOWN（≤100%）


def _coverage_scenario() -> tuple[EvaluationResult, dict]:
    """旧实现会算出 200% 的场景（分子含 UNKNOWN，分母不含）。

    C1-A：PASS，标签集与金标都为 {}（完全命中）；
    C1-B：UNKNOWN，标签集与金标都为 {INSUFFICIENT_EVIDENCE}（完全命中，但它是 UNKNOWN）。
    旧口径 = 分子 2 / 分母 1 = 2.0；正确口径 = 1 / 1 = 1.0。
    """
    case = _paired_case()
    cr = CaseResult(
        case=case,
        assessments=[
            _assessment(Verdict.PASS, (), run_id="C1-A"),
            _assessment(Verdict.UNKNOWN, ("INSUFFICIENT_EVIDENCE",), run_id="C1-B"),
        ],
        comparison=VersionComparison("insufficient_evidence", "investigate", "测试"),
    )
    labels = {"C1": _labels_entry([
        _gold("C1-A", []),
        _gold("C1-B", ["INSUFFICIENT_EVIDENCE"]),
    ])}
    return _result(cr), labels


def test_unknown_run_not_counted_in_label_coverage_numerator():
    result, labels = _coverage_scenario()
    metrics = score_against_labels(result, labels).values
    assert metrics["runs_scored"] == 1
    assert metrics["runs_unknown"] == 1
    assert metrics["label_coverage"] == 1.0


def test_label_coverage_never_exceeds_one():
    """回归：旧实现把 UNKNOWN 计进分子（分母不含），该场景会得到 200%。"""
    result, labels = _coverage_scenario()
    metrics = score_against_labels(result, labels).values
    coverage = metrics["label_coverage"]
    assert coverage is not None
    assert coverage <= 1.0, f"label_coverage 超过 100%：{coverage}"


# ------------------------------------------- 无有效分母 → None（不得伪造 0）


def test_no_valid_denominator_returns_none_not_zero():
    """全部运行都是 UNKNOWN 时，任何准确率/覆盖率都必须返回 None，不能用 0% 冒充。"""
    case = _paired_case()
    cr = CaseResult(
        case=case,
        assessments=[
            _assessment(Verdict.UNKNOWN, ("INSUFFICIENT_EVIDENCE",), run_id="C1-A"),
            _assessment(Verdict.UNKNOWN, ("INSUFFICIENT_EVIDENCE",), run_id="C1-B"),
        ],
        comparison=VersionComparison("insufficient_evidence", "investigate", "测试"),
    )
    labels = {"C1": _labels_entry(
        [_gold("C1-A", ["AMOUNT_BASIS"]), _gold("C1-B", ["AMOUNT_BASIS"])],
        direction="equal",
    )}
    metrics = score_against_labels(_result(cr), labels).values

    assert metrics["runs_scored"] == 0
    assert metrics["runs_unknown"] == 2
    for key in (
        "agent_task_success_accuracy",
        "business_invoice_created_accuracy",
        "version_direction_accuracy",
        "first_fault_event_exact_match",
        "root_cause_label_precision",
        "root_cause_label_recall",
        "root_cause_label_macro_f1",
        "evidence_id_validity",
        "label_coverage",
    ):
        assert metrics[key] is None, f"{key} 无有效分母时必须为 None，实际 {metrics[key]!r}"


def test_no_gold_labels_returns_none():
    """完全没有标注 → 不计算任何对比指标（不拿自造基准冒充准确率）。"""
    cr = CaseResult(
        case=_paired_case(),
        assessments=[
            _assessment(Verdict.PASS, (), run_id="C1-A"),
            _assessment(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-B"),
        ],
        comparison=VersionComparison("baseline_better", "defer", "测试"),
    )
    metrics = score_against_labels(_result(cr), {}).values
    assert metrics["runs_scored"] == 0
    assert metrics["label_coverage"] is None
    assert metrics["root_cause_label_macro_f1"] is None


# ------------------------------------------- manifest.integrity 字段与独立性


def test_manifest_integrity_exposes_unknown_and_degraded_counts():
    dataset = _synthetic_dataset()
    manifest = build_manifest(dataset, extra={"unknown_runs": 2, "degraded_runs": 1})
    integrity = manifest["integrity"]
    # 顶层字段保留（其它调用方在用），integrity 里也能读到（报告层要用）
    assert manifest["unknown_runs"] == 2
    assert manifest["degraded_runs"] == 1
    assert integrity["unknown_runs"] == 2
    assert integrity["degraded_runs"] == 1
    # 有降级却没有显式声明时，不得默认"评测完整"
    assert integrity["evaluation_complete"] is False
    assert integrity["complete"] is False


def test_integrity_three_flags_are_independent():
    """A/B/C 三项分开报告：读取没读全，不影响契约与评测两项各自的判定。"""
    dataset = _synthetic_dataset(read_ok=False, schema_ok=True)
    integrity = build_manifest(dataset, extra={"evaluation_complete": True})["integrity"]
    assert integrity["read_complete"] is False
    assert integrity["schema_valid"] is True
    assert integrity["evaluation_complete"] is True
    assert integrity["complete"] is False, "任一项失败，综合判定必须为不完整"


# ------------------------------------- integrity 三档各自触发降级（不得 release）


def test_complete_integrity_allows_release():
    recommendation, _ = aggregate_recommendation(_result_with_integrity())
    assert recommendation == "release"


@pytest.mark.parametrize(
    ("flags", "letter", "expected"),
    [
        ({"read_ok": False}, "A", "investigate"),
        ({"schema_ok": False}, "B", "investigate"),
        ({"evaluation_complete": False}, "C", "gray"),
    ],
    ids=["A-read-incomplete", "B-schema-invalid", "C-evaluation-incomplete"],
)
def test_integrity_gap_blocks_release_and_names_the_item(flags, letter, expected):
    """方向分布明明是 candidate_better（本来 release），完整性缺口必须拦住它。"""
    result = _result_with_integrity(**flags)
    recommendation, reason = aggregate_recommendation(result)
    assert recommendation != "release"
    assert recommendation == expected
    assert letter in reason, f"reason 必须点名是哪一项不完整：{reason}"
    assert "完整性校验未通过" in reason


def test_incomplete_integrity_keeps_unknown_only_branch_intact():
    """只有 UNKNOWN 运行的分支行为不变：仍是 investigate，且不被完整性注记改写。"""
    result = _result_with_integrity(evaluation_complete=False)
    case_result = result.results[0]
    case_result.assessments = [
        _assessment(Verdict.UNKNOWN, (), run_id="C1-A"),
        _assessment(Verdict.UNKNOWN, (), run_id="C1-B"),
    ]
    case_result.comparison = VersionComparison("insufficient_evidence", "investigate", "测试")
    recommendation, reason = aggregate_recommendation(result)
    assert recommendation == "investigate"
    assert "UNKNOWN" in reason


# ------------------------------------- evaluate_dataset 真的计算 evaluation_complete


def test_evaluate_dataset_marks_evaluation_incomplete_on_degradation(monkeypatch):
    """R901 降级必须让 integrity.evaluation_complete=False，而不是永远 True。"""
    import tracelab.evaluation as ev

    dataset = _synthetic_dataset()
    broken_run = "C1-A"
    original = ev.assess_run

    def flaky(case, run, **kwargs):
        if run.run_id == broken_run:
            raise RuntimeError("模拟评测器内部异常")
        return original(case, run, **kwargs)

    monkeypatch.setattr(ev, "assess_run", flaky)
    result = ev.evaluate_dataset(dataset)

    integrity = result.manifest["integrity"]
    assert integrity["evaluation_complete"] is False
    assert integrity["complete"] is False
    assert integrity["degraded_runs"] == 1, "只有 R901 降级的那条运行算 degraded"
    assert integrity["unknown_runs"] >= 1
    assert result.manifest["degraded_runs"] == 1, "顶层字段必须保留，别破坏既有调用方"
    # 报告层：完整性不足时不得 release
    assert aggregate_recommendation(result)[0] != "release"


def test_evaluate_dataset_is_complete_without_failures():
    result = evaluate_dataset(_synthetic_dataset())
    integrity = result.manifest["integrity"]
    assert integrity["evaluation_complete"] is True
    assert integrity["degraded_runs"] == 0
    # F-13 契约变更：存在未判出的运行（UNKNOWN>0）时，整批不能宣称"结论完整"，
    # 即使评测流程本身没有失败。"评测完成"与"结论确定"是两件事，必须分开表达。
    if integrity["unknown_runs"]:
        assert integrity["complete"] is False
    else:
        assert integrity["complete"] is True
