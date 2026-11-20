"""第四轮收尾的回归测试（P4/已知项）。

* R3-B-08：experiments 变体与 metamorphic 变换必须同时更新 IR 与 raw（否则导出不可复现）
* R3-B-07：manifest 未评测时不得宣称 complete=true
* R3-A-08：复测前提不成立使用专用退出码 4（2 只表示输入/路径错误）
* R3-A-07：镜像构建期自检与发布后 CI 复核对同一路径判定一致（含嵌套命中/根级 dotfile）
* R3-A-06：明细金额签名与 money_identical 同源（不再两处各写一份）
"""

from __future__ import annotations

import json

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab import rules_business as rules
from tracelab.contracts import Dataset
from tracelab.evaluation import build_manifest, evaluate_dataset
from tracelab.experiments import _patch_input, _patch_output
from tracelab.metamorphic import money_equivalent_representation, run_invariance
from tracelab.state import money_signature


def _case(case_id: str = "P4", events=None):
    events = events if events is not None else basic_flow(prefix=case_id)
    run = make_run(f"{case_id}-A", events, final_state="SUCCEEDED", invoice_created=True)
    return make_case(case_id, context=context(), reference_result=reference(), runs=[run])


def _dataset(case) -> Dataset:
    return Dataset(cases=[case], issues=[], source="t", sha256="",
                   stats={"lines_total": 1, "cases_parsed": 1, "skipped": 0, "truncated": 0})


# ------------------------------------------------------------------ IR / raw 同步


def test_experiment_patch_syncs_raw_output():
    case = _case()
    patched = _patch_output(case, "COLLECT_INVOICE_DATA", amount_basis="tax_inclusive")
    events = patched.runs[0].events
    target = next(event for event in events if event.step_id == "COLLECT_INVOICE_DATA")
    assert target.observed_output["amount_basis"] == "tax_inclusive"
    assert target.raw["observed_output"]["amount_basis"] == "tax_inclusive", \
        "变体只改 IR 不同步 raw → 导出的是未变换原轨迹（R3-B-08）"


def test_experiment_patch_input_syncs_raw_input():
    case = _case()
    patched = _patch_input(case, "SUBMIT_INVOICE", user_confirmed=False)
    submit = next(event for event in patched.runs[0].events if event.step_id == "SUBMIT_INVOICE")
    assert submit.observed_input["user_confirmed"] is False
    assert submit.raw["observed_input"]["user_confirmed"] is False


def test_metamorphic_money_transform_syncs_raw():
    case = _case()
    transformed = money_equivalent_representation(case)
    for before, after in zip(case.runs[0].events, transformed.case.runs[0].events, strict=True):
        before_amounts = before.observed_output.get("computed_amounts")
        if not before_amounts:
            continue
        after_amounts = after.observed_output["computed_amounts"]
        assert after.raw["observed_output"]["computed_amounts"] == after_amounts, \
            "变换后的 raw 必须是变换后的值（否则导出后无法复现）"


def test_experiment_variant_export_round_trip_keeps_change():
    """把变体按 raw 序列化再反序列化，改动必须仍在（可交给别人复核）。"""
    case = _case()
    patched = _patch_output(case, "COLLECT_INVOICE_DATA", amount_basis="tax_inclusive")
    run = patched.runs[0]
    exported = json.loads(json.dumps([dict(event.raw) for event in run.events], ensure_ascii=False))
    collect = next(item for item in exported if item["step_id"] == "COLLECT_INVOICE_DATA")
    assert collect["observed_output"]["amount_basis"] == "tax_inclusive"


# ------------------------------------------------------------------ manifest 三态


def test_manifest_without_evaluation_is_not_claimed_complete():
    shell = [event for event in basic_flow(prefix="M") if event.step_id != "COLLECT_INVOICE_DATA"]
    manifest = build_manifest(_dataset(_case("M", events=shell)))
    integrity = manifest["integrity"]
    assert integrity["evaluated"] is False
    assert integrity["complete"] is None, "没评测 ≠ 没有 UNKNOWN，不能断言完整"
    assert integrity["unknown_runs"] is None
    evaluated = evaluate_dataset(_dataset(_case("M", events=shell))).manifest["integrity"]
    assert evaluated["evaluated"] is True
    assert isinstance(evaluated["complete"], bool)


# ------------------------------------------------------------------ 退出码 4


def test_regression_compatibility_uses_dedicated_exit_code():
    from tracelab.regression import CompatibilityIssue, VerifyReport

    report = VerifyReport()
    report.compatibility.append(CompatibilityIssue("error", "dataset_sha256", "a", "b", "hint"))
    assert report.exit_code == 4
    assert VerifyReport().exit_code == 0


# ------------------------------------------------------------------ 镜像两端口径


def test_mirror_ci_and_generator_agree_on_representative_paths():
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "build_public_mirror.py"
    if not script.is_file():  # 公开镜像里没有生成器脚本
        return
    spec = importlib.util.spec_from_file_location("mirror_parity", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    paths = [
        "tracelab/stray.cpython-311.pyc",     # 缓存目录之外的字节码
        "x/y.pyo",
        "docs/our_credential.json",           # 嵌套凭证命名
        "a/token.txt",
        "x/y.log",
        "docs/.env",                          # 根级 dotfile 与嵌套 dotfile
        ".env",
        "data/official/x.jsonl",
        "tests/integration/t.py",
        "docs/ablation-report.md",
        "docs/casebook-a.md",
        "README.md",
        "docs/method.md",
    ]
    for rel in paths:
        assert module.generator_matches_exclusion(rel) == module.ci_matches_exclusion(rel), rel
    assert "*.pyc" in module.CI_EXCLUDED_GLOBS


# ------------------------------------------------------------------ 金额签名同源


def test_line_amount_signature_delegates_to_state():
    assert rules._line_amount_signature("1.00") == ("amount", "1.00")
    assert rules._line_amount_signature(1000.0)[0] == "amount"
    assert money_signature("abc") == ("unparsed", "invalid")
    assert rules._line_amount_signature("abc")[0] == "unparsed"
    # 两套语义同源：合法金额的规范化字符串必须一致
    assert rules._line_amount_signature("60.0")[1] == money_signature(60)[1]


# ------------------------------------------------------------------ 变形实验不回归


def test_invariance_still_consistent_after_raw_sync():
    report = run_invariance([_case("I1")])
    assert report.cases_total == 1
    assert report.skipped == []
    assert report.consistency_ratio == 1.0
