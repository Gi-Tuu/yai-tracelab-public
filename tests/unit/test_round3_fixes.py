"""第三批审计修复的回归测试（2026-11-04 收尾批：R2-08/09/11/12/15/16/19/20/21/22）。

这批的目标不是"新功能"，而是让**报告数字自洽、拒绝失败也要说得清**：
指标同时给精确率/召回率、官方报告三节口径统一、CLI 退出码契约、
提交包 fail-closed、镜像 CI 与生成器共用排除清单。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.adapters import load_dataset
from tracelab.attribution import assess_run
from tracelab.contracts import Dataset, Verdict
from tracelab.evaluation import evaluate_dataset
from tracelab.evidence_record import build_records
from tracelab.evidence_verify import STATUS_VERIFIED, verify_record
from tracelab.official_report import write_official_report
from tracelab.reports import build_markdown
from tracelab.rules import RULES

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"


def _dataset(cases) -> Dataset:
    return Dataset(cases=list(cases), issues=[], source="test", sha256="",
                   stats={"lines_total": len(cases), "cases_parsed": len(cases),
                          "skipped": 0, "truncated": 0})


def _pair(case_id: str, *, events=None, ref=None, ctx=None, runs: int = 2) -> object:
    events = events if events is not None else basic_flow(prefix=case_id)
    run_objs = [
        make_run(f"{case_id}-R{i + 1}", events, final_state="SUCCEEDED",
                 invoice_created=True, agent_version=f"v{i + 1}")
        for i in range(runs)
    ]
    return make_case(case_id, context=ctx or context(), reference_result=ref or reference(), runs=run_objs)


# ---------------------------------------------------------------- 指标：P/R 同显


def test_r2_08_propagation_metric_shows_precision_and_recall():
    from tracelab.secondary import propagation_accuracy

    class Stub:
        propagation_expected = 17
        propagation_hit = 15
        propagation_hit_edges = 15
        propagation_edges = 15

    metric = propagation_accuracy([Stub()])
    assert metric.value == 1.0
    assert metric.recall == 0.8824
    assert "P 100.0%" in metric.display and "R 88.2%" in metric.display
    assert "召回率 88.2%" in metric.reason


def test_r2_08_edge_dedupe_keeps_precision_sane():
    from tracelab.mutation import InjectionResult

    result = InjectionResult(injection="x", description="", expected_label="AMOUNT_BASIS")
    result.propagation_expected = 2
    result.propagation_hit = 2
    result.propagation_hit_edges = 2
    result.propagation_edges = 2
    assert result.propagation_precision == 1.0


# ---------------------------------------------------------------- 官方报告口径统一


def test_r2_09_three_run_case_is_not_counted_as_pair(tmp_path):
    case = _pair("THREE", runs=3)
    result = evaluate_dataset(_dataset([case]))
    text = write_official_report(result, tmp_path / "r.md").read_text(encoding="utf-8")
    # §1 的成对 Case 数必须与 §3 的"不可配对"说明一致：3-run 不算 A/B 对
    assert "有效 A/B 成对 Case 数：0" in text
    assert "不可配对（runs≠2）" in text
    assert "证据不足 0 个 Case" in text


def test_r2_15_zero_run_case_is_not_called_unknown(tmp_path):
    no_runs = make_case("ZERO", context=context(), reference_result=reference(), runs=[])
    result = evaluate_dataset(_dataset([no_runs]))
    text = build_markdown(result, None)
    assert "无可评估运行" in text or "无法配对" in text


def test_r2_16_uncertainty_is_disclosed_in_reports(tmp_path):
    parse = make_event("P-1", 1, "PARSE_INTENT",
                       observed_output={"intent": "ISSUE", "invoice_task_type": "BLUE"})
    second = make_event("P-2", 2, "PARSE_INTENT",
                        observed_output={"intent": "ISSUE", "invoice_task_type": "RED"})
    events = [parse, second] + [e for e in basic_flow(prefix="u") if e.step_id not in ("PARSE_INTENT",)]
    case = _pair("UNC", events=events)
    result = evaluate_dataset(_dataset([case]))
    official = write_official_report(result, tmp_path / "r.md").read_text(encoding="utf-8")
    markdown = build_markdown(result, None)
    assert "含不确定性的运行" in official
    assert "含不确定性的运行" in markdown or "多条 PARSE_INTENT" in markdown


# ---------------------------------------------------------------- 重复 INSUFFICIENT_EVIDENCE


def test_r2_19_single_insufficient_evidence_per_run():
    opaque = [make_event("O-1", 1, "AGENT_PARSE"), make_event("O-2", 2, "AGENT_SUBMIT")]
    run = make_run("R1", opaque, final_state="SUCCEEDED", invoice_created=True)
    case = make_case("OPQ", context=context(), reference_result=reference(), runs=[run])
    assessment = assess_run(case, run)
    assert [v.label for v in assessment.violations].count("INSUFFICIENT_EVIDENCE") == 1


# ---------------------------------------------------------------- R902 记录覆盖新判据


def test_r2_20_invalid_money_has_r902_record_and_verifies():
    events = []
    for event in basic_flow(prefix="im"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["amount_value"] = "1,000.00"
            from dataclasses import replace
            event = replace(event, observed_output=output)
        events.append(event)
    run = make_run("R1", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case("IM", context=context(), reference_result=reference(), runs=[run])
    assessment = assess_run(case, run)
    assert assessment.verdict is Verdict.UNKNOWN
    record = next(r for r in build_records(case, run, assessment) if r.rule_id == "R902")
    assert verify_record(record, case, run).status == STATUS_VERIFIED


# ---------------------------------------------------------------- CLI 退出码契约


def test_r2_11_cli_input_errors_exit_2_without_traceback(tmp_path):
    cases = str(SAMPLE)
    commands = [
        ["manifest", "--cases", cases, "--adapter", "nope"],
        ["evaluate", "--cases", str(ROOT / "docs"), "--out", str(tmp_path / "o1")],
        ["ablate", "--cases", cases, "--labels", str(SAMPLE), "--out", str(ROOT / "pyproject.toml")],
        ["mutate", "--cases", cases, "--out", str(ROOT / "pyproject.toml")],
        ["regression", "verify", "--pack", "regression/invoice-pack/regression-pack.json",
         "--cases", cases, "--out", str(ROOT / "pyproject.toml")],
    ]
    for command in commands:
        proc = subprocess.run([sys.executable, "-m", "tracelab", *command],
                              cwd=str(ROOT), capture_output=True, text=True)
        assert proc.returncode == 2, (command, proc.returncode, proc.stderr[-300:])
        assert "Traceback" not in proc.stderr, command


# ---------------------------------------------------------------- 提交包 fail-closed


def test_r2_12_bundle_official_check_fails_when_empty(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("bundle_mod", ROOT / "scripts" / "build_submission_bundle.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    empty_bundle = tmp_path / "bundle"
    empty_bundle.mkdir()
    check = module._official_check(empty_bundle, include_official=True)
    assert check.passed is False, "声明打包官方资料但包内为空时必须失败"


# ---------------------------------------------------------------- 镜像排除清单共用


def test_r2_21_mirror_ci_globs_cover_credential_names():
    """生成器与 CI 必须覆盖同一批凭证命名；公开镜像里没有生成器脚本，改为核对 CI 文件本身。"""
    import fnmatch
    import importlib.util

    script = ROOT / "scripts" / "build_public_mirror.py"
    names = ("github_token.txt", "apikey.txt", "our_credential.json", "secret-notes.md")
    if script.is_file():
        spec = importlib.util.spec_from_file_location("mirror_mod", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name in names:
            assert any(fnmatch.fnmatch(name, glob) for glob in module.EXCLUSION_GLOBS), name
            assert "*token*" in module.MIRROR_CI
        return
    # 公开镜像：生成器被排除，改为断言已发布的 CI 复核文件包含全部凭证模式
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for pattern in ("*credential*", "*secret*", "*token*", "*apikey*"):
        assert pattern in ci_text, pattern


# ---------------------------------------------------------------- 注入实验去重


def test_r2_22_mutate_dedupes_repeated_input(tmp_path):
    # R3-C-03: 必须与「单输入基线」精确相等，不能用上界（去重失效时 38 也 <= 40）。
    single = tmp_path / "single"
    double = tmp_path / "double"
    base = [sys.executable, "-m", "tracelab", "mutate", "--cases", str(SAMPLE)]
    first = subprocess.run([*base, "--out", str(single)], cwd=str(ROOT),
                           capture_output=True, text=True)
    assert first.returncode == 0, first.stderr[-400:]
    proc = subprocess.run([*base, "--cases", str(SAMPLE), "--out", str(double)],
                          cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "去重" in proc.stderr
    one = json.loads((single / "mutation.json").read_text(encoding="utf-8"))["overall"]
    two = json.loads((double / "mutation.json").read_text(encoding="utf-8"))["overall"]
    assert two["tried"] == one["tried"], (one["tried"], two["tried"])
    assert two["effective"] == one["effective"]
    assert two["injections"] == 8


# ---------------------------------------------------------------- 单测下沉：ablation / mutation


def test_ablation_profiles_partition_all_rules():
    from tracelab.ablation import BUSINESS_RULE_IDS, GENERIC_RULE_IDS

    all_rule_ids = {rule.rule_id for rule in RULES}
    assert GENERIC_RULE_IDS | BUSINESS_RULE_IDS == all_rule_ids
    assert not (GENERIC_RULE_IDS & BUSINESS_RULE_IDS)


def test_mutation_study_runs_on_synthetic_samples(tmp_path):
    from tracelab.mutation import overall, run_injection_study

    dataset = load_dataset(SAMPLE)
    results = run_injection_study(dataset)
    stats = overall(results)
    assert stats["injections"] == 8
    assert stats["tried"] > 0
    assert stats["detected"] <= stats["effective"]
    assert all(r.tried >= r.invalid for r in results)
    # R3-C-04: 本批样本上确实有 3 类注入器没有适用样本——写成显式断言，
    # 而不是 injections>=5 这种能放过空实现的宽松条件。
    zero_tried = {r.injection for r in results if r.tried == 0}
    assert zero_tried == {"context_stale_amount", "batch_item_drop", "product_swap"}, zero_tried
    assert {r.injection for r in results if r.tried > 0}, "至少要有注入器真的尝试过"


# ---------------------------------------------------------------- evidence CLI 端到端


def test_evidence_cli_end_to_end(tmp_path):
    # R3-C-05: 证据链 CLI 此前在单测里 0 覆盖，这里跑通「产出 + 独立复核」。
    out = tmp_path / "evidence-out"
    proc = subprocess.run(
        [sys.executable, "-m", "tracelab", "evidence", "--cases", str(SAMPLE), "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    records = (out / "evidence" / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert records, "必须产出依据记录"
    payload = json.loads((out / "evidence" / "verify.json").read_text(encoding="utf-8"))
    assert payload["counts"]["MISMATCH"] == 0
    assert payload["counts"]["VERIFIED"] == len(records)
