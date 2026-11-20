"""版本比较：同 Case 双版本配对判定（PASS/FAIL/UNKNOWN 三态优先）。"""

from __future__ import annotations

from dataclasses import dataclass

from .attribution import RunAssessment
from .contracts import Case, Verdict

# ------------------------------------------------------------------- 版本比较

@dataclass(frozen=True)
class VersionComparison:
    direction: str                 # baseline_better | candidate_better | equal | insufficient_evidence
    release_recommendation: str    # release | gray | defer | investigate
    reason: str
    quality_delta: float = 0.0
    #: 审计 F-03：比较结果必须能指回"比的是哪两条运行"，否则重复 run_id/乱序数据下
    #: 无法复核方向来自哪两侧。
    base_run_id: str = ""
    cand_run_id: str = ""


def _comparison_guard(case: Case, base: RunAssessment, cand: RunAssessment) -> VersionComparison | None:
    """配对前置检查（审计可读性拆分）：未配对 / 同版本号 / 任一侧 UNKNOWN → 拒判。"""
    if not case.is_paired:
        return VersionComparison(
            "insufficient_evidence", "investigate",
            f"Case {case.case_id} 只有 {len(case.runs)} 条运行，无法做版本配对比较。",
        )
    # C-09：基线/候选靠数据里的数组顺序判定；同 agent_version 时无法证明谁是基线。
    if case.runs[0].agent_version == case.runs[1].agent_version:
        return VersionComparison(
            "insufficient_evidence", "investigate",
            f"Case {case.case_id} 的两条运行 agent_version 都是 "
            f"{case.runs[0].agent_version!r}，无法确定哪条是基线、哪条是候选；"
            "请按数据契约把 runs 写成 [基线, 候选]，并保证两版版本号不同后复测。",
        )
    if base.is_unknown or cand.is_unknown:
        sides: list[str] = []
        if base.is_unknown:
            sides.append("基线版")
        if cand.is_unknown:
            sides.append("候选版")
        return VersionComparison(
            "insufficient_evidence", "investigate",
            "、".join(sides) + "判定为 UNKNOWN（证据不足或评测器未能完成判断），"
            "本 Case 不产生改善/退化结论；需补齐证据或修复评测流程后复测。",
            0.0,
        )
    return None


def _candidate_regression(cand: RunAssessment, delta: float, ids: dict[str, str]) -> VersionComparison:
    """基线正确、候选出错 → 局部退化（defer）。"""
    fixed = "候选版新增问题：" + "、".join(cand.labels)
    return VersionComparison(
        "baseline_better", "defer",
        f"基线版处理正确，候选版出现可证实错误（{fixed}），本 Case 为局部退化。单一 Case 不能代表整体版本结论。",
        delta, **ids,
    )


def _candidate_improvement(base: RunAssessment, delta: float, ids: dict[str, str]) -> VersionComparison:
    """基线出错、候选正确 → 局部提升（release）。"""
    fixed = "修复了：" + "、".join(base.labels)
    return VersionComparison(
        "candidate_better", "release",
        f"基线版存在可证实错误（{fixed}），候选版处理正确，本 Case 为局部提升。",
        delta, **ids,
    )


def _both_passed(delta: float, ids: dict[str, str]) -> VersionComparison:
    """两版都 PASS：同分 equal，不同分按质量分定方向（差异来自过程扣分）。"""
    if abs(delta) < 0.5:
        return VersionComparison(
            "equal", "release",
            "两版均处理正确且质量分相同，本 Case 无差异。",
            delta, **ids,
        )
    winner = "候选版" if delta > 0 else "基线版"
    direction = "candidate_better" if delta > 0 else "baseline_better"
    return VersionComparison(
        direction, "release" if delta > 0 else "defer",
        f"两版均处理正确，但{winner}质量分更高（Δ={delta:+.1f}），差异来自过程扣分而非业务结果。",
        delta, **ids,
    )


def _both_failed(
    base: RunAssessment, cand: RunAssessment, delta: float, ids: dict[str, str]
) -> VersionComparison:
    """两版都 FAIL：同标签 / 同扣分 / 不同扣分三种情况（审计 F-06）。"""
    same_labels = set(base.labels) == set(cand.labels)
    if same_labels:
        return VersionComparison(
            "equal", "investigate",
            f"两版同错：在同一组错误上失败（{'、'.join(sorted(set(base.labels))) or '未知'}），"
            "错误构成相同、未观察到版本差异；这不等于「两版能力相同」，也不能当作无风险，需要补充证据或修复后复测。",
            delta, **ids,
        )
    if abs(delta) < 0.5:
        return VersionComparison(
            "equal", "investigate",
            f"两版都失败且扣分相同（Δ={delta:+.1f}）：基线 {base.labels} → 候选 {cand.labels}；"
            "错误构成不同但严重度相同，不能据此判定谁更好。",
            delta, **ids,
        )
    direction = "candidate_better" if delta > 0 else "baseline_better"
    return VersionComparison(
        direction, "investigate",
        f"两版均未正确完成任务：基线 {base.labels} → 候选 {cand.labels}（Δ={delta:+.1f}）。"
        f"错误构成发生变化，但两版都不满足发布条件，需按错误类型分别修复。",
        delta, **ids,
    )


def compare_versions(case: Case, base: RunAssessment, cand: RunAssessment) -> VersionComparison:
    """同 Case 双版本配对比较（编排层：前置检查 → 按结果类型分派）。

    先比内部三态，再比质量分。**任一方 UNKNOWN 一律 insufficient_evidence**：
    评测器自己没判断出来，不能被解读成"版本变好/变差"。
    """
    guard = _comparison_guard(case, base, cand)
    if guard is not None:
        return guard
    delta = round(cand.quality_score - base.quality_score, 1)
    ids = {"base_run_id": base.run_id, "cand_run_id": cand.run_id}
    base_ok = base.verdict is Verdict.PASS
    cand_ok = cand.verdict is Verdict.PASS
    if base_ok and not cand_ok:
        return _candidate_regression(cand, delta, ids)
    if cand_ok and not base_ok:
        return _candidate_improvement(base, delta, ids)
    if base_ok and cand_ok:
        return _both_passed(delta, ids)
    return _both_failed(base, cand, delta, ids)
