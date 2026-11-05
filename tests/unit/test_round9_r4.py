"""第四轮审计修复的回归测试（Batch 0–3）。

覆盖：报告方向口径统一、金额符号零、证据覆盖率分母、IR/raw 同步、manifest 三态、
打包 fail-closed、include-* 覆盖、导出路径守卫、镜像两端口径、退出码文档、UTF-8 子进程。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Dataset, Verdict
from tracelab.evaluation import build_manifest, evaluate_dataset
from tracelab.experiments import synthetic_clean_case
from tracelab.metamorphic import money_equivalent_representation
from tracelab.reports import ab_pair, comparable_directions
from tracelab.state import money_identical, money_signature

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"


def _case(case_id: str = "R4", events=None):
    events = events if events is not None else basic_flow(prefix=case_id)
    run = make_run(f"{case_id}-A", events, final_state="SUCCEEDED", invoice_created=True)
    return make_case(case_id, context=context(), reference_result=reference(), runs=[run])


def _dataset(cases) -> Dataset:
    return Dataset(cases=list(cases), issues=[], source="t", sha256="",
                   stats={"lines_total": len(cases), "cases_parsed": len(cases),
                          "skipped": 0, "truncated": 0})


# ------------------------------------------------------------------ 金额：符号零


def test_sign_zero_no_longer_flips_verdict():
    good = [{"item_id": "ITEM-006", "exclusive_amount": 0.0}]

    def verdict_for(value):
        events = []
        for event in basic_flow(prefix="z0", task_type="BATCH"):
            output = dict(event.observed_output)
            if event.step_id == "COLLECT_INVOICE_DATA":
                output["line_items"] = [{"item_id": "ITEM-006", "exclusive_amount": value}]
                output["line_count"] = 1
            events.append(replace(event, observed_output=output))
        run = make_run("R0", events, final_state="SUCCEEDED", invoice_created=True)
        case = make_case("Z0", context=context(),
                         reference_result=reference(invoice_task_type="BATCH", expected_line_items=good),
                         runs=[run])
        return assess_run(case, run).verdict

    assert verdict_for(0.0) is Verdict.PASS
    assert verdict_for(-0.0) is Verdict.PASS, "±0.0 是同一个金额，不得判违规（R4-04）"
    assert money_signature(0.0) == money_signature(-0.0)
    assert money_identical(0.0, -0.0) is True


# ------------------------------------------------------------------ 证据覆盖率


def test_evidence_coverage_does_not_shrink_denominator(capsys):
    from tracelab.adapters import load_dataset
    from tracelab.evaluation import load_reference_labels
    from tracelab.secondary import evidence_coverage_of_gold

    cases = ROOT / "data" / "official" / "student-1.0" / "02_公开训练轨迹.jsonl"
    labels_path = ROOT / "data" / "official" / "student-1.0" / "03_公开训练参考标注.jsonl"
    if not cases.is_file():
        return  # 公开镜像无官方数据
    result = evaluate_dataset(load_dataset(cases))
    labels = load_reference_labels(labels_path)
    normal = evidence_coverage_of_gold(result, labels)

    renamed = {case_id: {"case_id": case_id, "run_assessments": [
        {**ra, "run_id": str(ra["run_id"]) + "-RENAMED"} for ra in payload.get("run_assessments", [])
    ]} for case_id, payload in labels.items()}
    shifted = evidence_coverage_of_gold(result, renamed)
    assert shifted.denominator == normal.denominator, "未匹配金标仍须计入分母（R4-10）"
    assert shifted.value < normal.value
    assert "金标运行与本次评测匹配" in shifted.reason


# ------------------------------------------------------------------ IR/raw 同步


def test_tool_calls_raw_shape_is_preserved():
    event = make_event(
        "T-1", 1, "COLLECT_INVOICE_DATA",
        observed_output={"computed_amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0,
                                              "inclusive_amount": 1060.0}},
        tool_calls=[{"name": "invoice.submit", "arguments": {"a": 1},
                     "result": {"ok": True}, "attempt": 1, "trace_id": "T-9"}],
    )
    run = make_run("RT", [event], final_state="SUCCEEDED", invoice_created=True)
    case = make_case("TC", context=context(), reference_result=reference(), runs=[run])
    transformed = money_equivalent_representation(case)
    raw_tool = transformed.case.runs[0].events[0].raw["tool_calls"][0]
    assert raw_tool == {"name": "invoice.submit", "arguments": {"a": 1},
                        "result": {"ok": True}, "attempt": 1, "trace_id": "T-9"}, \
        "tool_calls 的 raw 必须保持原始 vendor 形状（R4-13）"


def test_rebuild_case_syncs_case_raw_for_round_trip():
    from tracelab.experiments import _patch_output

    patched = _patch_output(synthetic_clean_case(), "COLLECT_INVOICE_DATA",
                            buyer_id="CUST-X")
    dumped = patched.raw
    assert "CUST-X" in json.dumps(dumped, ensure_ascii=False), \
        "case.raw 必须同步（否则按原始记录导出拿到的是变换前轨迹，R4-06）"
    # 变体仍可被官方适配器读回（导出→复现链路）
    import tempfile

    from tracelab.adapters import get_adapter

    tmp = Path(tempfile.mkdtemp(prefix="r4-rt-"))
    path = tmp / "cases.jsonl"
    path.write_text(json.dumps(dumped, ensure_ascii=False) + "\n", encoding="utf-8")
    dataset = get_adapter("official").load(path)
    events = dataset.cases[0].runs[0].events
    assert any(event.observed_output.get("buyer_id") == "CUST-X" for event in events)


# ------------------------------------------------------------------ manifest 三态


def test_manifest_evaluated_inference_covers_all_count_keys():
    ds = _dataset([_case("M")])
    unevaluated = build_manifest(ds)
    assert unevaluated["integrity"]["evaluated"] is False
    assert unevaluated["integrity"]["complete"] is None

    partial = build_manifest(ds, extra={"degraded_runs": 3})
    assert partial["integrity"]["evaluated"] is True
    assert partial["integrity"]["degraded_runs"] == 3
    assert partial["integrity"]["complete"] is False

    evaluated = build_manifest(ds, extra={"unknown_runs": 1, "evaluated": True})
    assert evaluated["integrity"]["evaluated"] is True
    assert evaluated["integrity"]["unknown_runs"] == 1


# ------------------------------------------------------------------ 报告方向口径


def test_reports_agree_on_comparable_directions(tmp_path):
    pair = make_case("PAIR", context=context(), reference_result=reference(), runs=[])
    runs = [make_run(f"PAIR-{i}", basic_flow(prefix=f"pr{i}"), final_state="SUCCEEDED",
                     invoice_created=True, agent_version=f"v{i}") for i in range(2)]
    pair = replace(pair, runs=tuple(runs))
    three = make_case("THREE", context=context(), reference_result=reference(), runs=tuple(
        make_run(f"THREE-{i}", basic_flow(prefix=f"th{i}"), final_state="SUCCEEDED",
                 invoice_created=True, agent_version=f"v{i}") for i in range(3)
    ))
    zero = make_case("ZERO", context=context(), reference_result=reference(), runs=())
    result = evaluate_dataset(_dataset([pair, three, zero]))
    official = (tmp_path / "o.md")
    from tracelab.official_report import write_official_report

    text = write_official_report(result, official).read_text(encoding="utf-8")
    dirs = comparable_directions(result)
    assert "有效 A/B 成对 Case 数：1" in text
    assert f"A {sum(dirs.values())}" in text or "A 1" in text
    assert "不可配对（runs≠2）" in text
    # 报告层与 helper 同源：只有 1 个可比较对
    assert sum(dirs.values()) == 1
    assert len([cr for cr in result.results if ab_pair(cr)]) == 1


# ------------------------------------------------------------------ 打包 fail-closed


def _bundle_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("bundle_r4", ROOT / "scripts" / "build_submission_bundle.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bundle_scan_failure_leaves_no_success_named_zip(tmp_path):
    module = _bundle_module()
    # 目录名含敏感串（拼接写法：避免测试文件自身被包内敏感串扫描命中）
    out_root = tmp_path / ("p" + "wd=1")
    options = module.BundleOptions(root=ROOT, out_root=out_root, allow_outside=True,
                                   skip_tests=True, skip_checks=True)
    code = module.build_bundle(options)
    assert code == 3
    assert not list(out_root.glob("*.zip")), "扫描未通过时不得留下成功名 zip（R4-01）"
    assert list(out_root.glob("*.zip.incomplete")), "失败产物应为 .zip.incomplete"
    checklist = (out_root / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "全部通过" not in checklist.split("## 对应赛题要求")[0], "失败时清单不得写全部通过"
    marker = module.read_marker(out_root)
    assert marker is not None and marker.get("files"), "失败也必须写真实文件清单，否则目录不可重建"


def test_include_official_requires_existing_whitelist_files(tmp_path):
    module = _bundle_module()
    root = tmp_path / "root"
    (root / "data" / "official").mkdir(parents=True)
    (root / "data" / "official" / "MANIFEST.sha256").write_text("x", encoding="utf-8")
    bundle = tmp_path / "bundle"
    (bundle / "data" / "official").mkdir(parents=True)
    (bundle / "data" / "official" / "MANIFEST.sha256").write_text("x", encoding="utf-8")
    check = module._official_check(bundle, include_official=True, root=root)
    assert check.passed is True, "源目录只有 1 个白名单文件时，复制它即视为完整"
    # 源目录有 2 个、包内只放 1 个 → 必须失败
    (root / "data" / "official" / "student-1.0").mkdir(parents=True)
    (root / "data" / "official" / "student-1.0" / "01_竞赛版流程字典.json").write_text("{}", encoding="utf-8")
    check = module._official_check(bundle, include_official=True, root=root)
    assert check.passed is False
    assert "缺失" in check.detail


# ------------------------------------------------------------------ 辅助脚本守卫


def test_export_refuses_bundle_directories():
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_r4", ROOT / "scripts" / "export_submission_doc.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module._guard_out_dir(ROOT / "dist") is not None
    assert module._guard_out_dir(ROOT / "dist" / "submission") is not None
    assert module._guard_out_dir(ROOT / "out" / "submission-doc") is None


def test_validator_and_sample_script_type_errors_exit_2(tmp_path):
    validator = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_official_validator.py"),
         "--validator", str(ROOT / "docs"), "--cases", str(SAMPLE), "--submission", str(SAMPLE)],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert validator.returncode == 2
    assert "Traceback" not in validator.stderr

    sample = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_sample_dataset.py"), "--out", "pyproject.toml"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert sample.returncode == 2
    assert "Traceback" not in sample.stderr


# ------------------------------------------------------------------ 镜像两端口径


def test_mirror_globs_derived_and_parity_on_edge_paths():
    import ast
    import importlib.util
    import re

    script = ROOT / "scripts" / "build_public_mirror.py"
    if not script.is_file():
        return
    spec = importlib.util.spec_from_file_location("mirror_r4", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    block = re.search(r"EXCLUDED_GLOBS = (\([^)]*\))", module.MIRROR_CI)
    assert block is not None, "CI 模板里必须有排除清单"
    assert tuple(ast.literal_eval(block.group(1))) == tuple(module.CI_EXCLUDED_GLOBS), \
        "MIRROR_CI 的清单必须由生成器注入（R4-14）"

    paths = [
        "secret/stuff.txt", "a/PASSWORD.txt", "a/X.PYC", "x/y.pyc", "x/y.pyo",
        "docs/our_credential.json", "a/token.txt", "x/y.log", "docs/.env", ".env",
        "data/official/x.jsonl", "tests/integration/t.py", "docs/ablation-report.md",
        "docs/casebook-a.md", "README.md", "docs/method.md", "out/report.md", "dist/a.zip",
    ]
    for rel in paths:
        assert module.generator_matches_exclusion(rel) == module.ci_matches_exclusion(rel), rel


# ------------------------------------------------------------------ UTF-8 子进程


def test_bundle_run_forces_utf8(tmp_path):
    module = _bundle_module()
    old = os.environ.get("PYTHONIOENCODING")
    os.environ["PYTHONIOENCODING"] = "cp1252"
    try:
        proc = module.run([sys.executable, "-c", "print('中文输出测试')"], ROOT)
    finally:
        if old is None:
            os.environ.pop("PYTHONIOENCODING", None)
        else:
            os.environ["PYTHONIOENCODING"] = old
    assert proc.returncode == 0, proc.stderr[-300:]
    assert "中文输出测试" in proc.stdout


# ------------------------------------------------------------------ 文档与退出码


def test_exit_codes_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "CLI 退出码约定" in readme
    for code in ("`0`", "`1`", "`2`", "`3`", "`4`"):
        assert f"| {code} |" in readme
    method = (ROOT / "docs" / "method.md").read_text(encoding="utf-8")
    assert "复测前提不成立" in method and "`4`" in method
    help_text = subprocess.run(
        [sys.executable, "-m", "tracelab", "regression", "--help"],
        cwd=str(ROOT), capture_output=True, text=True,
    ).stdout
    assert "4=" in help_text, "--help 必须说明退出码 4"


def test_section7_marks_already_packed_runs(tmp_path):
    from tracelab.adapters import load_dataset
    from tracelab.official_report import write_official_report

    if not (ROOT / "regression" / "invoice-pack" / "regression-pack.json").is_file():
        return
    result = evaluate_dataset(load_dataset(SAMPLE))
    text = write_official_report(result, tmp_path / "r.md").read_text(encoding="utf-8")
    if "regression/invoice-pack" in text:
        assert "已在 `regression/invoice-pack`" in text or "加入 `regression/invoice-pack`" in text
