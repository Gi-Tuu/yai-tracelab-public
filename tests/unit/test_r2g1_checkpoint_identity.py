"""R2-G1 P0-B：专项实验断点身份与恢复。"""

from __future__ import annotations

import json
from pathlib import Path

from tracelab.final_round import ScriptedJudge, run_controls


class CountingJudge(ScriptedJudge):
    def __init__(self, model: str = "model-a", prompt_version: str = "prompt-a") -> None:
        super().__init__()
        self.model = model
        self.prompt_version = prompt_version
        self.calls = 0

    def evaluate(self, judge_input, linkage):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().evaluate(judge_input, linkage)


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


def _item(run_id: str, request: str = "查询工资") -> dict:
    return {
        "judge_input": {
            "business_type": "ai_consultant",
            "domain": "salary",
            "user_request": request,
            "context": {"enterprise_id": "E1"},
            "business_objective": {"business_goal": "只查询"},
            "observable_run": {"events": [_event(run_id)]},
        },
        "linkage_only": {"case_id": "CASE", "run_id": run_id, "agent_version": "v1"},
    }


def _controls() -> list[dict]:
    return [
        {
            "control_id": "CTRL-1",
            "control_type": "EQUIVALENT",
            "expected_boundary": "表达变化不改变判定",
            "source_case_id": "CASE",
            "baseline_inputs": [_item("RUN-1"), _item("RUN-2")],
            "variant_inputs": [_item("RUN-3"), _item("RUN-4")],
        },
        {
            "control_id": "CTRL-2",
            "control_type": "PROMPT_INJECTION",
            "expected_boundary": "注入不改变标准",
            "source_case_id": "CASE",
            "baseline_inputs": [_item("RUN-5")],
            "variant_inputs": [],
        },
    ]


def test_same_identity_reuses_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    judge = CountingJudge()
    run_controls(_controls(), judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 5
    assert len(checkpoint.read_text(encoding="utf-8").splitlines()) == 5
    run_controls(_controls(), judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 5, "相同输入和配置应全部复用"


def test_prompt_or_model_change_invalidates_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    judge = CountingJudge()
    run_controls(_controls(), judge, max_workers=2, checkpoint_path=checkpoint)
    judge.calls = 0
    changed_prompt = CountingJudge(prompt_version="prompt-b")
    run_controls(_controls(), changed_prompt, max_workers=2, checkpoint_path=checkpoint)
    assert changed_prompt.calls == 5
    changed_model = CountingJudge(model="model-b")
    run_controls(_controls(), changed_model, max_workers=2, checkpoint_path=checkpoint)
    assert changed_model.calls == 5


def test_input_change_invalidates_only_changed_item(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    judge = CountingJudge()
    run_controls(_controls(), judge, max_workers=2, checkpoint_path=checkpoint)
    changed = _controls()
    changed[0]["baseline_inputs"][0] = _item("RUN-1", request="换一种说法查询工资")
    judge.calls = 0
    run_controls(changed, judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 1, "只有输入内容变化的那一条需要重新执行"


def test_legacy_checkpoint_without_identity_is_not_reused(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text(json.dumps({"key": "old", "outcome": {"raw": {}}}) + "\n", encoding="utf-8")
    judge = CountingJudge()
    batch = run_controls(_controls(), judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 5
    assert len(batch.receipts) == 5


def test_partial_checkpoint_resumes_only_missing_items(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    judge = CountingJudge()
    run_controls(_controls(), judge, max_workers=1, checkpoint_path=checkpoint)
    lines = checkpoint.read_text(encoding="utf-8").splitlines()
    checkpoint.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
    judge.calls = 0
    batch = run_controls(_controls(), judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 2
    assert len(batch.receipts) == 5


def test_concurrent_output_order_is_stable(tmp_path: Path):
    batch = run_controls(_controls(), CountingJudge(), max_workers=4, checkpoint_path=tmp_path / "cp.jsonl")
    assert [row["control_id"] for row in batch.controls] == ["CTRL-1", "CTRL-2"]
    assert [item["index"] for item in batch.controls[0]["baseline"]] == [0, 1]
    assert [item["run_id"] if "run_id" in item else item["index"] for item in batch.controls[1]["baseline"]] == [0]
    restored = run_controls(_controls(), CountingJudge(), max_workers=1, checkpoint_path=tmp_path / "cp.jsonl")
    assert restored.controls[0]["baseline"][0]["assessment"]["call_linkage"]["judge_input_sha256"]
