"""第七轮审查（外部报告 f3b049c）修复回归：F7-01 ~ F7-06。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.adapters import load_dataset
from tracelab.contracts import Dataset
from tracelab.evaluation import (
    LabelFormatError,
    evaluate_dataset,
    load_reference_labels,
    score_against_labels,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"


def _case(case_id: str, runs: int = 2):
    run_objs = tuple(
        make_run(f"{case_id}-{index}", basic_flow(prefix=f"{case_id}-{index}"),
                 final_state="SUCCEEDED", invoice_created=True, agent_version=f"v{index}")
        for index in range(runs)
    )
    return make_case(case_id, context=context(), reference_result=reference(), runs=run_objs)


def _dataset(cases) -> Dataset:
    return Dataset(cases=list(cases), issues=[], source="t", sha256="",
                   stats={"lines_total": len(cases), "cases_parsed": len(cases),
                          "skipped": 0, "truncated": 0})


# ---------------------------------------------------------------- F7-01


def test_duplicate_run_id_within_case_is_rejected(tmp_path):
    path = tmp_path / "labels.jsonl"
    row = {
        "case_id": "C1",
        "run_assessments": [
            {"run_id": "r", "agent_task_success": True,
             "business_invoice_created": False, "root_causes": []},
            {"run_id": "r", "agent_task_success": False,
             "business_invoice_created": False, "root_causes": []},
        ],
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(LabelFormatError) as info:
        load_reference_labels(path)
    assert "重复" in str(info.value)

    # 跨 case 同名 run_id 合法（金标按 (case_id, run_id) 唯一）
    clean = json.loads(json.dumps(row))
    clean["run_assessments"] = clean["run_assessments"][:1]
    row2 = json.loads(json.dumps(clean))
    row2["case_id"] = "C2"
    path.write_text(json.dumps(clean, ensure_ascii=False) + "\n"
                    + json.dumps(row2, ensure_ascii=False) + "\n", encoding="utf-8")
    loaded = load_reference_labels(path)
    assert set(loaded) == {"C1", "C2"}


# ---------------------------------------------------------------- F7-02


def test_partial_labels_expose_bidirectional_coverage():
    c1, c2 = _case("C1"), _case("C2")
    result = evaluate_dataset(_dataset([c1, c2]))
    labels = {
        "C1": {
            "case_id": "C1",
            "run_assessments": [
                {"run_id": a.run_id, "agent_task_success": True,
                 "business_invoice_created": True, "root_causes": []}
                for a in result.results[0].assessments
            ],
        }
    }
    values = score_against_labels(result, labels).values
    assert values["runs_evaluated"] == 4
    assert values["runs_labeled"] == 2
    assert values["runs_unlabeled"] == 2
    assert values["label_run_coverage"] == 0.5
    assert values["labels_unmatched"] == 0, "单向字段语义保持不变"


def test_full_labels_have_zero_unlabeled_and_full_coverage():
    case = _case("FULL")
    result = evaluate_dataset(_dataset([case]))
    labels = {
        "FULL": {
            "case_id": "FULL",
            "run_assessments": [
                {"run_id": a.run_id, "agent_task_success": a.agent_task_success,
                 "business_invoice_created": a.business_invoice_created, "root_causes": []}
                for a in result.results[0].assessments
            ],
        }
    }
    values = score_against_labels(result, labels).values
    assert values["runs_unlabeled"] == 0
    assert values["label_run_coverage"] == 1.0


def test_ci_verify_gate_requires_bidirectional_coverage(tmp_path):
    from scripts.ci_verify import EXPECTED_METRICS, check_metrics

    directory = tmp_path / "metrics"
    directory.mkdir()
    values = {key: 1.0 for key in EXPECTED_METRICS}
    values.update({"runs_unlabeled": 5, "runs_evaluated": 28, "runs_labeled": 23})
    (directory / "metrics.json").write_text(
        json.dumps({"values": values}, ensure_ascii=False), encoding="utf-8"
    )
    assert check_metrics(directory) == 1
    values["runs_unlabeled"] = 0
    (directory / "metrics.json").write_text(
        json.dumps({"values": values}, ensure_ascii=False), encoding="utf-8"
    )
    assert check_metrics(directory) == 0


# ---------------------------------------------------------------- F7-03


def _holdout_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("holdout_audit7", ROOT / "scripts" / "holdout.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_freeze_guard_covers_code_and_rule_snapshot(tmp_path, monkeypatch):
    holdout = _holdout_module()
    cases = tmp_path / "cases.jsonl"
    shutil.copyfile(SAMPLE, cases)
    freeze = tmp_path / "freeze.json"

    def write(code_sha: str | None) -> None:
        source = {"file": cases.name, "sha256": holdout._sha256(cases)}
        if code_sha is not None:
            source["code_sha256"] = code_sha
        freeze.write_text(json.dumps({"source": source}), encoding="utf-8")

    # 代码摘要不一致 → 拒绝（数据哈希正确也不行）
    monkeypatch.setattr(holdout, "_code_snapshot_sha", lambda: "current-code")
    write("frozen-old-code")
    error, _ = holdout._freeze_guard(cases, freeze)
    assert error and "代码" in error

    # 一致 → 通过
    write("current-code")
    error, warnings = holdout._freeze_guard(cases, freeze)
    assert error is None and not warnings

    # legacy（无代码摘要）→ 仅警告，保留历史结果
    write(None)
    error, warnings = holdout._freeze_guard(cases, freeze)
    assert error is None and any("legacy" in w for w in warnings)


# ---------------------------------------------------------------- F7-04


def test_malformed_metadata_does_not_crash_loader(tmp_path):
    case = json.loads(SAMPLE.read_text(encoding="utf-8").splitlines()[0])
    case["dataset_version"] = {"bad": "object"}
    path = tmp_path / "bad-meta.jsonl"
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")

    dataset = load_dataset(path)
    assert len(dataset.cases) == 1
    assert dataset.schema_valid is False
    assert any(issue.code == "BAD_FIELD_TYPE" for issue in dataset.issues)


# ---------------------------------------------------------------- F7-05


def test_independent_eval_status_is_current():
    text = (ROOT / "docs" / "independent-eval.md").read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:8])
    assert "未完成" not in head
    assert "已完成一批" in head
    assert "legacy" in head
