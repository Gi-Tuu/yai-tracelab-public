"""批次汇总：三态计数、方向分布、根因聚合与平均质量分。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contracts import Verdict

if TYPE_CHECKING:
    from .evaluation import EvaluationResult
else:  # 审计 F6-03：独立导入本模块时，get_type_hints 也要能解析注解
    EvaluationResult = Any  # type: ignore[no-redef]

def _pairwise_outcomes(result: EvaluationResult) -> dict[str, int]:
    """Case 级配对结果计数（审计可读性拆分，口径见 summarise docstring）。"""
    unpaired_cases = sum(1 for cr in result.results if cr.assessments and not cr.case.is_paired)
    unknown_cases_paired = sum(
        1 for cr in result.results
        if cr.case.is_paired and cr.comparison.direction == "insufficient_evidence"
    )
    equal_both_fail = equal_both_pass = both_failed_cases = investigate_cases = 0
    for cr in result.results:
        if cr.comparison.release_recommendation == "investigate":
            investigate_cases += 1
        if len(cr.assessments) < 2:
            continue
        pair = (cr.assessments[0], cr.assessments[-1])
        if any(a.is_unknown for a in pair):
            continue
        if all(a.verdict is Verdict.FAIL for a in pair):
            both_failed_cases += 1
            if cr.comparison.direction == "equal":
                equal_both_fail += 1
        elif all(a.verdict is Verdict.PASS for a in pair) and cr.comparison.direction == "equal":
            equal_both_pass += 1
    return {
        "unpaired_cases": unpaired_cases,
        "unknown_cases_paired": unknown_cases_paired,
        "equal_both_fail": equal_both_fail,
        "equal_both_pass": equal_both_pass,
        "both_failed_cases": both_failed_cases,
        "investigate_cases": investigate_cases,
        "cases_without_runs": sum(1 for cr in result.results if not cr.assessments),
    }


def _root_cause_counts(runs: list[Any]) -> dict[str, int]:
    """按根因标签计数（未排序，排序在 summarise 里做）。"""
    labels: dict[str, int] = {}
    for assessment in runs:
        for violation in assessment.violations:
            labels[violation.label] = labels.get(violation.label, 0) + 1
    return labels


def _avg_quality(evaluable_runs: list[Any]) -> tuple[float | None, int]:
    """返回 `(avg_quality, scored_runs, excluded_unknown)`（UNKNOWN 不进分母，C-11）。"""
    avg = (
        round(sum(a.quality_score for a in evaluable_runs) / len(evaluable_runs), 1)
        if evaluable_runs else None
    )
    return avg, len(evaluable_runs)


def summarise(result: EvaluationResult) -> dict[str, Any]:
    """批次汇总：三态分开计数，并把"两版同错"从"无差异"里拆出来（C-06）。

    * `equal_both_fail`：方向为 equal 但两版**都判错**（错误构成相同）。这不是
      "无差异"，而是"两版在同一个坑里"，聚合建议不得据此 release；
    * `equal_both_pass`：方向为 equal 且两版都判对，这才是真正的无差异；
    * `avg_quality`：分母排除 UNKNOWN（C-11）——UNKNOWN 的质量分是评测器没能
      评分的结果，混进平均值会把"判不了"变成"分低"。
    """
    runs = [a for cr in result.results for a in cr.assessments]
    evaluable_runs = [a for a in runs if not a.is_unknown]
    dirs: dict[str, int] = {}
    for cr in result.results:
        dirs[cr.comparison.direction] = dirs.get(cr.comparison.direction, 0) + 1
    labels = _root_cause_counts(runs)
    outcomes = _pairwise_outcomes(result)
    avg_quality, scored_runs = _avg_quality(evaluable_runs)
    return {
        "cases": len(result.results),
        "runs": len(runs),
        "task_success": sum(1 for a in runs if a.verdict is Verdict.PASS),
        "task_failure": sum(1 for a in runs if a.verdict is Verdict.FAIL),
        # 三态必须分开报：UNKNOWN 既不是成功也不是"Agent 做错了"
        "task_unknown": sum(1 for a in runs if a.is_unknown),
        "verdicts": {
            Verdict.PASS.value: sum(1 for a in runs if a.verdict is Verdict.PASS),
            Verdict.FAIL.value: sum(1 for a in runs if a.verdict is Verdict.FAIL),
            Verdict.UNKNOWN.value: sum(1 for a in runs if a.is_unknown),
        },
        "invoice_created": sum(1 for a in runs if a.business_invoice_created),
        # C-11：平均质量分的分母是"可评估运行"，UNKNOWN 单独计数、不进分母
        "avg_quality": avg_quality,
        "avg_quality_scored_runs": scored_runs,
        "avg_quality_excluded_unknown": len(runs) - scored_runs,
        "directions": dirs,
        "root_cause_counts": dict(sorted(labels.items(), key=lambda kv: -kv[1])),
        "guards_detected": sum(len(a.guards) for a in runs),
        "abstained": sum(1 for a in runs if a.is_unknown),
        "equal_both_fail": outcomes["equal_both_fail"],
        "equal_both_pass": outcomes["equal_both_pass"],
        "both_failed_cases": outcomes["both_failed_cases"],
        "investigate_cases": outcomes["investigate_cases"],
        "cases_without_runs": outcomes["cases_without_runs"],
        "unpaired_cases": outcomes["unpaired_cases"],
        "unknown_cases_paired": outcomes["unknown_cases_paired"],
    }
