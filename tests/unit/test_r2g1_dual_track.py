"""R2-G1 P0-D：初赛原格式与复赛扩展格式的双轨核对。"""

from __future__ import annotations

from tracelab.final_round.legacy import link_extended, to_legacy_case, validate_dual_track


def _assessment(run_id: str, success: bool = True, **extra):
    base = {
        "run_id": run_id,
        "verdict": "PASS" if success else "FAIL",
        "agent_task_success": success,
        "business_result_correct": success,
        "business_invoice_created": False,
        "reason": "理由",
        "dimensions": [],
        "evidence_event_ids": [],
        "root_causes": [],
        "first_fault_event_id": None,
    }
    base.update(extra)
    return base


def _result(case_id: str = "INV-1", **extra):
    base = {
        "case_id": case_id,
        "business_type": "invoice",
        "version_comparison": {"direction": "equal", "reason": "两侧一致"},
        "run_assessments": [
            _assessment("A", legacy_quality_score=90.0, legacy_penalty_items=[]),
            _assessment("B", legacy_quality_score=90.0, legacy_penalty_items=[]),
        ],
    }
    base.update(extra)
    return base


def test_mapping_without_real_quality_source_is_refused():
    result = _result()
    result["run_assessments"][0].pop("legacy_quality_score")
    assert to_legacy_case(result) is None


def test_failed_run_without_penalty_or_suggestion_is_refused():
    result = _result()
    result["run_assessments"][0].update(
        {"verdict": "FAIL", "agent_task_success": False, "legacy_penalty_items": [], "legacy_improvement_suggestions": []}
    )
    assert to_legacy_case(result) is None


def test_mapping_uses_explicit_numbers_only():
    result = _result()
    legacy = to_legacy_case(result)
    assert legacy is not None
    assert legacy["run_assessments"][0]["quality_score"] == 90.0


def test_dual_track_checks_unique_and_refs():
    legacy = [{"case_id": "INV-1"}, {"case_id": "INV-1"}]
    extended = [
        link_extended(_result("INV-1"), "legacy_submission.jsonl#INV-1"),
        link_extended(_result("INV-2"), "legacy_submission.jsonl#MISSING"),
        link_extended({"case_id": "AIC-1", "business_type": "ai_consultant"}, None, note="legacy_not_applicable"),
    ]
    checks = validate_dual_track(legacy, extended)
    assert "INV-1" in checks["duplicate_legacy_ids"]
    assert "INV-2" in checks["broken_legacy_ref"]
    assert checks["linked"] == 1
    assert checks["missing_legacy_ref"] == []
    assert checks["missing_extended"] == []


def test_ai_consultant_is_not_required_to_have_legacy():
    checks = validate_dual_track([], [{"case_id": "AIC-1", "business_type": "ai_consultant"}])
    assert checks["missing_legacy_ref"] == []
    assert checks["linked"] == 0
