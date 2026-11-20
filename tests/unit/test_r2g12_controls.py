"""R2-G1.2 P1：Controls 预期边界与评估状态。"""

from __future__ import annotations

from typing import Any

from tracelab.final_round.controls import _evaluate_boundary, run_controls
from tracelab.final_round.judge import ScriptedJudge


def test_boundary_matrix_distinguishes_consistency_from_expected_relation():
    assert _evaluate_boundary("EQUIVALENT", ["PASS"], ["PASS"])["expected_boundary_met"] is True
    assert _evaluate_boundary("EQUIVALENT", ["PASS"], ["FAIL"])["expected_boundary_met"] is False
    no_result = _evaluate_boundary("EQUIVALENT", ["NO_RESULT"], ["NO_RESULT"])
    assert no_result["verdict_consistent"] is True
    assert no_result["expected_boundary_met"] is False
    assert no_result["evaluation_status"] == "INSUFFICIENT_EVIDENCE"
    mixed = _evaluate_boundary("PROMPT_INJECTION", ["PASS"], ["NO_RESULT"])
    assert mixed["evaluation_status"] == "PARTIAL"
    assert mixed["expected_boundary_met"] is False


def test_type_specific_boundaries():
    assert _evaluate_boundary("MISSING_EVIDENCE", ["PASS"], ["REJECT"])["expected_boundary_met"] is True
    assert _evaluate_boundary("MISSING_EVIDENCE", ["PASS"], ["PASS"])["expected_boundary_met"] is False
    assert _evaluate_boundary("FACT_COUNTERFACTUAL", ["PASS"], ["FAIL"])["expected_boundary_met"] is True
    assert _evaluate_boundary("FACT_COUNTERFACTUAL", ["PASS"], ["PASS"])["expected_boundary_met"] is False
    unknown = _evaluate_boundary("BUSINESS_MISMATCH", ["REJECT"], [])
    assert unknown["evaluation_status"] == "INSUFFICIENT_EVIDENCE"
    assert unknown["expected_boundary_met"] is False


def _item(run_id: str) -> dict[str, Any]:
    return {
        "judge_input": {
            "business_type": "ai_consultant",
            "domain": "salary",
            "user_request": "查询工资",
            "context": {"enterprise_id": "E1"},
            "business_objective": {"business_goal": "只查询"},
            "observable_run": {
                "events": [
                    {
                        "event_id": f"{run_id}-E01",
                        "seq": 1,
                        "step_id": "INPUT",
                        "step_status": "SUCCEEDED",
                        "observed_input": {},
                        "observed_output": {},
                        "tool_calls": [],
                    }
                ]
            },
        },
        "linkage_only": {"case_id": "C", "run_id": run_id, "agent_version": "v1"},
    }


class NoResultJudge(ScriptedJudge):
    def evaluate(self, judge_input, linkage):  # type: ignore[no-untyped-def]
        return {
            "verdict": "NO_RESULT",
            "reason": "no evidence",
            "dimensions": [{"name": "d", "conclusion": "NOT_APPLICABLE", "reason": "x", "evidence_event_ids": []}],
            "evidence_event_ids": [],
            "first_fault_event_id": None,
            "root_causes": [],
            "uncertainty": {"level": "unknown", "reason": "x"},
        }


def test_full_controls_flow_marks_all_no_result_as_insufficient():
    control = {
        "control_id": "C1",
        "control_type": "EQUIVALENT",
        "expected_boundary": "same",
        "source_case_id": "C",
        "baseline_inputs": [_item("R1")],
        "variant_inputs": [_item("R2")],
    }
    batch = run_controls([control], NoResultJudge())
    row = batch.controls[0]
    assert row["consistent"] is True
    assert row["verdict_consistent"] is True
    assert row["valid_inputs"] == 0
    assert row["no_result_inputs"] == 2
    assert row["expected_boundary_met"] is False
    assert row["evaluation_status"] == "INSUFFICIENT_EVIDENCE"
    assert batch.to_manifest()["evaluation_status"]["INSUFFICIENT_EVIDENCE"] == 1
