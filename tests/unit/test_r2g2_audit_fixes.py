"""R2-G2 审计修复回归：F1-F5。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tracelab.final_round import (
    ControlFormatError,
    FinalRoundFormatError,
    JudgeReceipt,
    ReceiptChain,
    ScriptedJudge,
    load_cases,
    load_controls,
    run_cases,
    run_controls,
)
from tracelab.final_round.receipts import receipt_from_call
from tracelab.final_round.results import build_case_result, validate_case_result


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


def _case(case_id: str = "CASE-A") -> dict:
    return {
        "dataset_version": "dv1",
        "data_origin": "synth",
        "case_id": case_id,
        "business_type": "ai_consultant",
        "domain": "salary",
        "data_purpose": "test",
        "near_duplicate_group": "g",
        "user_request": "u",
        "context": {"enterprise_id": "E1"},
        "reference_result": {
            "business_goal": "g",
            "pass_criteria": ["c"],
            "required_evidence": ["e"],
            "rule_source": {"x": 1},
            "reference_semantics": "business_goal_only",
        },
        "runs": [
            {
                "run_id": "RUN-A",
                "agent_version": "v1",
                "task_id": "T-A",
                "trace_id": "TR-A",
                "events": [_event("RUN-A")],
                "final_state": "DONE",
                "snapshot_state": "DONE",
                "workflow_terminal": True,
                "observation_complete": True,
            },
            {
                "run_id": "RUN-B",
                "agent_version": "v2",
                "task_id": "T-B",
                "trace_id": "TR-B",
                "events": [_event("RUN-B")],
                "final_state": "DONE",
                "snapshot_state": "DONE",
                "workflow_terminal": True,
                "observation_complete": True,
            },
        ],
    }


def _assessment(run_id: str, verdict: str = "PASS", **extra) -> dict:
    base = {
        "run_id": run_id,
        "agent_version": "v",
        "judge_id": "j",
        "judge_version": "jv",
        "verdict": verdict,
        "agent_task_success": True if verdict == "PASS" else False,
        "business_result_correct": True if verdict == "PASS" else False,
        "reason": "r",
        "dimensions": [{"name": "d", "conclusion": "PASS", "reason": "r", "evidence_event_ids": []}],
        "evidence_event_ids": [],
        "first_fault_event_id": None,
        "root_causes": [],
        "uncertainty": {"level": "low", "reason": "r"},
        "duration_ms": 1.0,
        "call_linkage": {
            "task_id": "t",
            "call_id": "c",
            "attempt_id": "a",
            "input_snapshot_id": "s",
            "judge_input_sha256": "h",
            "model": "m",
            "prompt_version": "p",
            "execution_origin": "TEST",
        },
    }
    base.update(extra)
    return base


class _MetaJudge:
    judge_id = "j"
    judge_version = "jv"
    model = "m"
    prompt_version = "p"
    execution_origin = "TEST"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate_with_meta(self, judge_input, linkage):  # type: ignore[no-untyped-def]
        self.calls += 1
        run_id = str(linkage.get("run_id"))
        time.sleep(0.02)
        meta = {
            "status": "valid",
            "response_model": f"resp-{run_id}",
            "token_usage": {"total_tokens": 1 if run_id == "RUN-A" else 2},
            "attempts": 1,
            "prompt_sha256": f"prompt-{run_id}",
        }
        return {
            "verdict": "PASS",
            "agent_task_success": True,
            "business_result_correct": True,
            "business_invoice_created": None,
            "reason": "ok",
        }, meta


def test_f1_each_receipt_gets_its_own_call_metadata():
    batch = run_cases([_case()], _MetaJudge(), max_workers=2)
    assert len(batch.receipts) == 2
    by_run = {r["run_id"]: r for r in batch.receipts}
    assert by_run["RUN-A"]["response_model"] == "resp-RUN-A"
    assert by_run["RUN-B"]["response_model"] == "resp-RUN-B"
    assert by_run["RUN-A"]["token_usage"]["total_tokens"] == 1
    assert by_run["RUN-B"]["token_usage"]["total_tokens"] == 2


def test_f1_controls_each_receipt_gets_its_own_metadata():
    control = _control()
    control["baseline_inputs"].append(
        {
            "judge_input": {
                "business_type": "ai_consultant",
                "domain": "salary",
                "user_request": "u2",
                "context": {},
                "business_objective": {},
                "observable_run": {"events": [_event("R2")]},
            },
            "linkage_only": {"run_id": "R2", "agent_version": "a"},
        }
    )
    batch = run_controls([control], _MetaJudge(), max_workers=2)
    by_run = {r["run_id"]: r for r in batch.receipts}
    assert by_run["R1"]["response_model"] == "resp-R1"
    assert by_run["R2"]["response_model"] == "resp-R2"


def test_f2_legacy_and_extended_receipts_verify():
    legacy = JudgeReceipt(
        sequence=0,
        call_id="c0",
        case_id="case",
        run_id="r0",
        business_type="ai_consultant",
        judge_id="j",
        judge_version="v",
        model="m",
        prompt_version="p",
        input_snapshot_id="s0",
        judge_input_sha256="h0",
        output_sha256="o0",
        verdict="PASS",
        attempt_id="a",
        execution_origin="TEST",
        duration_ms=1.0,
    ).seal("")
    intermediate = JudgeReceipt(
        sequence=0,
        call_id="c1",
        case_id="case",
        run_id="r1",
        business_type="ai_consultant",
        judge_id="j",
        judge_version="v",
        model="m",
        prompt_version="p",
        input_snapshot_id="s1",
        judge_input_sha256="h1",
        output_sha256="o1",
        verdict="PASS",
        attempt_id="a",
        execution_origin="TEST",
        duration_ms=1.0,
        response_model="m",
    ).seal("")
    new = receipt_from_call(
        sequence=0,
        call_id="c2",
        case_id="case",
        run_id="r2",
        business_type="ai_consultant",
        judge_id="j",
        judge_version="v",
        model="m",
        prompt_version="p",
        input_snapshot_id="s2",
        judge_input_sha256="h2",
        output={"verdict": "PASS"},
        verdict="PASS",
        attempt_id="a",
        execution_origin="TEST",
        duration_ms=1.0,
        response_model="m",
        token_usage={"total_tokens": 1},
    )
    new = ReceiptChain().append(new)
    for receipt in (legacy, intermediate, new):
        assert ReceiptChain([receipt]).verify() == []
    tampered = JudgeReceipt.from_dict({**new.to_dict(), "verdict": "FAIL"})
    assert ReceiptChain([tampered]).verify()


class _PromptJudge(_MetaJudge):
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    def prompt_digest(self, business_type):
        return self.digest

    def evaluate_with_meta(self, judge_input, linkage):
        return super().evaluate_with_meta(judge_input, linkage)


def _control():
    return {
        "control_id": "CTRL-1",
        "control_type": "EQUIVALENT",
        "expected_boundary": "b",
        "source_case_id": "C",
        "baseline_inputs": [
            {
                "judge_input": {
                    "business_type": "ai_consultant",
                    "domain": "salary",
                    "user_request": "u",
                    "context": {},
                    "business_objective": {},
                    "observable_run": {"events": [_event("R1")]},
                },
                "linkage_only": {"run_id": "R1", "agent_version": "a"},
            }
        ],
        "variant_inputs": [],
    }


def test_f3_prompt_content_change_invalidates_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "cp.jsonl"
    first = _PromptJudge("sha-a")
    run_controls([_control()], first, checkpoint_path=checkpoint)
    second = _PromptJudge("sha-b")
    run_controls([_control()], second, checkpoint_path=checkpoint)
    assert first.calls == 1
    assert second.calls == 1, "Prompt 内容变化必须重新执行"


def test_f4_duplicate_ids_are_rejected(tmp_path: Path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(json.dumps(_case("DUP"), ensure_ascii=False) for _ in range(2)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(FinalRoundFormatError):
        load_cases(path)
    batch = run_cases([_case("DUP"), _case("DUP")], ScriptedJudge())
    assert batch.results == []
    assert any("重复" in issue for issue in batch.issues)
    controls_path = tmp_path / "controls.jsonl"
    controls_path.write_text(
        "\n".join(json.dumps(_control(), ensure_ascii=False) for _ in range(2)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ControlFormatError):
        load_controls(controls_path)


def test_f5_root_cause_first_fault_must_exist():
    case = _case()
    bad = _assessment("RUN-A", root_causes=[{"label": "AMOUNT_BASIS", "first_fault_event_id": "MISSING", "evidence_event_ids": [], "explanation": "x"}])
    result = build_case_result(
        case,
        [bad, _assessment("RUN-B")],
        annotation_version="a",
        rubric_version="r",
        config_version="c",
    )
    issues = validate_case_result(result, case)
    assert any("root_causes[0].first_fault_event_id 不存在" in issue for issue in issues)


def test_f6_normal_batch_checkpoint_resumes_and_skips_completed_runs(tmp_path: Path):
    checkpoint = tmp_path / "case-checkpoint.jsonl"
    judge = _MetaJudge()
    first = run_cases([_case()], judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 2
    assert len(first.receipts) == 2
    second = run_cases([_case()], judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 2, "相同输入/模型/Prompt/规则应全部复用"
    assert len(second.receipts) == 2

    lines = checkpoint.read_text(encoding="utf-8").splitlines()
    checkpoint.write_text("\n".join(lines[:1]) + "\n", encoding="utf-8")
    judge.calls = 0
    resumed = run_cases([_case()], judge, max_workers=2, checkpoint_path=checkpoint)
    assert judge.calls == 1, "只补未完成 run"
    assert len(resumed.receipts) == 2


def test_f6_changed_prompt_or_model_invalidates_case_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "case-checkpoint.jsonl"
    first = _MetaJudge()
    run_cases([_case()], first, checkpoint_path=checkpoint)
    changed = _MetaJudge()
    changed.prompt_version = "p2"
    run_cases([_case()], changed, checkpoint_path=checkpoint)
    assert changed.calls == 2


def test_f7_prompt_digest_is_prompt_only_and_matches_freeze_basis():
    from tracelab.final_round.judge_input import sha256_json
    from tracelab.final_round.llm_judge import OpenAICompatJudge

    judge = OpenAICompatJudge()
    invoice = judge.prompt_digest("invoice")
    consultant = judge.prompt_digest("ai_consultant")
    assert invoice == "3e06d73234b81dd4a5000a4a023a6e61e93097b1b9f607702edd6b6680f23fe7"
    assert consultant == "a127271f9a2cf99844f31f23eaa8f95d36a5d755601af4d14d5592e8a4ea6b9f"
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "different input"}]
    assert invoice != sha256_json(messages)


def test_f8_unknown_verdict_not_counted_effective():
    from tracelab.final_round.metrics import compute_metrics

    pairs = [{"case_id": "C", "run_id": "R", "result": {"verdict": "UNKNOWN"}, "label": {"verdict": "FAIL"}}]
    metrics = compute_metrics(pairs)
    assert metrics["runs"] == 1
    assert metrics["effective_judgments"] == 0
    assert metrics["invalid_verdicts"] == 1


def test_f9_experiment_index_completed_excludes_no_result(tmp_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "final_round_experiment_index_audit", Path(__file__).resolve().parents[2] / "scripts" / "final_round_experiment_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    index = module.build_index(
        "x",
        [{"case_id": "C", "run_assessments": [{"run_id": "R", "verdict": "NO_RESULT"}]}],
        None,
        None,
    )
    assert index["attempted_runs"] == 1
    assert index["completed_runs"] == 0
    assert index["effective_judgments"] == 0


def test_f10_console_refuses_non_loopback_host():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "final_round_console_audit", Path(__file__).resolve().parents[2] / "scripts" / "final_round_console.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module._is_loopback_host("127.0.0.1")
    assert module._is_loopback_host("localhost")
    assert module._is_loopback_host("::1")
    assert not module._is_loopback_host("0.0.0.0")
    assert not module._is_loopback_host("192.168.1.10")


def test_receipt_metadata_status_marks_historical_records():
    legacy = JudgeReceipt(
        sequence=0,
        call_id="c0",
        case_id="case",
        run_id="r0",
        business_type="ai_consultant",
        judge_id="j",
        judge_version="v",
        model="m",
        prompt_version="p",
        input_snapshot_id="s0",
        judge_input_sha256="h0",
        output_sha256="o0",
        verdict="PASS",
        attempt_id="a",
        execution_origin="TEST",
        duration_ms=1.0,
    )
    assert legacy.metadata_status == "legacy_v0_unreliable"
    assert not legacy.metadata_reliable
    intermediate = JudgeReceipt.from_dict(
        {
            **legacy.seal("").to_dict(),
            "response_model": "m",
            "receipt_hash": "",
        }
    ).seal("")
    assert intermediate.metadata_status == "partial_v1_extended"
    new = JudgeReceipt.from_dict(
        {
            **legacy.to_dict(),
            "schema_version": "2",
            "prompt_sha256": "p",
            "messages_sha256": "m",
            "receipt_hash": "",
        }
    ).seal("")
    assert new.metadata_status == "reliable_v2"
    assert new.metadata_reliable


def test_corrupted_case_checkpoint_is_ignored(tmp_path: Path):
    checkpoint = tmp_path / "case-checkpoint.jsonl"
    judge = _MetaJudge()
    run_cases([_case()], judge, checkpoint_path=checkpoint)
    rows = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    rows[0]["outcome"].pop("assessment")
    checkpoint.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    judge.calls = 0
    run_cases([_case()], judge, checkpoint_path=checkpoint)
    assert judge.calls >= 1, "损坏断点不能被复用"


def test_live_judge_without_prompt_digest_disables_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "case-checkpoint.jsonl"
    judge = _MetaJudge()
    judge.execution_origin = "LIVE_MODEL"
    batch = run_cases([_case()], judge, checkpoint_path=checkpoint)
    assert judge.calls == 2
    assert not checkpoint.exists()
    assert any("prompt_digest" in warning for warning in batch.warnings)


def test_f10_console_page_asset_renders_without_placeholder():
    import importlib.util

    page = Path(__file__).resolve().parents[2] / "scripts" / "final_round_console_page.html"
    assert page.is_file()
    spec = importlib.util.spec_from_file_location(
        "final_round_console_asset", Path(__file__).resolve().parents[2] / "scripts" / "final_round_console.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    html = module._render_page()
    assert "YAI TraceLab" in html
    assert "__DATASETS__" not in html

    original = dict(module.DATASETS)
    module.DATASETS["__audit__"] = {"name": "</script><script>alert(1)</script>"}
    try:
        unsafe = module._render_page()
        assert "<\\/script><script>alert(1)" in unsafe.replace("</script><script>", "<\\/script><script>")
    finally:
        module.DATASETS.clear()
        module.DATASETS.update(original)


def test_experiment_index_marks_historical_receipt_metadata():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "final_round_experiment_index_meta", Path(__file__).resolve().parents[2] / "scripts" / "final_round_experiment_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    legacy = JudgeReceipt(
        sequence=0,
        call_id="c",
        case_id="case",
        run_id="r",
        business_type="ai_consultant",
        judge_id="j",
        judge_version="v",
        model="m",
        prompt_version="p",
        input_snapshot_id="s",
        judge_input_sha256="h",
        output_sha256="o",
        verdict="PASS",
        attempt_id="a",
        execution_origin="TEST",
        duration_ms=1.0,
    )
    index = module.build_index("x", [], None, None, receipts=[legacy.to_dict()])
    assert index["historical_metadata_unreliable"] is True
    assert index["receipt_metadata"]["legacy_v0_unreliable"] == 1


def test_corrupted_nested_case_checkpoint_is_ignored(tmp_path: Path):
    checkpoint = tmp_path / "case-checkpoint.jsonl"
    judge = _MetaJudge()
    run_cases([_case()], judge, checkpoint_path=checkpoint)
    rows = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    rows[0]["outcome"]["assessment"].pop("run_id")
    checkpoint.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    judge.calls = 0
    run_cases([_case()], judge, checkpoint_path=checkpoint)
    assert judge.calls >= 1, "嵌套字段损坏的断点不能复用"

