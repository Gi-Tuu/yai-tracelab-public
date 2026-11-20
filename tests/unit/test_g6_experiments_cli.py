"""Batch 1/2 验收：实验命令可复现 + 提交包自带「提交说明」。

两条都是"让评委/大模型初审低成本核验"的入口：
* `tracelab experiments`：不依赖官方资料包、固定种子、两次运行逐字节一致；
* 提交包根目录的 `提交说明.md`：交付物 → 包内路径 → 复现命令 → 数据边界。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tracelab.cli import main

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_submission_bundle.py"


@pytest.fixture(scope="module")
def bundle_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bundle_script_g6_note", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ Batch 1：实验命令


def test_experiments_cli_writes_machine_and_human_reports(tmp_path):
    out = tmp_path / "experiments"
    assert main(["experiments", "--out", str(out)]) == 0
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "tracelab-experiment-suite/1"
    assert payload["corpus"]["cases"] >= 6
    assert payload["corpus"]["digest"], "样本摘要用于证明本次跑的是同一批构造"
    text = (out / "report.md").read_text(encoding="utf-8")
    for keyword in ("变形实验报告", "语义保持", "业务变化", "自造"):
        assert keyword in text, keyword


def test_experiments_cli_numbers_are_complete_and_consistent(tmp_path):
    out = tmp_path / "exp"
    assert main(["experiments", "--out", str(out)]) == 0
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    invariance = payload["invariance"]
    sensitivity = payload["sensitivity"]
    # 分子/分母必须自洽：7 个 Case（含 1 个应拒判样本）× 6 类变换
    assert payload["corpus"]["cases"] == 7
    assert invariance["total"] == payload["corpus"]["cases"] * 6
    assert invariance["consistent"] + invariance["inconsistent"] == invariance["total"]
    assert invariance["consistency_ratio"] == 1.0
    # 业务变化：18 个变体（含 3 个"应拒判"变体），逐个都有期望与实测
    assert sensitivity["total"] == 18
    assert sensitivity["detected"] == sensitivity["total"]
    assert sum(sensitivity["rule_coverage"].values()) == sensitivity["total"]
    assert "R902" in sensitivity["rule_coverage"], "拒判规则必须被实验覆盖"
    assert sensitivity["indeterminate"] == 0, "预期之外的 UNKNOWN 必须为 0"
    assert len(sensitivity["outcomes"]) == sensitivity["total"]
    assert sensitivity["indeterminate"] == 0


def test_experiments_cli_is_byte_identical_across_runs(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["experiments", "--out", str(first)]) == 0
    assert main(["experiments", "--out", str(second)]) == 0
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    assert (first / "report.md").read_bytes() == (second / "report.md").read_bytes()


def test_experiments_cli_seed_is_recorded(tmp_path):
    out = tmp_path / "seeded"
    assert main(["experiments", "--out", str(out), "--seed", "7"]) == 0
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["seed"] == 7


def test_samples_module_is_the_single_source_of_truth():
    """测试侧构造器必须是产品侧生成器的薄再导出（否则两份样本会漂移）。"""
    import tests.builders as test_builders
    import tracelab.samples as samples

    for name in ("make_event", "make_run", "make_case", "basic_flow", "reference", "context"):
        assert getattr(test_builders, name) is getattr(samples, name), name


# ------------------------------------------------------------------ Batch 2：提交说明


def _note_text(script: ModuleType, tmp_path: Path, *, include_official: bool = False,
               include_regression: bool = False) -> str:
    bundle = tmp_path / "submission"
    bundle.mkdir(parents=True, exist_ok=True)
    script._write_submission_note(bundle, include_official=include_official,
                                  include_regression=include_regression)
    return (bundle / "提交说明.md").read_text(encoding="utf-8")


def test_submission_note_covers_four_deliverables(bundle_script, tmp_path):
    text = _note_text(bundle_script, tmp_path)
    for keyword in ("交付物对照", "可运行离线评测程序", "评测方法说明", "由命令生成",
                    "复现命令", "数据边界", "完整性核验"):
        assert keyword in text, keyword
    assert "docs/method.md" in text
    assert "tracelab/" in text
    assert "docs/submission-index.md" in text


def test_submission_note_declares_data_boundary_per_switch(bundle_script, tmp_path):
    default_text = _note_text(bundle_script, tmp_path / "a")
    assert "默认不含" in default_text and "data/official" in default_text
    with_official = _note_text(bundle_script, tmp_path / "b", include_official=True)
    assert "**包含**" in with_official
    with_regression = _note_text(bundle_script, tmp_path / "c", include_regression=True)
    assert "**包含** `regression/`" in with_regression


def test_submission_note_contains_no_self_reported_numbers(bundle_script, tmp_path):
    """说明只讲"东西在哪、怎么跑"，不写评测结论数字，避免与实跑结果不一致。"""
    text = _note_text(bundle_script, tmp_path)
    for suspicious in ("100%", "accuracy", "准确率：", "F1"):
        assert suspicious not in text, suspicious


def test_generated_names_and_allow_files_include_new_entries(bundle_script):
    assert "提交说明.md" in bundle_script.GENERATED_NAMES
    assert "docs/submission-index.md" in bundle_script.ALLOW_FILES
