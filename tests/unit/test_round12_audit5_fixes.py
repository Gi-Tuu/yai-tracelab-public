"""第五轮审计修复的回归测试（F5-01 ~ F5-08）。

覆盖：标注元素级 schema、holdout 冻结/报告校验、CSV 导入确定性、打包清单完整性、
evaluate 中断恢复、freeze 覆盖保护、快照哈希匹配、0-run 报告措辞。
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.builders import context, make_case, reference
from tracelab.contracts import Dataset
from tracelab.evaluation import (
    LabelFormatError,
    evaluate_dataset,
    load_reference_labels,
    validate_label_fields,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"


def _holdout_module():
    spec = importlib.util.spec_from_file_location("holdout_script_audit5", ROOT / "scripts" / "holdout.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


holdout = _holdout_module()


# ------------------------------------------------------------------ F5-01 标注 schema


def test_label_validation_rejects_malformed_root_causes(tmp_path):
    with pytest.raises(LabelFormatError) as info:
        validate_label_fields({
            "agent_task_success": True,
            "business_invoice_created": False,
            "root_causes": ["不是对象"],
        }, where="run[R1]")
    assert "root_causes[0]" in str(info.value)

    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps({
        "case_id": "C1",
        "run_assessments": [{
            "run_id": "R1", "agent_task_success": True,
            "business_invoice_created": False, "root_causes": ["坏数据"],
        }],
    }) + "\n", encoding="utf-8")
    with pytest.raises(LabelFormatError) as info:
        load_reference_labels(path)
    assert "root_causes[0]" in str(info.value)

    # 缺布尔字段也要在加载期拒绝，而不是在打分时 KeyError/TypeError
    with pytest.raises(LabelFormatError):
        validate_label_fields({"run_id": "R1"}, where="run[R1]")


def test_holdout_check_rejects_malformed_root_causes(tmp_path):
    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    frozen = tmp_path / "freeze.json"
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen), force=False)) == 0
    # 用模板得到全部 run 键
    template = tmp_path / "tpl.jsonl"
    assert holdout.cmd_template(Namespace(cases=str(cases), out=str(template))) == 0
    rows = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row.update({"annotator": "t", "annotator_independent": True,
                    "agent_task_success": True, "business_invoice_created": False})
    rows[0]["root_causes"] = ["坏数据"]
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    args = Namespace(cases=str(cases), labels=str(labels), freeze=str(frozen),
                     allow_self_annotation=False)
    assert holdout.cmd_check(args) == 3
    # report 直接调用也不能崩栈，而是拒绝（3）
    report_args = Namespace(cases=str(cases), labels=str(labels), freeze=str(frozen),
                            out=str(tmp_path / "report"), allow_self_annotation=False)
    assert holdout.cmd_report(report_args) == 3


# ------------------------------------------------------------------ F5-02 report 冻结校验


def test_report_refuses_tampered_data(tmp_path):
    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    frozen = tmp_path / "freeze.json"
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen), force=False)) == 0
    template = tmp_path / "tpl.jsonl"
    assert holdout.cmd_template(Namespace(cases=str(cases), out=str(template))) == 0
    rows = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row.update({"annotator": "t", "annotator_independent": True,
                    "agent_task_success": True, "business_invoice_created": False})
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    with cases.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    args = Namespace(cases=str(cases), labels=str(labels), freeze=str(frozen),
                     out=str(tmp_path / "out"), allow_self_annotation=False)
    assert holdout.cmd_report(args) == 2, "report 不能绕过冻结哈希校验"


# ------------------------------------------------------------------ F5-03 CSV 确定性


def test_import_csv_is_deterministic(tmp_path):
    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    csv_path = tmp_path / "labels.csv"
    assert holdout.cmd_csv(Namespace(cases=str(cases), out=str(csv_path), force=False)) == 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert "annotated_at" in (rows[0] if rows else {}), "CSV 必须包含可复现时间列"
    for row in rows:
        row["agent_task_success"] = "是"
        row["business_invoice_created"] = "否"
        row["annotated_at"] = "2026-11-05T00:11:23+08:00"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for target in (first, second):
        assert holdout.cmd_import_csv(Namespace(csv=str(csv_path), out=str(target),
                                                annotator="t", self_annotation=False)) == 0
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()
    assert json.loads(first.read_text(encoding="utf-8").splitlines()[0])["annotated_at"] == "2026-11-05T00:11:23+08:00"


# ------------------------------------------------------------------ F5-05 中断恢复


def test_evaluate_recovers_when_manifest_is_missing(tmp_path):
    from tracelab import cli

    out = tmp_path / "out"
    assert cli.main(["evaluate", "--cases", str(SAMPLE), "--out", str(out)]) == 0
    (out / ".tracelab-manifest.json").unlink()
    assert cli.main(["evaluate", "--cases", str(SAMPLE), "--out", str(out)]) == 0, \
        "marker 丢失后应能识别全部自家产物并自动恢复"


# ------------------------------------------------------------------ F5-06 freeze 保护


def test_freeze_refuses_to_silently_replace_baseline(tmp_path):
    cases = tmp_path / "traces.jsonl"
    shutil.copyfile(SAMPLE, cases)
    frozen = tmp_path / "freeze.json"
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen), force=False)) == 0
    before = frozen.read_bytes()
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen), force=False)) == 2
    assert frozen.read_bytes() == before
    with cases.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen), force=False)) == 2
    assert holdout.cmd_freeze(Namespace(cases=str(cases), out=str(frozen), force=True)) == 0


# ------------------------------------------------------------------ F5-07 快照哈希匹配


def test_packed_runs_requires_matching_dataset_hash():
    from tracelab.official_report import _packed_runs

    pack = ROOT / "regression" / "invoice-pack" / "regression-pack.json"
    if not pack.is_file():
        pytest.skip("公开镜像不含回归快照")
    created = json.loads(pack.read_text(encoding="utf-8"))["created_from"]
    assert _packed_runs(created["dataset_sha256"]), "哈希匹配时应返回快照 runs"
    assert _packed_runs("deadbeef" * 8) == set(), "数据哈希不匹配时不得声称已在快照中"
    assert _packed_runs(None) == set(), "缺省不校验哈希时也不得误用快照"


# ------------------------------------------------------------------ F5-08 0-run 措辞


def test_zero_run_case_is_not_described_as_runs_ne_two(tmp_path):
    case = make_case("ZERO-ONLY", context=context(), reference_result=reference(), runs=())
    dataset = Dataset(cases=[case], issues=[], source="t", sha256="",
                      stats={"lines_total": 1, "cases_parsed": 1, "skipped": 0, "truncated": 0})
    result = evaluate_dataset(dataset)
    from tracelab.official_report import build_official_report
    from tracelab.reports import build_markdown

    for text in (build_markdown(result), build_official_report(result)):
        for line in text.splitlines():
            if "运行数≠2" in line or "不可配对" in line:
                assert "ZERO-ONLY" not in line, "0-run Case 不能被写成 runs≠2"


def test_bundle_success_checklist_includes_zip_row(tmp_path):
    import importlib.util as _ilu
    import zipfile

    spec = _ilu.spec_from_file_location("bundle_audit5", ROOT / "scripts" / "build_submission_bundle.py")
    assert spec is not None and spec.loader is not None
    module = _ilu.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    out = tmp_path / "out"
    code = module.build_bundle(module.BundleOptions(
        root=ROOT, out_root=out, allow_outside=True, skip_tests=True, skip_checks=True,
    ))
    assert code == 0
    checklist = (out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "ZIP 回读安全扫描通过" in checklist, "成功包清单必须含 ZIP 回读检查行"
    archive = next(out.glob("*.zip"))
    inside = zipfile.ZipFile(archive).read("yai-tracelab/提交清单.md").decode("utf-8")
    assert inside.replace("\r\n", "\n") == checklist, "包内清单必须与展开目录清单一致"


def test_rule_registration_order_is_frozen():
    """规则拆分后 RULES 的注册顺序必须与拆分前逐项一致（R902 在 R015 之前）。"""
    from tracelab.rules import RULES

    assert [rule.rule_id for rule in RULES] == [
        "R001", "R002", "R003", "R004", "R005", "R006", "R007", "R008",
        "R009", "R010", "R011", "R012", "R013", "R014", "R902", "R015",
    ]
