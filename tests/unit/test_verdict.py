"""三态判定 PASS/FAIL/UNKNOWN（公共测试：全部使用合成数据）。

核心不变量：**评测器自己没判断出来（UNKNOWN）绝不能被解释成 Agent 退化**。
"""

from __future__ import annotations

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.attribution import RunAssessment, assess_run
from tracelab.contracts import Dataset, Verdict
from tracelab.evaluation import (
    CaseResult,
    EvaluationResult,
    VersionComparison,
    compare_versions,
    summarise,
)
from tracelab.reports import aggregate_recommendation

# ------------------------------------------------------------------ 构造工具

def _assessment(verdict: Verdict, score: float = 50.0, run_id: str = "X-A") -> RunAssessment:
    from tracelab.rules import Violation

    violations = []
    if verdict is Verdict.FAIL:
        violations = [Violation(rule_id="R001", rule_version="t", label="AMOUNT_BASIS",
                                event_id="X-1", seq=1, evidence_ids=("X-1",),
                                explanation="测试", dimension="BUSINESS_RESULT",
                                points=30.0, confidence=1.0)]
    elif verdict is Verdict.UNKNOWN:
        violations = [Violation(rule_id="R900", rule_version="t", label="INSUFFICIENT_EVIDENCE",
                                event_id=None, seq=10**9, evidence_ids=(),
                                explanation="证据不足", dimension="PROCESS",
                                points=20.0, confidence=0.2)]
    return RunAssessment(case_id="X", run_id=run_id, agent_version="v", verdict=verdict,
                         business_invoice_created=False, quality_score=score, violations=violations)


def _paired_case(run_id: str = "X"):
    # 两版必须给不同 agent_version：否则评测器无法证明"第 0 条是基线"（C-09 守卫）
    return make_case(runs=[make_run(f"{run_id}-A", basic_flow(f"{run_id}A"), agent_version="baseline-v1.0"),
                           make_run(f"{run_id}-B", basic_flow(f"{run_id}B"), agent_version="candidate-v2.0")])


def _result(pairs: list[tuple[Verdict, Verdict, str]]) -> EvaluationResult:
    """pairs: (基线态, 候选态, 方向)"""
    results = []
    for i, (base_v, cand_v, direction) in enumerate(pairs):
        case = _paired_case(f"V{i}")
        results.append(CaseResult(
            case=case,
            assessments=[_assessment(base_v, 60.0, f"V{i}-A"), _assessment(cand_v, 40.0, f"V{i}-B")],
            comparison=VersionComparison(direction, "release", "测试"),
        ))
    return EvaluationResult(dataset=Dataset(cases=[]), results=results, manifest={})


# ------------------------------------------------------------ 三态配对比较

def _cmp(base: Verdict, cand: Verdict) -> VersionComparison:
    return compare_versions(_paired_case(), _assessment(base), _assessment(cand))


def test_pass_vs_fail_is_regression():
    cmp = _cmp(Verdict.PASS, Verdict.FAIL)
    assert cmp.direction == "baseline_better"
    assert cmp.release_recommendation == "defer"


def test_pass_vs_unknown_is_insufficient_not_regression():
    """UNKNOWN 不能被当成退化。"""
    cmp = _cmp(Verdict.PASS, Verdict.UNKNOWN)
    assert cmp.direction == "insufficient_evidence"
    assert cmp.release_recommendation == "investigate"
    assert "UNKNOWN" in cmp.reason


def test_fail_vs_unknown_is_insufficient_not_improvement():
    """UNKNOWN 也不能被当成改善——这是最容易出错的方向。"""
    cmp = _cmp(Verdict.FAIL, Verdict.UNKNOWN)
    assert cmp.direction == "insufficient_evidence"
    assert cmp.release_recommendation == "investigate"


def test_unknown_vs_unknown_is_insufficient():
    cmp = _cmp(Verdict.UNKNOWN, Verdict.UNKNOWN)
    assert cmp.direction == "insufficient_evidence"
    assert cmp.quality_delta == 0.0


# ------------------------------------------------------------ 聚合与统计

def test_unknown_blocks_plain_release():
    """有改善但存在 UNKNOWN 时，不能直接 release。"""
    result = _result([
        (Verdict.FAIL, Verdict.PASS, "candidate_better"),
        (Verdict.UNKNOWN, Verdict.UNKNOWN, "insufficient_evidence"),
    ])
    recommendation, reason = aggregate_recommendation(result)
    assert recommendation != "release"
    assert "UNKNOWN" in reason


def test_all_clean_can_release():
    result = _result([(Verdict.FAIL, Verdict.PASS, "candidate_better")])
    recommendation, _ = aggregate_recommendation(result)
    assert recommendation == "release"


def test_summarise_reports_three_states():
    result = _result([
        (Verdict.PASS, Verdict.FAIL, "baseline_better"),
        (Verdict.UNKNOWN, Verdict.UNKNOWN, "insufficient_evidence"),
    ])
    summary = summarise(result)
    assert summary["verdicts"] == {"PASS": 1, "FAIL": 1, "UNKNOWN": 2}
    assert summary["task_unknown"] == 2
    assert summary["abstained"] == 2


# ------------------------------------------------- 官方提交格式映射分离

def test_official_submission_maps_unknown_without_faking_failure():
    """UNKNOWN 在官方 Schema 里只能写 false，但必须写清原因与 INSUFFICIENT_EVIDENCE 根因。"""
    result = _result([(Verdict.FAIL, Verdict.PASS, "candidate_better")])
    cr = result.results[0]
    cr.assessments[0] = _assessment(Verdict.UNKNOWN, 20.0)
    payload = cr.to_submission()
    unknown_payload = payload["run_assessments"][0]
    assert unknown_payload["agent_task_success"] is False
    assert "UNKNOWN" in unknown_payload["explanation"]
    assert any(rc["label"] == "INSUFFICIENT_EVIDENCE" for rc in unknown_payload["root_causes"])


def test_assess_run_returns_unknown_when_state_mismatch_but_no_rule():
    """终态偏离但规则无法定位 → UNKNOWN（R900），不是 FAIL。"""
    run = make_run("U-A", basic_flow(final_state="WAITING_USER", invoice_created=False),
                   final_state="WAITING_USER", invoice_created=False)
    case = make_case(context=context(),
                     reference_result=reference(acceptable_snapshot_state="TIMEOUT_TASK",
                                                invoice_should_exist=False),
                     runs=[run])
    assessment = assess_run(case, run)
    assert assessment.verdict is Verdict.UNKNOWN
    assert assessment.labels == ["INSUFFICIENT_EVIDENCE"]
    assert assessment.agent_task_success is False
