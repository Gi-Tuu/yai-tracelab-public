"""R2-G1.2 P1：复合身份与分类证据统计。"""

from __future__ import annotations

from tracelab.final_round.metrics import compute_metrics, pair_runs


def _result(case_id: str, run_id: str, *, evidence=None, dims=None, roots=None, first=None) -> dict:
    return {
        "case_id": case_id,
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": "FAIL",
                "agent_task_success": False,
                "evidence_event_ids": evidence or [],
                "first_fault_event_id": first,
                "dimensions": [
                    {"name": "d", "conclusion": "FAIL", "reason": "r", "evidence_event_ids": dims or []}
                ],
                "root_causes": [
                    {
                        "label": "AMOUNT_BASIS",
                        "first_fault_event_id": first,
                        "evidence_event_ids": roots or [],
                        "explanation": "e",
                    }
                ],
            }
        ],
    }


def _label(case_id: str, run_id: str, first: str | None = "E1") -> dict:
    return {
        "case_id": case_id,
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": "FAIL",
                "agent_task_success": False,
                "first_fault_event_id": first,
            }
        ],
    }


def test_same_run_id_different_cases_do_not_cross_contaminate_events():
    results = [
        _result("C1", "R1", evidence=["E1"], dims=["E1"], roots=["E1"], first="E1"),
        _result("C2", "R1", evidence=["E2"], dims=["E1"], roots=["E2"], first="E2"),
    ]
    labels = [_label("C1", "R1"), _label("C2", "R1")]
    pairs, pairing = pair_runs(results, labels)
    assert pairing["matched"] == 2
    metrics = compute_metrics(
        pairs,
        events_by_run={("C1", "R1"): {"E1"}, ("C2", "R1"): {"E2"}},
    )
    categories = metrics["evidence_by_category"]
    assert categories["top_level"]["total"] == 2
    assert categories["top_level"]["located"] == 2
    assert categories["dimensions"]["total"] == 2
    assert categories["dimensions"]["located"] == 1
    assert categories["dimensions"]["missing"] == 1
    assert categories["root_causes"]["located"] == 2
    assert categories["first_fault"]["located"] == 2


def test_missing_event_data_is_na_not_zero_or_all_correct():
    results = [_result("C1", "R1", evidence=["E1"], dims=["E1"], first="E1")]
    labels = [_label("C1", "R1")]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    for category in ("top_level", "dimensions", "root_causes", "first_fault"):
        stats = metrics["evidence_by_category"][category]
        assert stats["value"] is None
        assert stats["unverifiable"] == stats["total"]


def test_first_fault_category_counts_only_non_null_refs():
    results = [_result("C1", "R1", first=None)]
    labels = [_label("C1", "R1", first=None)]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs, events_by_run={("C1", "R1"): {"E1"}})
    assert metrics["evidence_by_category"]["first_fault"]["total"] == 0
    assert metrics["first_fault_both_null"] == 1
