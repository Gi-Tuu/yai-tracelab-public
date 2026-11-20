"""复赛核心（Judge 输入隔离/收据链/扩展结果）的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelab.final_round import (
    ControlFormatError,
    FinalRoundFormatError,
    ReceiptChain,
    ScriptedJudge,
    build_judge_input,
    load_cases,
    load_controls,
    run_cases,
    run_controls,
    validate_case,
    validate_case_result,
    write_batch,
)
from tracelab.final_round.receipts import receipt_from_call


def _event(event_id: str, seq: int, step: str, action: str, *, output=None) -> dict:
    return {
        "event_id": event_id,
        "seq": seq,
        "step_id": step,
        "module": "test",
        "action": action,
        "step_status": "SUCCEEDED",
        "observed_input": {"query": "x"},
        "observed_output": output or {},
        "tool_calls": [],
    }


def _run(run_id: str, case_id: str, *, invoice_created: bool | None = None) -> dict:
    run = {
        "run_id": run_id,
        "agent_version": "synthetic-agent-A",
        "task_id": f"TASK-{run_id}",
        "trace_id": f"TRACE-{run_id}",
        "events": [
            _event(f"{run_id}-E01", 1, "INPUT", "input"),
            _event(f"{run_id}-E02", 2, "TOOL_CALL", "query", output={"value": 1}),
            _event(f"{run_id}-E03", 3, "AGENT_REPLY", "reply", output={"reply": "ok"}),
        ],
        "final_state": "NO_ACTION",
        "snapshot_state": "NO_ACTION",
        "workflow_terminal": False,
        "observation_complete": True,
    }
    if invoice_created is not None:
        run["invoice_created"] = invoice_created
    return run


def _case(case_id: str, business_type: str, domain: str) -> dict:
    return {
        "dataset_version": "retest-test-v1",
        "data_origin": "synthetic_scenario",
        "case_id": case_id,
        "business_type": business_type,
        "domain": domain,
        "scenario": "test",
        "data_purpose": "dev",
        "near_duplicate_group": f"group-{case_id}",
        "variant": "standard",
        "user_request": "测试请求",
        "context": {
            "enterprise_id": "SIM-1",
            "accounting_period": "2026-10",
            "current_date": "2026-11-16",
            "facts": {"x": 1},
            "input_materials": [],
            "scope": "test",
        },
        "reference_result": {
            "business_goal": "测试目标",
            "pass_criteria": ["目标满足"],
            "required_evidence": ["证据"],
            "rule_source": {"source": "test"},
            "reference_semantics": "测试语义",
        },
        "runs": [
            _run(f"{case_id}-A", case_id, invoice_created=False if business_type == "invoice" else None),
            _run(f"{case_id}-B", case_id, invoice_created=True if business_type == "invoice" else None),
        ],
    }


def _write_cases(tmp_path: Path, cases: list[dict]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    return path


def test_load_cases_and_structure():
    case = _case("C1", "invoice", "invoice")
    assert validate_case(case) == []
    bad = json.loads(json.dumps(case))
    bad["runs"] = bad["runs"][:1]
    assert any("恰好 2 条" in item for item in validate_case(bad))


def test_load_rejects_bad_events(tmp_path):
    case = _case("C1", "ai_consultant", "salary")
    case["runs"][0]["events"][1]["seq"] = 1  # 不再严格递增
    path = _write_cases(tmp_path, [case])
    with pytest.raises(FinalRoundFormatError) as info:
        load_cases(path)
    assert "严格递增" in str(info.value)

    case2 = _case("C1", "ai_consultant", "salary")
    case2["runs"][0]["events"][1]["event_id"] = case2["runs"][0]["events"][0]["event_id"]
    with pytest.raises(FinalRoundFormatError) as info:
        load_cases(_write_cases(tmp_path / "b", [case2]))
    assert "event_id 重复" in str(info.value)


def test_judge_input_excludes_answers_and_is_deterministic():
    case = _case("C1", "invoice", "invoice")
    run = case["runs"][0]
    payload = build_judge_input(case, run, run_index=0)
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("run_assessments", "agent_task_success", "first_fault_event_id", "invoice_created"):
        assert forbidden not in blob
    assert payload["judge_input"]["business_type"] == "invoice"
    assert payload["linkage_only"]["task_id"] == run["task_id"]
    again = build_judge_input(case, run, run_index=0)
    assert payload["judge_input_sha256"] == again["judge_input_sha256"]
    assert payload["input_snapshot_id"].startswith("js-")


def test_receipt_chain_detects_tamper():
    chain = ReceiptChain()
    for index in range(2):
        chain.append(
            receipt_from_call(
                sequence=index,
                call_id=f"call-{index}",
                case_id="C1",
                run_id=f"r{index}",
                business_type="invoice",
                judge_id="judge",
                judge_version="1",
                model="m",
                prompt_version="p1",
                input_snapshot_id="js-x",
                judge_input_sha256="a" * 64,
                output={"verdict": "REJECT"},
                verdict="REJECT",
                attempt_id="a1",
                execution_origin="SCRIPTED_TEST_NOT_EXECUTED",
                duration_ms=1.0,
            )
        )
    assert chain.verify() == []
    records = chain.to_dicts()
    records[0]["verdict"] = "PASS"
    assert ReceiptChain([receipt_from_dict(item) for item in records]).verify()


def receipt_from_dict(item: dict):
    from tracelab.final_round.receipts import JudgeReceipt

    return JudgeReceipt.from_dict(item)


def test_scripted_batch_is_valid_and_never_pass(tmp_path):
    cases = [_case("C1", "invoice", "invoice"), _case("C2", "ai_consultant", "salary")]
    batch = run_cases(cases, ScriptedJudge())
    assert batch.issues == []
    assert len(batch.results) == 2
    assert all(result["run_assessments"][0]["verdict"] == "REJECT" for result in batch.results)
    assert ReceiptChain([receipt_from_dict(item) for item in batch.receipts]).verify() == []

    out = tmp_path / "out"
    write_batch(out, batch)
    assert (out / "results.jsonl").is_file()
    assert (out / "receipts.json").is_file()
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["executed"] is False


def test_result_validation_catches_null_semantics_and_bad_evidence():
    case = _case("C1", "invoice", "invoice")
    batch = run_cases([case], ScriptedJudge())
    result = batch.results[0]
    assert validate_case_result(result, case) == []

    tampered = json.loads(json.dumps(result))
    tampered["run_assessments"][0]["agent_task_success"] = True
    assert any("必须为 null" in item for item in validate_case_result(tampered, case))

    tampered2 = json.loads(json.dumps(result))
    tampered2["run_assessments"][0]["evidence_event_ids"] = ["NOT-EXIST"]
    assert any("不存在的事件" in item for item in validate_case_result(tampered2, case))


def test_judge_exception_becomes_no_result():
    class BrokenJudge(ScriptedJudge):
        def evaluate(self, judge_input, linkage):
            raise TimeoutError("simulated timeout")

    case = _case("C1", "ai_consultant", "salary")
    batch = run_cases([case], BrokenJudge())
    assert batch.results[0]["run_assessments"][0]["verdict"] == "NO_RESULT"
    assert batch.results[0]["run_assessments"][0]["agent_task_success"] is None
    assert "TimeoutError" in batch.results[0]["run_assessments"][0]["reason"]
    assert ReceiptChain([receipt_from_dict(item) for item in batch.receipts]).verify() == []


def test_concurrent_batch_keeps_order_and_chain():
    cases = [_case(f"CC{i}", "ai_consultant", "salary") for i in range(4)]
    batch = run_cases(cases, ScriptedJudge(), max_workers=4)
    assert batch.issues == []
    assert [result["case_id"] for result in batch.results] == [f"CC{i}" for i in range(4)]
    assert ReceiptChain([receipt_from_dict(item) for item in batch.receipts]).verify() == []


def _control_row(control_id: str = "CTRL-1") -> dict:
    def item(run_id: str) -> dict:
        judge_input = {
            "business_type": "ai_consultant",
            "domain": "salary",
            "user_request": "测试",
            "context": {},
            "business_objective": {"business_goal": "测试"},
            "observable_run": {"events": [
                {"event_id": f"{run_id}-E01", "seq": 1, "step_id": "INPUT",
                 "step_status": "SUCCEEDED", "observed_input": {}, "observed_output": {}, "tool_calls": []}
            ]},
        }
        return {
            "judge_input": judge_input,
            "linkage_only": {"dataset_version": "t", "data_purpose": "dev", "case_id": "C1",
                             "run_id": run_id, "trace_id": f"T-{run_id}", "agent_version": "a"},
        }
    return {
        "control_id": control_id,
        "control_type": "EQUIVALENT",
        "expected_boundary": "表达变化不单独造成判错",
        "source_case_id": "C1",
        "paired_case_id": "C2",
        "baseline_inputs": [item("R1"), item("R2")],
        "variant_inputs": [item("R3"), item("R4")],
    }


def test_controls_runner_and_validation(tmp_path):
    path = tmp_path / "controls.jsonl"
    path.write_text(json.dumps(_control_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    controls = load_controls(path)
    assert len(controls) == 1
    batch = run_controls(controls, ScriptedJudge(), max_workers=2)
    assert batch.issues == []
    assert len(batch.controls) == 1
    assert len(batch.receipts) == 4
    assert batch.controls[0]["consistent"] is True
    out = tmp_path / "out"
    from tracelab.final_round import write_control_batch
    write_control_batch(out, batch)
    assert (out / "controls.jsonl").is_file()
    assert (out / "manifest.json").is_file()

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"control_id": "x"}, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ControlFormatError):
        load_controls(bad)


def test_controls_resume_skips_completed_inputs(tmp_path):
    path = tmp_path / "controls.jsonl"
    path.write_text(json.dumps(_control_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    controls = load_controls(path)
    calls = {"n": 0}

    class CountingJudge(ScriptedJudge):
        def evaluate(self, judge_input, linkage):
            calls["n"] += 1
            return super().evaluate(judge_input, linkage)

    checkpoint = tmp_path / "checkpoint.jsonl"
    first = run_controls(controls, CountingJudge(), max_workers=2, checkpoint_path=checkpoint)
    assert calls["n"] == 4
    assert len(first.controls[0]["baseline"]) + len(first.controls[0]["variant"]) == 4
    second = run_controls(controls, CountingJudge(), max_workers=2, checkpoint_path=checkpoint)
    assert calls["n"] == 4, "第二次应完全从断点恢复，不再调用"
    assert [item["verdict"] for item in second.controls[0]["baseline"]] == [
        item["verdict"] for item in first.controls[0]["baseline"]
    ]
