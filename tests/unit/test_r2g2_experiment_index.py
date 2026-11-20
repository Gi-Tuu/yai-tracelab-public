"""R2-G2 G2-D：实验索引生成测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "final_round_experiment_index", ROOT / "scripts" / "final_round_experiment_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _row(case_id: str, run_id: str, verdict: str = "PASS"):
    return {
        "case_id": case_id,
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": verdict,
                "agent_task_success": True if verdict == "PASS" else None,
                "business_invoice_created": None,
                "first_fault_event_id": None,
                "evidence_event_ids": [],
                "reason": "r",
                "call_linkage": {
                    "model": "mock-model",
                    "prompt_version": "prompt-v1",
                    "execution_origin": "TEST",
                },
            }
        ],
    }


def test_build_index_counts_and_pairing():
    module = _load()
    results = [_row("C1", "R1"), _row("C2", "R2", "NO_RESULT")]
    labels = [_row("C1", "R1")]
    index = module.build_index("exp", results, labels, None, "synthetic")
    assert index["cases"] == 2
    assert index["runs"] == 2
    assert index["comparable"] == 1
    assert index["only_result"] == 1
    assert index["models"] == ["mock-model"]
    assert index["prompt_versions"] == ["prompt-v1"]
    assert index["no_result"] == 1


def test_index_requires_no_label_for_counts():
    module = _load()
    index = module.build_index("exp", [_row("C1", "R1")], None, None)
    assert index["comparable"] == 1
    assert "pairing" not in index


def test_cli_writes_index(tmp_path: Path):
    module = _load()
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(_row("C1", "R1"), ensure_ascii=False) + "\n", encoding="utf-8")
    old_argv = sys.argv
    try:
        sys.argv = [
            "final_round_experiment_index.py",
            "--name", "exp",
            "--results", str(results),
            "--out", str(tmp_path / "out"),
        ]
        assert module.main() == 0
    finally:
        sys.argv = old_argv
    assert (tmp_path / "out" / "experiment-index-exp.json").is_file()
