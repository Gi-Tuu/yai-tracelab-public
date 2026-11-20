"""规则包的统一版本对比与报告（对应赛题加分项：统一评测、归因与版本对比报告）。

分工
----
* `rulepacks.py`：**判定**（一个规则包 + 一条运行 → `PackAssessment`）；
* 本模块：**比较与呈现**（同一 Case 两条运行 → `PackComparison`；一批 Case → 报告）。

与开票评测的关系：开票场景原本就有 `evaluation.compare_versions` + `reports`；
本模块把它抽成"规则包无关"的形态，使**报销包这类新业务**不用改核心模块也能得到同样的
「逐 Case 方向 + 聚合建议 + 可复核报告」。方向与建议的判定口径与开票版一致（三态优先）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import Verdict
from .rulepacks import PackAssessment

#: 方向 → 单 Case 建议（与 `evaluation.compare_versions` 的映射保持一致）
_DIRECTION_RECOMMENDATION = {
    "candidate_better": "release",
    "baseline_better": "defer",
}


@dataclass(frozen=True)
class PackComparison:
    """同一 Case 的两条运行（基线/候选）在某个规则包下的比较结果。"""

    case_id: str
    base_run_id: str
    cand_run_id: str
    base_verdict: str
    cand_verdict: str
    direction: str          # candidate_better | baseline_better | equal | insufficient_evidence
    recommendation: str     # release | gray | defer | investigate
    reason: str
    quality_delta: float = 0.0
    #: 两侧都失败（"同错"）时置 True：聚合时不能当成"无差异"
    both_failed: bool = False
    base_labels: tuple[str, ...] = ()
    cand_labels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "base_run_id": self.base_run_id,
            "cand_run_id": self.cand_run_id,
            "base_verdict": self.base_verdict,
            "cand_verdict": self.cand_verdict,
            "direction": self.direction,
            "recommendation": self.recommendation,
            "reason": self.reason,
            "quality_delta": self.quality_delta,
            "both_failed": self.both_failed,
            "base_labels": list(self.base_labels),
            "cand_labels": list(self.cand_labels),
        }


def _make_comparison(
    base: PackAssessment,
    cand: PackAssessment,
    *,
    direction: str,
    recommendation: str,
    reason: str,
    both_failed: bool = False,
) -> PackComparison:
    """统一构造比较结果（把重复字段收在一处，避免散落的字典展开）。"""
    return PackComparison(
        case_id=base.case_id,
        base_run_id=base.run_id,
        cand_run_id=cand.run_id,
        base_verdict=base.verdict.value,
        cand_verdict=cand.verdict.value,
        direction=direction,
        recommendation=recommendation,
        reason=reason,
        quality_delta=round(cand.quality_score - base.quality_score, 1),
        both_failed=both_failed,
        base_labels=tuple(base.labels),
        cand_labels=tuple(cand.labels),
    )


def compare_pack_assessments(base: PackAssessment, cand: PackAssessment) -> PackComparison:
    """比较两个版本在**同一规则包**下的判定（三态优先，任一侧 UNKNOWN 不下版本结论）。"""
    delta = round(cand.quality_score - base.quality_score, 1)
    if base.is_unknown or cand.is_unknown:
        sides = []
        if base.is_unknown:
            sides.append("基线版")
        if cand.is_unknown:
            sides.append("候选版")
        return _make_comparison(
            base, cand,
            direction="insufficient_evidence",
            recommendation="investigate",
            reason="、".join(sides) + "为 UNKNOWN（证据不足），本 Case 不产生改善/退化结论。",
        )
    base_ok = base.verdict is Verdict.PASS
    cand_ok = cand.verdict is Verdict.PASS
    if base_ok and not cand_ok:
        return _make_comparison(
            base, cand, direction="baseline_better", recommendation="defer",
            reason=f"基线版处理正确，候选版出现可证实违规：{', '.join(cand.labels) or '—'}。",
        )
    if cand_ok and not base_ok:
        return _make_comparison(
            base, cand, direction="candidate_better", recommendation="release",
            reason=f"基线版存在可证实违规（{', '.join(base.labels) or '—'}），候选版处理正确。",
        )
    if base_ok and cand_ok:
        if abs(delta) < 0.5:
            return _make_comparison(
                base, cand, direction="equal", recommendation="release",
                reason="两版均处理正确且质量分一致，本 Case 无差异。",
            )
        winner = "候选版" if delta > 0 else "基线版"
        direction = "candidate_better" if delta > 0 else "baseline_better"
        return _make_comparison(
            base, cand, direction=direction,
            recommendation=_DIRECTION_RECOMMENDATION[direction],
            reason=f"两版均处理正确，但{winner}质量分更高（Δ={delta:+.1f}）。",
        )
    # 两版都失败
    if base.labels == cand.labels:
        return _make_comparison(
            base, cand, direction="equal", recommendation="investigate", both_failed=True,
            reason=f"两版在同一组规则上失败（{', '.join(base.labels) or '—'}），既非改善也非退化。",
        )
    if abs(delta) < 0.5:
        # 审计 D-05：两版都失败且扣分相同 → 不能凭 delta 正负说"候选退化"。
        return _make_comparison(
            base, cand, direction="equal", recommendation="investigate", both_failed=True,
            reason=(
                f"两版都失败且扣分相同（Δ={delta:+.1f}）：基线 {base.labels} → 候选 {cand.labels}；"
                "错误构成不同但严重度相同，不能据此判定谁更好。"
            ),
        )
    direction = "candidate_better" if delta > 0 else "baseline_better"
    return _make_comparison(
        base, cand, direction=direction, recommendation="investigate", both_failed=True,
        reason=(
            f"两版均未通过：基线 {base.labels} → 候选 {cand.labels}（Δ={delta:+.1f}）；"
            "错误构成发生变化，但两版都不满足发布条件。"
        ),
    )


@dataclass
class PackSummary:
    """一批 Case 的聚合结果（含分子/分母，便于报告直接引用）。"""

    comparisons: list[PackComparison] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.comparisons)

    def _count(self, direction: str) -> int:
        return sum(1 for item in self.comparisons if item.direction == direction)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "candidate_better": self._count("candidate_better"),
            "baseline_better": self._count("baseline_better"),
            "equal": self._count("equal"),
            "insufficient_evidence": self._count("insufficient_evidence"),
            "both_failed": sum(1 for item in self.comparisons if item.both_failed),
        }

    @property
    def recommendation(self) -> tuple[str, str]:
        """聚合建议：与开票报告同口径（同错不得当成"无差异"，有同错不得直接 release）。"""
        counts = self.counts
        improved = counts["candidate_better"]
        regressed = counts["baseline_better"]
        equal = counts["equal"]
        unknown = counts["insufficient_evidence"]
        both_failed = counts["both_failed"]
        if not self.comparisons:
            return "investigate", "本批没有可比较的 Case。"
        if regressed and not improved:
            return "defer", f"候选版在 {regressed} 个 Case 上退化，不具备发布条件。"
        if improved and not regressed and not unknown and not both_failed:
            return "release", (
                f"候选版在 {improved}/{self.total} 个 Case 上改善且无退化、无同错、无证据不足，可以灰度发布并继续观察。"
            )
        if improved and (regressed or both_failed):
            return "defer", (
                f"候选版改善 {improved} 个，但另有 {regressed} 个退化、{both_failed} 个两版同错；"
                "先修复同错与退化再评估，暂不具备 release 条件。"
            )
        if unknown == self.total:
            return "investigate", f"{total_text(self.total)}个 Case 全部证据不足（UNKNOWN），无法形成版本结论。"
        if unknown:
            return "gray", (
                f"改善 {improved}、退化 {regressed}、同错 {both_failed}、证据不足 {unknown}；"
                "结论强度受限，建议先补齐证据。"
            )
        if equal == self.total:
            return "investigate", f"{self.total} 个 Case 无差异（全部为两版同错或同质结果），需按错误类型排查。"
        # 审计 P4（D-11）：这里原有一条"属混合变化"的 gray 分支，但在上面的判定
        # 全部穷尽后不可达（direction 只取四值且计数恒等于 total）。删掉死分支，
        # 改为显式校验"计数分区完整"——将来新增 direction 时会在这里暴露，而不是静默落进文案。
        raise AssertionError(
            f"方向计数未覆盖全部 Case：改善 {improved} + 退化 {regressed} + 无差异 {equal} "
            f"!= 总数 {self.total}（新增 direction 时需同步本方法）"
        )

    def as_dict(self) -> dict[str, Any]:
        recommendation, reason = self.recommendation
        return {
            "total": self.total,
            "counts": self.counts,
            "recommendation": recommendation,
            "reason": reason,
            "cases": [item.as_dict() for item in self.comparisons],
        }


def total_text(value: int) -> str:
    """中文计数的小工具（报告文案里"3 个"而不是"3个"）。"""
    return f"{value} "


def summarise_pack(comparisons: Sequence[PackComparison]) -> PackSummary:
    return PackSummary(comparisons=list(comparisons))


def render_pack_report(
    pack_id: str,
    summary: PackSummary,
    *,
    scenario: str = "",
    pack_version: str = "",
) -> str:
    """渲染"规则包无关"的版本评测报告（Markdown）。

    报告只陈述本批自造样本上的配对结果，不声称泛化能力。
    """
    recommendation, reason = summary.recommendation
    counts = summary.counts
    lines = [
        f"# 规则包版本评测报告（`{pack_id}`）",
        "",
        f"- 场景：{scenario or '—'}"
        + (f"　规则包版本：`{pack_version}`" if pack_version else ""),
        f"- Case 数：{summary.total}",
        f"- 方向分布：改善 {counts['candidate_better']}、退化 {counts['baseline_better']}、"
        f"无差异 {counts['equal']}、证据不足 {counts['insufficient_evidence']}（其中两版同错 {counts['both_failed']}）",
        f"- **聚合建议：`{recommendation}`** — {reason}",
        "",
        "## 逐 Case 对比",
        "",
        "| Case | 基线 | 候选 | 方向 | 单 Case 建议 | 质量分变化 |",
        "|---|---|---|---|---|---|",
    ]
    for item in summary.comparisons:
        lines.append(
            f"| `{item.case_id}` | {item.base_verdict} | {item.cand_verdict} | "
            f"`{item.direction}` | `{item.recommendation}` | {item.quality_delta:+.1f} |"
        )
    lines += [
        "",
        "## 口径说明",
        "",
        "- 三态优先：任一侧 `UNKNOWN`（证据不足）→ 本 Case 不产生改善/退化结论，单列统计。",
        "- \"两版同错\"单列，不得当成\"无差异\"；存在同错时聚合建议不得为 `release`。",
        "- 方向与建议的判定口径与开票规则包（`evaluation.compare_versions`）一致。",
        "- 本报告的样本为自造场景，只用于演示跨业务规则包的统一评测与对比能力，不代表真实业务泛化准确率。",
        "",
    ]
    return "\n".join(lines)


def compare_case_pair(
    pack_id: str,
    case: Any,
    base_run: Any,
    cand_run: Any,
    *,
    evaluate_case: Any = None,
) -> PackComparison:
    """便捷入口：同一 Case 的两条运行 → 比较结果（默认走 `evaluate_case_with`）。"""
    if evaluate_case is None:
        from .rulepacks import evaluate_case_with as evaluate_case  # 局部导入避免循环

    base = evaluate_case(pack_id, case, base_run)
    cand = evaluate_case(pack_id, case, cand_run)
    return compare_pack_assessments(base, cand)
