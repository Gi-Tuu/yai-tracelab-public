"""holdout 冻结 / 模板 / 校验 / 报告脚本的回归测试（审计「问题是否成立」脚手架）。

这些测试只验证**流程约束**（哈希、完整性、标注者身份、Wilson CI），
不产生、也不伪造任何 holdout 标注结果。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"

spec = importlib.util.spec_from_file_location("holdout_script", ROOT / "scripts" / "holdout.py")
assert spec is not None and spec.loader is not None
holdout = importlib.util.module_from_spec(spec)
sys.modules["holdout_script"] = holdout
spec.loader.exec_module(holdout)


def test_freeze_records_hash_and_run_keys(tmp_path):
    frozen = tmp_path / "freeze.json"
    code = holdout.cmd_freeze(Namespace(cases=str(SAMPLE), out=str(frozen)))
    assert code == 0
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    assert payload["source"]["sha256"] == holdout._sha256(SAMPLE)
    assert payload["dataset"]["runs"] == len(payload["dataset"]["run_keys"]) > 0
    assert payload["evaluator_version"] and payload["rulepack_version"]


def test_freeze_can_be_used_by_check_and_rejects_tampering(tmp_path):
    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    frozen = tmp_path / "freeze.json"
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen))) == 0

    template = tmp_path / "labels.template.jsonl"
    assert holdout.cmd_template(Namespace(cases=str(cases), out=str(template))) == 0
    rows = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row["annotator"] = "second-annotator"
        row["annotator_independent"] = True
        row["agent_task_success"] = True
        row["business_invoice_created"] = True
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    args = Namespace(cases=str(cases), labels=str(labels), freeze=str(frozen),
                     allow_self_annotation=False)
    assert holdout.cmd_check(args) == 0

    # 标注不完整 → 3
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows[:-1]) + "\n", encoding="utf-8")
    assert holdout.cmd_check(args) == 3

    # 标注者身份不合规 → 3
    for row in rows:
        row["annotator_independent"] = False
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    assert holdout.cmd_check(args) == 3

    # 数据被改动 → 哈希不符，退出 2
    cases.write_text(cases.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    for row in rows:
        row["annotator_independent"] = True
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    assert holdout.cmd_check(args) == 2


def test_report_emits_preregistered_metrics_and_wilson(tmp_path):
    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    frozen = tmp_path / "freeze.json"
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen))) == 0
    template = tmp_path / "labels.template.jsonl"
    assert holdout.cmd_template(Namespace(cases=str(cases), out=str(template))) == 0
    rows = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row.update({"annotator": "second-annotator", "annotator_independent": True,
                    "agent_task_success": True, "business_invoice_created": True})
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    out = tmp_path / "report"
    code = holdout.cmd_report(Namespace(cases=str(cases), labels=str(labels),
                                        freeze=str(frozen), out=str(out),
                                        allow_self_annotation=False))
    assert code == 0
    text = (out / "holdout-report.md").read_text(encoding="utf-8")
    assert "Wilson 95% CI" in text
    assert "不能把这里的任何比例当作隐藏测试集精度" in text
    payload = json.loads((out / "holdout-report.json").read_text(encoding="utf-8"))
    assert payload["data_sha256"] == holdout._sha256(cases)
    assert payload["labels_sha256"] == holdout._sha256(labels)


def test_wilson_bounds_are_sane():
    assert holdout.wilson(0, 0) is None
    low, high = holdout.wilson(5, 8)
    assert 0.0 <= low <= 5 / 8 <= high <= 1.0
    low, high = holdout.wilson(16, 16)
    assert low < 1.0 and high == pytest.approx(1.0)


def test_show_prints_raw_traces(capsys):
    code = holdout.cmd_show(Namespace(cases=str(SAMPLE), case="", run=""))
    assert code == 0
    output = capsys.readouterr().out
    assert "=== Case" in output and "--- Run" in output
    assert "final_state=" in output
    # 不应包含评测器结论字段（盲标要求）
    assert "verdict" not in output.lower()


def test_worksheet_contains_all_runs_without_verdict(tmp_path):
    out = tmp_path / "worksheet.md"
    code = holdout.cmd_worksheet(Namespace(cases=str(SAMPLE), out=str(out)))
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "agent_task_success" in text and "business_invoice_created" in text
    assert "verdict" not in text.lower(), "worksheet 必须是盲标材料，不得含评测器结论"
    dataset = holdout.load_dataset(SAMPLE)
    expected_runs = sum(len(case.runs) for case in dataset.cases)
    assert text.count("### Run `") == expected_runs > 0


def test_csv_export_and_import_roundtrip(tmp_path):
    import csv

    csv_path = tmp_path / "labels.csv"
    assert holdout.cmd_csv(Namespace(cases=str(SAMPLE), out=str(csv_path))) == 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["agent_task_success"] = "是"
        row["business_invoice_created"] = "否"
        row["root_causes"] = ""
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    labels = tmp_path / "labels.jsonl"
    code = holdout.cmd_import_csv(Namespace(csv=str(csv_path), out=str(labels),
                                            annotator="tester", self_annotation=False))
    assert code == 0
    data = [json.loads(line) for line in labels.read_text(encoding="utf-8").splitlines() if line]
    assert len(data) == len(rows)
    assert all(row["agent_task_success"] is True and row["business_invoice_created"] is False
               for row in data)
    assert all(row["annotator_independent"] is True for row in data)

    # 布尔列填错 → 退出 3，且不生成半成品
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        rows[0]["agent_task_success"] = "大概吧"
        writer.writerows(rows)
    assert holdout.cmd_import_csv(Namespace(csv=str(csv_path), out=str(labels),
                                            annotator="tester", self_annotation=False)) == 3


def test_finish_pipeline_end_to_end(tmp_path):
    import csv

    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    frozen = tmp_path / "freeze.json"
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen))) == 0
    csv_path = tmp_path / "labels.csv"
    assert holdout.cmd_csv(Namespace(cases=str(cases), out=str(csv_path))) == 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["agent_task_success"] = "是"
        row["business_invoice_created"] = "否"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    out = tmp_path / "report"
    code = holdout.cmd_finish(Namespace(
        cases=str(cases), csv=str(csv_path), labels=str(tmp_path / "labels.jsonl"),
        freeze=str(frozen), out=str(out), annotator="tester", allow_self_annotation=False,
    ))
    assert code == 0
    text = (out / "holdout-report.md").read_text(encoding="utf-8")
    assert "Wilson 95% CI" in text


def test_csv_refuses_to_overwrite_filled_file(tmp_path):
    import csv

    csv_path = tmp_path / "labels.csv"
    args = Namespace(cases=str(SAMPLE), out=str(csv_path), force=False)
    assert holdout.cmd_csv(args) == 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["agent_task_success"] = "是"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    # 已填写 → 拒绝覆盖（返回 2，文件内容不变）
    assert holdout.cmd_csv(args) == 2
    assert "是" in csv_path.read_text(encoding="utf-8-sig")
    # --force 才允许重置
    assert holdout.cmd_csv(Namespace(cases=str(SAMPLE), out=str(csv_path), force=True)) == 0


def test_guide_contains_all_runs_and_rules(tmp_path):
    out = tmp_path / "guide.html"
    assert holdout.cmd_guide(Namespace(cases=str(SAMPLE), out=str(out))) == 0
    text = out.read_text(encoding="utf-8")
    assert "reference_result" in text and "agent_task_success" in text
    assert "verdict" not in text.lower(), "盲标指南不得含评测器结论"
    dataset = holdout.load_dataset(SAMPLE)
    expected_runs = sum(len(case.runs) for case in dataset.cases)
    assert text.count("<h3>Run") == expected_runs > 0
