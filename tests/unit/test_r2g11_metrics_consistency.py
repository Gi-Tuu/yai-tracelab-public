"""R2-G1.1 P1：有效判定与首错指标跨脚本一致性。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _result(case_id: str, run_id: str, verdict: str, *, task=None, first=None, evidence=None) -> dict:
    return {
        "case_id": case_id,
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": verdict,
                "agent_task_success": task,
                "business_invoice_created": False,
                "first_fault_event_id": first,
                "evidence_event_ids": evidence or [],
                "reason": "r",
            }
        ],
    }


def _label(case_id: str, run_id: str, verdict: str, *, task=None, first=None) -> dict:
    return {
        "case_id": case_id,
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": verdict,
                "agent_task_success": task,
                "business_invoice_created": False,
                "first_fault_event_id": first,
            }
        ],
    }


def _report_and_index(results, labels):
    report_mod = _load("final_round_report_r2g11", "scripts/final_round_report.py")
    index_mod = _load("final_round_index_r2g11", "scripts/final_round_experiment_index.py")
    label_map = report_mod._load_labels_for_test(labels) if hasattr(report_mod, "_load_labels_for_test") else {
        row["case_id"]: {item["run_id"]: item for item in row["run_assessments"]} for row in labels
    }
    report_metrics = report_mod.compute(results, label_map, {})
    index = index_mod.build_index("x", results, labels, None)
    return report_metrics, index


def test_mixed_verdicts_effective_and_no_result_are_consistent():
    results = [
        _result("C1", "R1", "PASS", task=True),
        _result("C2", "R2", "FAIL", task=False),
        _result("C3", "R3", "REJECT"),
        _result("C4", "R4", "NO_RESULT"),
        _result("C5", "R5", "UNKNOWN"),
    ]
    labels = [
        _label("C1", "R1", "PASS", task=True),
        _label("C2", "R2", "FAIL", task=False),
        _label("C3", "R3", "REJECT"),
        _label("C4", "R4", "NO_RESULT"),
        _label("C5", "R5", "FAIL", task=False),
    ]
    from tracelab.final_round.metrics import compute_metrics, pair_runs

    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["total_runs"] == 5
    assert metrics["valid_verdicts"] == 4
    assert metrics["effective_judgments"] == 3
    assert metrics["no_result"] == 1
    assert metrics["invalid_verdicts"] == 1
    report_metrics, index = _report_and_index(results, labels)
    assert report_metrics["effective_judgments"] == 3
    assert report_metrics["no_result_count"] == 1
    assert report_metrics["invalid_verdicts"] == 1
    assert index["effective_judgments"] == 3
    assert index["no_result"] == 1
    assert index["invalid_verdicts"] == 1


def test_all_no_result_has_zero_effective_and_na_first_fault_rates():
    results = [_result("C1", "R1", "NO_RESULT"), _result("C2", "R2", "NO_RESULT")]
    labels = [_label("C1", "R1", "NO_RESULT"), _label("C2", "R2", "NO_RESULT")]
    from tracelab.final_round.metrics import compute_metrics, pair_runs

    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["effective_judgments"] == 0
    assert metrics["no_result"] == 2
    assert metrics["first_fault_expected"] == 0
    assert metrics["first_fault"]["location_coverage"] is None
    assert metrics["first_fault"]["exact_match_rate"] is None
    report_metrics, index = _report_and_index(results, labels)
    assert report_metrics["first_fault_expected"] == 0
    assert index["effective_judgments"] == 0


def test_partial_first_fault_location_and_exact_match():
    results = [
        _result("C1", "R1", "FAIL", task=False, first="E1"),
        _result("C2", "R2", "FAIL", task=False, first=None),
    ]
    labels = [
        _label("C1", "R1", "FAIL", task=False, first="E1"),
        _label("C2", "R2", "FAIL", task=False, first="E2"),
    ]
    from tracelab.final_round.metrics import compute_metrics, pair_runs

    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["first_fault_expected"] == 2
    assert metrics["first_fault_located"] == 1
    assert metrics["first_fault_exact_match"] == 1
    assert metrics["first_fault"]["location_coverage"] == 0.5
    assert metrics["first_fault"]["exact_match_rate"] == 0.5
    report_metrics, index = _report_and_index(results, labels)
    assert report_metrics["first_fault_expected"] == 2
    assert report_metrics["first_fault_located"] == 1
    assert index["metrics"]["first_fault_located"] == 1


def test_first_fault_all_null():
    results = [_result("C1", "R1", "PASS", task=True)]
    labels = [_label("C1", "R1", "PASS", task=True)]
    from tracelab.final_round.metrics import compute_metrics, pair_runs

    pairs, _ = pair_runs(results, labels)
    metrics = compute_metrics(pairs)
    assert metrics["first_fault_both_null"] == 1
    assert metrics["first_fault_expected"] == 0


def test_no_expert_labels_does_not_fabricate_metrics():
    results = [_result("C1", "R1", "PASS", task=True)]
    from tracelab.final_round.metrics import compute_metrics, pair_runs

    pairs, pairing = pair_runs(results, [])
    assert pairs == []
    metrics = compute_metrics(pairs)
    assert metrics["total_runs"] == 0
    assert metrics["verdict_agreement"]["value"] is None
    report_metrics, index = _report_and_index(results, [])
    assert report_metrics["total_runs"] == 0
    assert index["pairing"]["only_a"] == 1
    assert index["metrics"]["total_runs"] == 0
