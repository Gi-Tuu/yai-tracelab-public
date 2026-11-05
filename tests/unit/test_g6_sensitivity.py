"""G6-A2：业务语义变化实验（sensitivity）。

12 个声明式变体，覆盖 12 条规则（R001/R002/R003/R004/R005/R006/R007/R009/R010/R011/R012/R013）。
期望值是**按业务含义预先写死**的（在 `tracelab/experiments.py` 的 VariantSpec 里），
不是跑完评测器回填的；测试只负责核对"该报的报了、不该报的没报、首因落在该落的步骤"。
"""

from __future__ import annotations

import json

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.contracts import Case, Verdict
from tracelab.experiments import (
    SENSITIVITY_VARIANTS,
    render_markdown,
    run_sensitivity,
)


def _clean_case() -> Case:
    run = make_run("SENS-CLEAN", basic_flow(), final_state="SUCCEEDED", invoice_created=True)
    return make_case("SENS-CLEAN", context=context(), reference_result=reference(), runs=[run])


def test_baseline_is_clean_before_mutation():
    """基线必须本身是干净的：否则变体的"新规则"无法归因到变化本身。"""
    from tracelab.attribution import assess_run

    case = _clean_case()
    assessment = assess_run(case, case.runs[0])
    assert assessment.verdict is Verdict.PASS
    assert assessment.labels == []


def test_twelve_variants_declared_and_rule_coverage():
    assert len(SENSITIVITY_VARIANTS) >= 12
    rules = {spec.rule_under_test for spec in SENSITIVITY_VARIANTS}
    assert len(rules) >= 6, "至少要覆盖 6 条规则"
    assert {"R001", "R006", "R010"} <= rules
    # 第二轮补齐：取消误判 / 红票原票 / 多轮旧值 三条也要有变体
    assert {"R008", "R014", "R015"} <= rules, "R008/R014/R015 必须各自有语义变化变体"
    # 第三轮补齐：拒判（R902）也要有变体——证据不足时应"拒绝判断"而不是猜
    assert "R902" in rules, "R902 拒判必须有语义变化变体"
    assert len(rules) == 16, f"应覆盖 16 条规则（R001–R015 + R902），实际 {len(rules)}"
    unknown_variants = [spec.name for spec in SENSITIVITY_VARIANTS
                        if spec.expected_verdict is Verdict.UNKNOWN]
    assert len(unknown_variants) == 3, f"拒判类变体应为 3 个，实际 {unknown_variants}"
    for spec in SENSITIVITY_VARIANTS:
        assert spec.expected_rules, f"{spec.name} 必须声明期望规则"
        assert spec.expected_verdict in (Verdict.FAIL, Verdict.UNKNOWN), spec.name
        assert spec.changed_fields, f"{spec.name} 必须记录改了哪些字段"


def test_each_variant_is_detected_with_expected_verdict():
    """每个变体都要触发期望规则，并给出期望判定。"""
    report = run_sensitivity(_clean_case())
    failures = [
        item.as_dict()
        for item in report.outcomes
        if not (item.detected and item.verdict_ok)
    ]
    assert failures == [], json.dumps(failures, ensure_ascii=False)


def test_no_unexpected_rule_is_triggered():
    """不得牵连：变体声明的 must_not_report 一条都不许出现。"""
    report = run_sensitivity(_clean_case())
    leaked = [item.as_dict() for item in report.outcomes if item.unexpected_rules]
    assert leaked == [], json.dumps(leaked, ensure_ascii=False)


def test_attribution_positions_are_stable():
    """首因应落在业务事实发生的那一步（允许声明里的等价位置）。"""
    report = run_sensitivity(_clean_case())
    unstable = [item.as_dict() for item in report.outcomes if not item.attribution_ok]
    assert unstable == [], json.dumps(unstable, ensure_ascii=False)


def test_metric_numerators_and_denominators():
    """指标必须给出分子/分母/比例，且分子分母一致可核对。"""
    report = run_sensitivity(_clean_case())
    payload = report.as_dict()
    assert payload["total"] == len(SENSITIVITY_VARIANTS)
    assert payload["detected"] == payload["total"]
    assert payload["fault_sensitivity"] == 1.0
    assert payload["attribution_stable"] == payload["total"]
    assert payload["attribution_stability"] == 1.0
    assert payload["false_positive_variants"] == 0
    assert payload["false_positive_rate"] == 0.0
    assert payload["indeterminate"] == 0
    assert sum(payload["rule_coverage"].values()) == payload["total"]


def test_report_renders_short_markdown():
    report = run_sensitivity(_clean_case())
    text = render_markdown(sensitivity=report.as_dict())
    assert "sensitivity" in text
    assert "12" in text or str(report.total) in text
    assert "%" in text
