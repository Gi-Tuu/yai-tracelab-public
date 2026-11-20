"""R2-G1：报告脚本的覆盖测试（合成样本，不读取私有数据）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "final_round_report_script", ROOT / "scripts" / "final_round_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _row(case_id: str, run_id: str, verdict: str = "PASS", task=True):
    return {
        "case_id": case_id,
        "business_type": "ai_consultant",
        "run_assessments": [
            {
                "run_id": run_id,
                "verdict": verdict,
                "agent_task_success": task,
                "business_invoice_created": None,
                "first_fault_event_id": None,
                "evidence_event_ids": [],
                "reason": "r",
            }
        ],
    }


def test_report_compute_render_and_main(tmp_path: Path):
    module = _load_report_module()
    results = [_row("C1", "R1")]
    labels = {"C1": {"R1": _row("C1", "R1")["run_assessments"][0]}}
    metrics = module.compute(results, labels, {})
    text = module.render("合成报告", metrics)
    assert "合成报告" in text and "判定一致率" in text

    results_path = tmp_path / "results.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    out = tmp_path / "out"
    results_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    labels_path.write_text(json.dumps(_row("C1", "R1"), ensure_ascii=False) + "\n", encoding="utf-8")
    old_argv = sys.argv
    try:
        sys.argv = [
            "final_round_report.py",
            "--results", str(results_path),
            "--labels", str(labels_path),
            "--out", str(out),
            "--title", "合成报告",
        ]
        assert module.main() == 0
    finally:
        sys.argv = old_argv
    assert (out / "report.md").is_file()
    assert (out / "metrics.json").is_file()
