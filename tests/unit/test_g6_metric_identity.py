"""G6-0A：首因 Exact Match 的身份完整性（Case + Run + Label + Event）。

背景：修复前去重身份只有 `(label, event_id)`，于是
- 不同运行里恰好同名的事件编号会被合并成一条；
- 不同 Case 里同名标签 + 同名事件编号也会被合并。

本文件用全自造样本验证四元组身份，并核对「分母 == 实际可评分项目数量」。
"""

from __future__ import annotations

from tracelab.attribution import RunAssessment, Violation
from tracelab.contracts import Case, Dataset, Event, Run, Verdict
from tracelab.evaluation import CaseResult, EvaluationResult, VersionComparison, score_against_labels


def _event(event_id: str, seq: int, step_id: str = "COLLECT_INVOICE_DATA") -> Event:
    return Event(
        event_id=event_id, seq=seq, step_id=step_id, module=step_id, action=step_id,
        step_status="SUCCEEDED", result_code="E100", error_code=None, error_message=None,
        retry_count=0, observed_input={}, observed_output={}, tool_calls=(),
        started_at="", finished_at="", duration_ms=1, raw={},
    )


def _run(run_id: str, events: list[Event]) -> Run:
    return Run(
        run_id=run_id, agent_version=f"version-of-{run_id}", task_id=f"T-{run_id}",
        trace_id=f"TR-{run_id}", events=tuple(events), final_state="SUCCEEDED",
        snapshot_state="SUCCEEDED", workflow_terminal=True, invoice_created=True,
        resource_usage={}, raw={},
    )


def _case(case_id: str, runs: list[Run]) -> Case:
    return Case(
        case_id=case_id, scenario="synthetic", user_request="synthetic", context={},
        reference_result={}, runs=tuple(runs), dataset_version="g6-synthetic",
        data_origin="synthetic", raw={},
    )


def _violation(label: str, event_id: str, seq: int = 1) -> Violation:
    return Violation(
        rule_id="R_TEST", rule_version="test", label=label, event_id=event_id, seq=seq,
        evidence_ids=(event_id,), explanation="synthetic", dimension="BUSINESS_RESULT",
        points=30.0, confidence=1.0,
    )


def _assessment(run_id: str, labels_at: dict[str, str]) -> RunAssessment:
    """labels_at: label -> 本运行内该标签的 event_id（我们自己的判定）。"""
    return RunAssessment(
        case_id="ignored", run_id=run_id, agent_version=f"version-of-{run_id}",
        verdict=Verdict.FAIL, business_invoice_created=True, quality_score=70.0,
        violations=[_violation(label, event) for label, event in labels_at.items()],
        uncertainties=[],
    )


def _result(cases: list[Case], assessments_by_case: dict[str, list[RunAssessment]]) -> EvaluationResult:
    results = [
        CaseResult(case=case, assessments=assessments_by_case[case.case_id],
                   comparison=VersionComparison("equal", "investigate", "synthetic"))
        for case in cases
    ]
    dataset = Dataset(cases=cases, source="g6-synthetic", sha256="0" * 64,
                      stats={"lines_total": len(cases), "cases_parsed": len(cases), "skipped": 0,
                             "truncated": 0, "runs_total": sum(len(c.runs) for c in cases),
                             "events_total": 0})
    return EvaluationResult(dataset=dataset, results=results, manifest={})


def _gold(case_id: str, entries: list[tuple[str, str, str]]) -> dict[str, object]:
    """entries: (run_id, label, first_fault_event_id)。"""
    return {
        "case_id": case_id,
        "run_assessments": [
            {"run_id": run_id, "agent_task_success": False, "business_invoice_created": True,
             "root_causes": [{"label": label, "first_fault_event_id": event_id}]}
            for run_id, label, event_id in entries
        ],
    }


def test_same_run_duplicate_gold_counts_once():
    """同一运行内重复标注同一条（label, event）只计算一次。"""
    case = _case("CASE-DUP", [_run("DUP-R1", [_event("E-05", 5)])])
    gold = _gold("CASE-DUP", [("DUP-R1", "AMOUNT_BASIS", "E-05"),
                             ("DUP-R1", "AMOUNT_BASIS", "E-05")])
    result = _result([case], {"CASE-DUP": [_assessment("DUP-R1", {"AMOUNT_BASIS": "E-05"})]})

    values = score_against_labels(result, {"CASE-DUP": gold}).values
    assert values["first_fault_event_scored"] == 1
    assert values["first_fault_event_exact_match"] == 1.0


def test_same_event_id_in_different_runs_counts_separately():
    """不同运行里恰好同名的事件编号（都叫 E-05）必须分别计算。"""
    case = _case("CASE-RUNS", [
        _run("RUN-1", [_event("E-05", 5)]),
        _run("RUN-2", [_event("E-05", 5)]),
    ])
    gold = _gold("CASE-RUNS", [("RUN-1", "AMOUNT_BASIS", "E-05"),
                               ("RUN-2", "AMOUNT_BASIS", "E-05")])
    result = _result([case], {"CASE-RUNS": [
        _assessment("RUN-1", {"AMOUNT_BASIS": "E-05"}),
        _assessment("RUN-2", {"AMOUNT_BASIS": "OTHER"}),   # 第二个 run 判错了首因
    ]})

    values = score_against_labels(result, {"CASE-RUNS": gold}).values
    assert values["first_fault_event_scored"] == 2, "两个运行必须各占一个分母"
    assert values["first_fault_event_exact_match"] == 0.5, "只有第一个运行命中"


def test_same_label_and_event_across_cases_counts_separately():
    """不同 Case 的相同标签 + 相同事件编号必须分别计算。"""
    case_a = _case("CASE-A", [_run("A-1", [_event("E-05", 5)])])
    case_b = _case("CASE-B", [_run("B-1", [_event("E-05", 5)])])
    labels = {
        "CASE-A": _gold("CASE-A", [("A-1", "AMOUNT_BASIS", "E-05")]),
        "CASE-B": _gold("CASE-B", [("B-1", "AMOUNT_BASIS", "E-05")]),
    }
    result = _result([case_a, case_b], {
        "CASE-A": [_assessment("A-1", {"AMOUNT_BASIS": "E-05"})],
        "CASE-B": [_assessment("B-1", {"AMOUNT_BASIS": "E-05"})],
    })

    values = score_against_labels(result, labels).values
    assert values["first_fault_event_scored"] == 2, "两个 Case 必须各占一个分母"
    assert values["first_fault_event_exact_match"] == 1.0


def test_denominator_matches_scoreable_items_and_undefined_is_excluded():
    """分母 == 可评分金标数量；金标首因为空的项目单列，不进分母。"""
    case = _case("CASE-MIX", [
        _run("MIX-R1", [_event("E-01", 1)]),
        _run("MIX-R2", [_event("E-02", 2)]),
    ])
    gold = {
        "case_id": "CASE-MIX",
        "run_assessments": [
            {"run_id": "MIX-R1", "agent_task_success": False, "business_invoice_created": True,
             "root_causes": [
                 {"label": "AMOUNT_BASIS", "first_fault_event_id": "E-01"},
                 {"label": "AMOUNT_BASIS", "first_fault_event_id": None},          # 未定义
             ]},
            {"run_id": "MIX-R2", "agent_task_success": False, "business_invoice_created": True,
             "root_causes": [{"label": "PRODUCT_MATCH", "first_fault_event_id": "E-02"}]},
        ],
    }
    result = _result([case], {"CASE-MIX": [
        _assessment("MIX-R1", {"AMOUNT_BASIS": "E-01"}),
        _assessment("MIX-R2", {"PRODUCT_MATCH": "E-99"}),
    ]})

    values = score_against_labels(result, {"CASE-MIX": gold}).values
    assert values["first_fault_exact_match_undefined"] == 1
    assert values["first_fault_event_scored"] == 2, "可评分项只有 2 条（1 条未定义被排除）"
    assert values["first_fault_event_exact_match"] == 0.5
