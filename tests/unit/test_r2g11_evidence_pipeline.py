"""R2-G1.1 P0：专项 Controls 全流程证据引用核对。"""

from __future__ import annotations

from typing import Any

import pytest

from tracelab.final_round import ScriptedJudge, run_controls


def _event(run_id: str, seq: int = 1) -> dict[str, Any]:
    return {
        "event_id": f"{run_id}-E{seq:02d}",
        "seq": seq,
        "step_id": "INPUT",
        "step_status": "SUCCEEDED",
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
    }


def _item(run_id: str) -> dict[str, Any]:
    return {
        "judge_input": {
            "business_type": "ai_consultant",
            "domain": "salary",
            "user_request": "查询工资",
            "context": {"enterprise_id": "E1"},
            "business_objective": {"business_goal": "只查询"},
            "observable_run": {"events": [_event(run_id)]},
        },
        "linkage_only": {"case_id": f"CASE-{run_id}", "run_id": run_id, "agent_version": "v1"},
    }


def _controls(run_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "control_id": "CTRL-1",
            "control_type": "EQUIVALENT",
            "expected_boundary": "证据必须可核对",
            "source_case_id": "CASE",
            "baseline_inputs": [_item(run_ids[0]), _item(run_ids[1])],
            "variant_inputs": [_item(run_ids[2]), _item(run_ids[3])],
        }
    ]


def _raw(mode: str, run_id: str) -> dict[str, Any]:
    current = f"{run_id}-E01"
    other = "RUN-OTHER-E01" if run_id != "RUN-OTHER" else "RUN-1-E01"
    evidence = [current]
    first: str | None = None
    dimensions = [{"name": "d", "conclusion": "PASS", "reason": "ok", "evidence_event_ids": [current]}]
    root_causes: list[dict[str, Any]] = []
    if mode == "invalid_top":
        evidence = ["MISSING-EVENT"]
    elif mode == "invalid_first":
        first = "MISSING-EVENT"
    elif mode == "invalid_dimension":
        dimensions[0]["evidence_event_ids"] = ["MISSING-EVENT"]
    elif mode == "invalid_root":
        root_causes = [
            {
                "label": "AMOUNT_BASIS",
                "first_fault_event_id": "MISSING-EVENT",
                "evidence_event_ids": ["MISSING-EVENT"],
                "explanation": "bad",
            }
        ]
    elif mode == "cross_run":
        evidence = [other]
        dimensions[0]["evidence_event_ids"] = [other]
    return {
        "verdict": "PASS",
        "agent_task_success": True,
        "business_result_correct": True,
        "reason": f"mode={mode}",
        "dimensions": dimensions,
        "evidence_event_ids": evidence,
        "first_fault_event_id": first,
        "root_causes": root_causes,
        "uncertainty": {"level": "low", "reason": "test"},
    }


class EvidenceJudge(ScriptedJudge):
    def __init__(self, modes: dict[str, str]) -> None:
        super().__init__()
        self.modes = modes
        self.calls = 0

    def evaluate(self, judge_input, linkage):  # type: ignore[no-untyped-def]
        self.calls += 1
        run_id = str(linkage.get("run_id") or "")
        return _raw(self.modes.get(run_id, "valid"), run_id)


def _run(mode: str = "valid") -> tuple[Any, EvidenceJudge]:
    run_ids = ["RUN-1", "RUN-2", "RUN-3", "RUN-4"]
    judge = EvidenceJudge({run_id: mode for run_id in run_ids})
    return run_controls(_controls(run_ids), judge), judge


def _assessment(batch: Any, run_id: str) -> dict[str, Any]:
    for control in batch.controls:
        for side in ("baseline", "variant"):
            for item in control[side]:
                if item["assessment"]["run_id"] == run_id:
                    return item["assessment"]
    raise AssertionError(f"missing {run_id}")


def test_valid_evidence_passes_full_control_pipeline():
    batch, _ = _run("valid")
    assert batch.to_manifest()["output_errors"] == 0
    assert batch.to_manifest()["input_errors"] == 0
    assert all(receipt["verdict"] == "PASS" for receipt in batch.receipts)
    for run_id in ("RUN-1", "RUN-2", "RUN-3", "RUN-4"):
        assessment = _assessment(batch, run_id)
        assert assessment["verdict"] == "PASS"
        assert assessment["agent_task_success"] is True


@pytest.mark.parametrize("mode", ["invalid_top", "invalid_first", "invalid_root", "invalid_dimension", "cross_run"])
def test_invalid_evidence_becomes_no_result_without_affecting_others(mode: str):
    batch, _ = _run(mode)
    manifest = batch.to_manifest()
    assert manifest["output_errors"] == 4
    assert manifest["input_errors"] == 0
    assert manifest["no_result"] == 4
    for run_id in ("RUN-1", "RUN-2", "RUN-3", "RUN-4"):
        assessment = _assessment(batch, run_id)
        assert assessment["verdict"] == "NO_RESULT"
        assert assessment["agent_task_success"] is None
        assert assessment["business_result_correct"] is None
        assert "证据引用非法" in assessment["reason"]
    assert all(receipt["verdict"] == "NO_RESULT" for receipt in batch.receipts)


def test_one_invalid_record_does_not_block_valid_records():
    run_ids = ["RUN-1", "RUN-2", "RUN-3", "RUN-4"]
    judge = EvidenceJudge({"RUN-2": "invalid_top"})
    batch = run_controls(_controls(run_ids), judge)
    assert _assessment(batch, "RUN-1")["verdict"] == "PASS"
    assert _assessment(batch, "RUN-2")["verdict"] == "NO_RESULT"
    assert _assessment(batch, "RUN-3")["verdict"] == "PASS"
    assert batch.to_manifest()["output_errors"] == 1


def test_input_error_is_counted_separately_from_output_error():
    run_ids = ["RUN-1", "RUN-2", "RUN-3", "RUN-4"]
    controls = _controls(run_ids)
    controls[0]["baseline_inputs"][0]["judge_input"].pop("business_type")
    batch = run_controls(controls, EvidenceJudge({}))
    manifest = batch.to_manifest()
    assert manifest["input_errors"] == 1
    assert manifest["output_errors"] == 0
    assert manifest["no_result"] == 1
