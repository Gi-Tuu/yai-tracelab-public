"""R2-G1 P1-A：指标配对、严格类型与有效性统计。"""

from __future__ import annotations

from tracelab.final_round.metrics import compute_metrics, pair_runs, strict_bool


def _run(case_id: str, run_id: str, verdict: str = "PASS", task=True, **extra):
    row = {
        "case_id": case_id,
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": verdict,
                "agent_task_success": task,
                "business_invoice_created": False,
                "first_fault_event_id": None,
                "evidence_event_ids": [],
                "reason": "r",
            }
        ],
    }
    row.update(extra)
    return row


def test_partial_pairs_and_unmatched_counts():
    results = [_run("C1", "R1"), _run("C2", "R2")]
    labels = [_run("C1", "R1")]
    pairs, stats = pair_runs(results, labels)
    assert stats["matched"] == 1
    assert stats["only_a"] == 1
    assert stats["only_b"] == 0
    assert stats["a_total_runs"] == 2
    assert stats["b_total_runs"] == 1
    metrics = compute_metrics(pairs)
    assert metrics["runs"] == 1
    assert metrics["verdict_agreement"]["total"] == 1


def test_duplicates_are_counted_not_silently_merged():
    results = [_run("C1", "R1"), _run("C1", "R1")]
    labels = [_run("C1", "R1")]
    pairs, stats = pair_runs(results, labels)
    assert stats["duplicate_a"]
    assert stats["matched"] == 1


def test_string_booleans_are_not_truthy():
    assert strict_bool("false") is None
    results = [_run("C1", "R1", task="false")]
    labels = [_run("C1", "R1", task=True)]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["task_agreement"]["total"] == 0
    assert metrics["task_agreement"]["value"] is None
    assert metrics["invalid_label_values"] == 0


def test_invalid_label_boolean_counted():
    results = [_run("C1", "R1", task=True)]
    labels = [_run("C1", "R1", task="false")]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["invalid_label_values"] == 1
    assert metrics["task_agreement"]["total"] == 0


def test_no_effective_judgment_and_first_fault_denominators():
    results = [_run("C1", "R1", verdict="NO_RESULT", task=None)]
    labels = [_run("C1", "R1", verdict="NO_RESULT", task=None)]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["verdict_agreement"]["value"] == 1.0
    assert metrics["first_fault"]["both_null"] == 1
    assert metrics["first_fault"]["coverage"] == 0.0
    assert metrics["task_agreement"]["value"] is None


def test_one_side_first_fault_counts_as_defined():
    results = [_run("C1", "R1", task=False, verdict="FAIL")]
    results[0]["run_assessments"][0]["first_fault_event_id"] = "E1"
    labels = [_run("C1", "R1", task=False, verdict="FAIL")]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs, events_by_run={"R1": {"E1"}})
    assert metrics["first_fault"]["total"] == 1
    assert metrics["first_fault"]["hit"] == 0


def test_evidence_validity_counts():
    results = [_run("C1", "R1")]
    results[0]["run_assessments"][0]["evidence_event_ids"] = ["E1", "E404"]
    labels = [_run("C1", "R1")]
    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs, events_by_run={"R1": {"E1"}})
    assert metrics["evidence_reference_validity"] == {"valid": 1, "total": 2, "value": 0.5}
