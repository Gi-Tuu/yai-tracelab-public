"""R2-G1.2 P2：--limit 与完整运行的配置版本身份一致。"""

from __future__ import annotations

import json
from pathlib import Path

from tracelab.final_round import ScriptedJudge, run_cases, run_file
from tracelab.final_round.runner import default_config_version


def _event(run_id: str) -> dict:
    return {
        "event_id": f"{run_id}-E01",
        "seq": 1,
        "step_id": "INPUT",
        "step_status": "SUCCEEDED",
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
    }


def _case() -> dict:
    return {
        "case_id": "CASE-1",
        "dataset_version": "dv1",
        "data_origin": "review",
        "near_duplicate_group": "review",
        "data_purpose": "test",
        "business_type": "ai_consultant",
        "domain": "salary",
        "user_request": "查询工资",
        "context": {"enterprise_id": "E1"},
        "reference_result": {
            "business_goal": "只查询",
            "pass_criteria": [],
            "required_evidence": [],
            "rule_source": {"project": "x"},
            "reference_semantics": "business_goal_only",
        },
        "runs": [
            {
                "run_id": "R1", "task_id": "T1", "trace_id": "TR1", "agent_version": "v1",
                "events": [_event("R1")], "final_state": "DONE", "snapshot_state": "DONE",
                "workflow_terminal": True, "observation_complete": True,
            },
            {
                "run_id": "R2", "task_id": "T2", "trace_id": "TR2", "agent_version": "v2",
                "events": [_event("R2")], "final_state": "DONE", "snapshot_state": "DONE",
                "workflow_terminal": True, "observation_complete": True,
            },
        ],
    }


class LiveJudge(ScriptedJudge):
    execution_origin = "LIVE_MODEL"


class CustomLiveJudge(ScriptedJudge):
    execution_origin = "LIVE_MODEL_CUSTOM"


def test_default_config_versions_by_mode():
    assert default_config_version("LIVE_MODEL") == "final-round-live-v0"
    assert default_config_version("LIVE_MODEL_CUSTOM") == "final-round-live-custom-v0"
    assert default_config_version("SCRIPTED_TEST_NOT_EXECUTED") == "scripted-v0"


def test_limited_and_full_runs_share_config_version(tmp_path: Path):
    limited = run_cases([_case()], LiveJudge())
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")
    full = run_file(path, tmp_path / "full", judge=LiveJudge())
    assert limited.results[0]["config_version"] == "final-round-live-v0"
    assert full.results[0]["config_version"] == "final-round-live-v0"
    assert limited.results[0]["config_version"] == full.results[0]["config_version"]


def test_custom_live_uses_custom_identity():
    batch = run_cases([_case()], CustomLiveJudge())
    assert batch.results[0]["config_version"] == "final-round-live-custom-v0"
