"""收据兼容 verifier 测试（v0 / v1-ext / v2）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tracelab.final_round.receipts import JudgeReceipt, ReceiptChain, receipt_from_call

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "verify_receipts", ROOT / "scripts" / "verify_receipts.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _legacy() -> JudgeReceipt:
    return JudgeReceipt(
        sequence=0,
        call_id="c0",
        case_id="case",
        run_id="r0",
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
    ).seal("")


def _new() -> JudgeReceipt:
    return ReceiptChain().append(
        receipt_from_call(
            sequence=0,
            call_id="c2",
            case_id="case",
            run_id="r2",
            business_type="ai_consultant",
            judge_id="j",
            judge_version="v",
            model="m",
            prompt_version="p",
            input_snapshot_id="s",
            judge_input_sha256="h",
            output={"verdict": "PASS"},
            verdict="PASS",
            attempt_id="a",
            execution_origin="TEST",
            duration_ms=1.0,
            response_model="m",
            prompt_sha256="p",
            messages_sha256="m",
        )
    )


def test_verify_legacy_and_v2_chains():
    module = _load()
    report = module.build_report([_legacy().to_dict()])
    assert report["valid"] is True
    assert report["historical_metadata_unreliable"] is True
    assert report["metadata_status"]["legacy_v0_unreliable"] == 1
    report2 = module.build_report([_new().to_dict()])
    assert report2["valid"] is True
    assert report2["historical_metadata_unreliable"] is False
    assert report2["metadata_status"]["reliable_v2"] == 1


def test_verify_detects_tamper():
    module = _load()
    data = _new().to_dict()
    data["verdict"] = "FAIL"
    report = module.build_report([data])
    assert report["valid"] is False
    assert report["problems"]


def test_cli_writes_status_file(tmp_path: Path):
    module = _load()
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps([_legacy().to_dict()]), encoding="utf-8")
    old_argv = __import__("sys").argv
    try:
        __import__("sys").argv = ["verify_receipts.py", "--receipts", str(path), "--write-status"]
        assert module.main() == 0
    finally:
        __import__("sys").argv = old_argv
    status = json.loads((tmp_path / "metadata-status.json").read_text(encoding="utf-8"))
    assert status["historical_metadata_unreliable"] is True
