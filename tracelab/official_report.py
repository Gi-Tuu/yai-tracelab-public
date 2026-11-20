"""按官方《版本评测报告模板》生成的报告。

模板共 7 节，本模块逐节对齐，不再自创结构：
1. 报告信息　2. 执行摘要　3. 指标口径与总体结果　4. 场景差异
5. 典型 Bad Case 与证据链　6. 发布建议与风险边界　7. 改进和回归 Case

与综合报告的区别：这里**基线版 A 与候选版 B 分列**，并且明确写出每项指标的分母。
模板没有规定的算法细节不写进这里，避免把内部实现当成评审口径。
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .attribution import downstream_text
from .contracts import Verdict
from .evaluation import EvaluationResult, Metrics, summarise
from .reports import (
    ab_pair,
    aggregate_recommendation,
    comparable_directions,
    md_cell,
    md_inline,
)

#: 官方模板给定的 5 个场景簇（顺序也照抄，便于评审对照）
SCENARIO_CLUSTERS: tuple[tuple[str, frozenset[str]], ...] = (
    ("金额口径与计算", frozenset({"AMOUNT_BASIS", "AMOUNT_CALCULATION"})),
    ("字段缺失与客户/商品匹配", frozenset({"FIELD_FABRICATION", "ENTITY_RESOLUTION", "PRODUCT_MATCH"})),
    ("确认、取消与重复风险", frozenset({"CONFIRMATION_BYPASS", "DUPLICATE_CONTROL", "INTENT_CLASSIFICATION"})),
    ("工具异常、回调与重试", frozenset({"TOOL_ERROR_HANDLING", "CALLBACK_MISINTERPRETATION", "TOOL_ARGUMENT_ERROR"})),
    ("批量、多轮及其他", frozenset({"MULTI_ITEM_OMISSION", "CONTEXT_UPDATE", "INSUFFICIENT_EVIDENCE"})),
)

RECOMMENDATION_TEXT = {
    "release": "发布",
    "gray": "灰度",
    "defer": "暂缓",
    "investigate": "需进一步调查",
}

#: 模板点名的"高风险流程违规"：未确认提交、重复风险忽略、取消后继续提交
HIGH_RISK_LABELS = frozenset({"CONFIRMATION_BYPASS", "DUPLICATE_CONTROL"})


def code_commit() -> str:
    """代码提交标识：优先环境变量（CI 注入），否则尝试读 git，最后 unknown。"""
    env = os.environ.get("TRACELAB_COMMIT")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


@dataclass
class VersionStats:
    version: str
    runs: int = 0
    correct: int = 0
    invoice_created: int = 0
    high_risk_violations: int = 0
    quality_sum: float = 0.0
    quality_scored_runs: int = 0
    latency_sum: int = 0
    token_sum: int = 0
    abstained: int = 0

    @property
    def evaluated(self) -> int:
        """可评估 run = 观测 run − UNKNOWN。

        这是**正确率与平均质量分的唯一分母**：UNKNOWN 是"评测器没能判断"，
        进分母等于把它算成判错（C-04 / C-11）。
        """
        return self.runs - self.abstained

    @property
    def avg_quality(self) -> float | None:
        # 分母排除 UNKNOWN：只对真正打过分（非 UNKNOWN）的运行求平均
        return round(self.quality_sum / self.quality_scored_runs, 1) if self.quality_scored_runs else None

    @property
    def avg_latency(self) -> int | None:
        return round(self.latency_sum / self.runs) if self.runs else None

    @property
    def avg_tokens(self) -> int | None:
        return round(self.token_sum / self.runs) if self.runs else None

    @property
    def accuracy(self) -> float | None:
        return round(self.correct / self.evaluated, 4) if self.evaluated else None

    @property
    def invoice_rate(self) -> float | None:
        return round(self.invoice_created / self.runs, 4) if self.runs else None


def _collect_stats(result: EvaluationResult) -> tuple[VersionStats, VersionStats]:
    base = VersionStats("基线版 A")
    cand = VersionStats("候选版 B")
    for cr in result.results:
        if not _ab_pair(cr):
            # 审计 R4-02：只有真正的 A/B 对才进 A/B 分母。
            # 旧实现只过滤 <2 条运行，3-run Case 会被拆成"首/末两条"当成一对。
            continue
        for stats, a in ((base, cr.base), (cand, cr.cand)):
            if a is None:  # 理论上不会发生：上面已过滤 <2 条运行的 Case
                continue
            run = cr.case.run(a.run_id)
            usage: dict[str, Any] = dict(run.resource_usage) if run else {}
            stats.runs += 1
            stats.correct += int(a.verdict is Verdict.PASS)
            stats.invoice_created += int(a.business_invoice_created)
            stats.high_risk_violations += sum(1 for v in a.violations if v.label in HIGH_RISK_LABELS)
            if a.is_unknown:
                # C-11：UNKNOWN 的质量分是"没判出来"，不能进平均质量分
                stats.abstained += 1
            else:
                stats.quality_sum += a.quality_score
                stats.quality_scored_runs += 1
            stats.latency_sum += int(usage.get("total_latency_ms") or 0)
            stats.token_sum += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return base, cand


def _cluster_of(result_case: Any) -> str:
    labels = {
        v.label
        for a in result_case.assessments
        for v in a.violations
    }
    for name, bucket in SCENARIO_CLUSTERS:
        if labels & bucket:
            return name
    return "批量、多轮及其他"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _score(value: float | None) -> str:
    """质量分是 0–100 的分数，不是比例，不能套百分比格式化。"""
    return "—" if value is None else f"{value:g}"


def _delta(a: float | None, b: float | None, fmt: str = "{:+.1f}") -> str:
    if a is None or b is None:
        return "—"
    return fmt.format(b - a)


def _ab_pair(case_result: Any) -> bool:
    """兼容别名：唯一实现在 reports.ab_pair（审计 R4-02）。"""
    return ab_pair(case_result)


def _comparable_directions(result: EvaluationResult) -> dict[str, int]:
    """兼容别名：唯一实现在 reports.comparable_directions。"""
    return comparable_directions(result)


def _packed_runs(dataset_sha256: str | None = None) -> set[tuple[str, str]]:
    """回归快照里已登记的 (case_id, run_id)（§7 用它区分"加入"与"已在快照中"）。

    审计 F5-07：必须校验快照记录的数据集哈希与当前评测数据一致；否则对"同 ID 但
    内容已改"的数据也会声称"已在快照中"，属于跨环境误导。
    """
    candidates = (
        Path("regression/invoice-pack/regression-pack.json"),
        Path(__file__).resolve().parents[1] / "regression/invoice-pack/regression-pack.json",
    )
    for path in candidates:
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        created = payload.get("created_from") or {}
        if not dataset_sha256:
            continue  # 无法校验数据集身份时不得声称"已在快照中"
        if str(created.get("dataset_sha256") or "") != dataset_sha256:
            continue
        runs = {
            (str(item.get("case_id")), str(item.get("run_id")))
            for item in payload.get("expectations", [])
            if isinstance(item, dict)
        }
        if runs:
            return runs
    return set()


def _exec_summary(result: EvaluationResult, metrics: Metrics | None) -> list[str]:
    summary = summarise(result)
    dirs = _comparable_directions(result)
    unpaired = [cr for cr in result.results if not _ab_pair(cr)]
    recommendation, _ = aggregate_recommendation(result)
    improved = dirs.get("candidate_better", 0)
    regressed = dirs.get("baseline_better", 0)
    equal = dirs.get("equal", 0)
    insufficient = dirs.get("insufficient_evidence", 0)
    both_fail = int(summary.get("equal_both_fail", 0) or 0)

    risk_cases = [
        cr for cr in result.results
        if any(v.label in HIGH_RISK_LABELS for a in cr.assessments for v in a.violations)
    ]
    top_risk = risk_cases[0].case.case_id if risk_cases else None
    risk_text = (
        f"最高风险集中在风控门禁（如 {md_inline(top_risk)}），共 {len(risk_cases)} 个 Case 出现未确认提交或重复风险未阻断"
        if risk_cases else "本批未观测到风控门禁类违规"
    )

    lines = [
        f"候选版在 {improved} 个 Case 上改善、{regressed} 个 Case 上退化、{equal} 个无明显差异、"
        f"{insufficient} 个证据不足，整体建议为「{RECOMMENDATION_TEXT.get(recommendation, recommendation)}」。",
    ]
    if both_fail:
        lines.append(
            f"其中 {both_fail} 个 Case 属于「两版同错」（错误构成相同、方向记为 equal）："
            "它们不能当作「无差异」，也不构成候选版可发布的证据。"
        )
    non_pair = [cr.case.case_id for cr in unpaired if cr.assessments]
    if non_pair:
        lines.append(
            f"另有 {len(non_pair)} 个 Case 的运行数不是 2（{'、'.join(md_inline(c) for c in non_pair[:5])}），"
            "不参与成对比较；它们与「证据不足」是两回事。"
        )
    no_run = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run:
        lines.append(
            f"注意：{'、'.join(md_inline(c) for c in no_run)} 该 Case 无可评估运行（runs 为空），已从全部指标与版本比较中排除。"
        )
    lines.extend([
        f"{risk_text}。",
        "本结论基于成对比较（同一 Case 的两条轨迹），"
        "单个 Case 的差异不作为整体版本判断依据。",
    ])
    if metrics and metrics.values.get("evidence_id_validity") is not None:
        lines.append(
            f"证据链抽检：证据事件有效性 {_pct(metrics.values.get('evidence_id_validity'))}，"
            f"首因事件 Exact Match {_pct(metrics.values.get('first_fault_event_exact_match'))}（有标注子集）。"
        )
    lines.append(
        "证据局限：数据为赛事方提供的合成沙箱数据；公开训练集带参考标注，"
        "无标注验证集只能做规则级复核，因此不外推准确率。"
    )
    return lines


def build_official_report(
    result: EvaluationResult,
    metrics: Metrics | None = None,
    secondary: dict[str, Any] | None = None,
) -> str:
    """按官方 7 节模板生成报告（编排层：准备公共变量 → 逐节追加 → 拼接）。"""
    manifest = result.manifest
    summary = summarise(result)
    base, cand = _collect_stats(result)
    recommendation, reason = aggregate_recommendation(result)

    lines: list[str] = []
    _cover(lines)
    _section_report_info(lines, result, manifest)
    _section_summary(lines, result, metrics)
    _section_metrics(lines, result, base, cand, summary, secondary)
    _section_scenarios(lines, result)
    _section_cases(lines, result)
    _section_release(lines, result, recommendation, reason)
    _section_regression_cases(lines, result)
    return "\n".join(lines)


def _cover(lines: list[str]) -> None:
    add = lines.append
    add("# AI 开票 Agent A/B 版本评测报告")
    add("")
    add("> 按赛事《版本评测报告模板》组织。所有数字由 `python -m tracelab evaluate` 实际产出，"
        "每条判断均可按事件编号回查原始轨迹。")
    add("")
    add("> **数据性质（请先读）**：本报告的六项方法指标在**公开训练集（含参考标注）**上计算，"
        "属**拟合值**，用于验证评测链路与规则，**不代表隐藏集泛化能力**；"
        "报告的逐 Case 结论只由轨迹证据推出，未使用大模型裁决。"
        "变形实验与依据记录演示使用**自造样本**，详见 `docs/g6-method.md`、`docs/g6-evidence.md`。")
    add("")


def _section_report_info(lines: list[str], result: EvaluationResult, manifest: Any) -> None:
    add = lines.append
    # ── 1. 报告信息 ────────────────────────────────────────────────
    add("## 1. 报告信息")
    add("")
    paired = sum(1 for cr in result.results if _ab_pair(cr))
    add(f"- 评测程序版本：`{manifest['tracelab_evaluator_version']}`　规则集 `{manifest['rulepack_version']}`　"
        f"代码提交标识 `{code_commit()}`")
    add(f"- 数据包版本：`{manifest['dataset']['dataset_version']}`"
        f"（文件 `{manifest['dataset']['source']}`，SHA-256 `{manifest['dataset']['sha256']}`）")
    add(f"- 评测时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    add(f"- 待测 Case 数：{manifest['dataset']['cases']}；有效 A/B 成对 Case 数：{paired}")
    add(f"- 运行环境与依赖：Python 3.11+，**零第三方运行时依赖**；Trace IR `{manifest['trace_ir_version']}`，"
        f"适配器 `{manifest['adapter']['name']}@{manifest['adapter']['version']}`")
    add("- 评分方法说明：见 `docs/method.md`（规则清单、扣分口径、首因与传播判定、不确定性策略）")
    reading = manifest["integrity"].get("reading") or {}
    add(f"- 读取完整性：总行 {reading.get('lines_total')} / 解析 {reading.get('cases_parsed')} / "
        f"跳过 {reading.get('skipped')} / 截断 {reading.get('truncated')}"
        f"（{'完整' if manifest['integrity'].get('complete') else '**部分评测**'}）")
    add("")


def _section_summary(lines: list[str], result: EvaluationResult, metrics: Metrics | None) -> None:
    add = lines.append
    # ── 2. 执行摘要 ────────────────────────────────────────────────
    add("## 2. 执行摘要")
    add("")
    for sentence in _exec_summary(result, metrics):
        add(sentence)
    add("")


def _section_metrics(
    lines: list[str], result: EvaluationResult, base: Any, cand: Any,
    summary: Mapping[str, Any], secondary: dict[str, Any] | None,
) -> None:
    add = lines.append
    # ── 3. 指标口径与总体结果 ──────────────────────────────────────
    add("## 3. 指标口径与总体结果")
    add("")
    add("先定义分母再填结果。`agent_task_success` 与 `business_invoice_created` 分别统计；"
        "正确等待确认或正确阻断风险时，前者可能为真、后者为假。时延和 Token 均为题目中的模拟值。")
    add("")
    add("| 指标 | 口径 / 分母 | 基线版 A | 候选版 B | 差值 | 说明 |")
    add("|---|---|---:|---:|---:|---|")
    add(f"| Agent 处理正确率 | 正确处理的 run / **可评估 run**（A {base.evaluated}，B {cand.evaluated}；"
        f"UNKNOWN 不进分母） | {_pct(base.accuracy)} "
        f"| {_pct(cand.accuracy)} | {_delta(base.accuracy, cand.accuracy, '{:+.1%}')} | 业务规则逐条校验后的结论 |")
    add(f"| 实际发票生成率 | 已生成发票的 run / 观测 run（A {base.runs}，B {cand.runs}，含不可评估） "
        f"| {_pct(base.invoice_rate)} "
        f"| {_pct(cand.invoice_rate)} | {_delta(base.invoice_rate, cand.invoice_rate, '{:+.1%}')} "
        f"| 不可直接当作质量分 |")
    add(f"| 高风险流程违规数 | 未确认提交、重复风险忽略等（分母 A {base.runs}，B {cand.runs}） "
        f"| {base.high_risk_violations} "
        f"| {cand.high_risk_violations} | {cand.high_risk_violations - base.high_risk_violations:+d} "
        f"| 命中 `CONFIRMATION_BYPASS` / `DUPLICATE_CONTROL` |")
    add(f"| 平均质量分 | `quality_score` 算术平均 / **可评估 run**"
        f"（A {base.quality_scored_runs}，B {cand.quality_scored_runs}；分母排除 UNKNOWN） "
        f"| {_score(base.avg_quality)} | {_score(cand.avg_quality)} "
        f"| {_delta(base.avg_quality, cand.avg_quality)} | 100 分起按可证实问题扣分 |")
    add(f"| 平均耗时 | `resource_usage.total_latency_ms`（分母 A {base.runs}，B {cand.runs}） | {base.avg_latency} | {cand.avg_latency} "
        f"| {_delta(base.avg_latency, cand.avg_latency, '{:+.0f}')} | 毫秒（模拟值） |")
    add(f"| 平均 Token | 输入与输出 Token 之和（分母 A {base.runs}，B {cand.runs}） | {base.avg_tokens} | {cand.avg_tokens} "
        f"| {_delta(base.avg_tokens, cand.avg_tokens, '{:+.0f}')} | 模拟数据 |")
    add(f"| 证据不足数（不可评估） | 单独计数，**不进入任何正确率 / 质量分分母** "
        f"| {base.abstained} | {cand.abstained} "
        f"| {cand.abstained - base.abstained:+d} | 输出 `INSUFFICIENT_EVIDENCE`，不是「判错」 |")
    add("")

    dirs = _comparable_directions(result)
    add(f"**成对比较**：候选版提升 {dirs.get('candidate_better', 0)} 个 Case，"
        f"退化 {dirs.get('baseline_better', 0)} 个 Case，"
        f"无明显差异 {dirs.get('equal', 0)} 个 Case，"
        f"证据不足 {dirs.get('insufficient_evidence', 0)} 个 Case。")
    _unpaired = [
        cr.case.case_id for cr in result.results
        if cr.assessments and not _ab_pair(cr)
    ]
    if _unpaired:
        add(f"**不可配对（runs≠2）**：{len(_unpaired)} 个 Case 不参与成对比较"
            f"（{'、'.join(md_inline(c) for c in _unpaired[:5])}）；"
            "它们既不算改善/退化，也不算「证据不足（UNKNOWN）」。")
    add("")
    both_fail = int(summary.get("equal_both_fail", 0) or 0)
    if both_fail:
        add(f"其中 {both_fail} 个 Case 两版同错（方向记为 `equal`，但两版都判错）："
            "它们**不构成「无明显差异」**，也不支持发布结论。")
        add("")
    add("判定规则：先比 `agent_task_success`（一方正确、一方错误 → 改善/退化）；"
        "两方都正确时比 `quality_score`（差 <0.5 判无差异）；两方都错误时按错误构成是否一致判定，"
        "构成不同则按质量分差定方向但标记需调查。"
        "任一版本 UNKNOWN 一律记 `insufficient_evidence`，既不算改善也不算退化。")
    add("")

    # ── 3.1 补充指标（含不可评估项）──────────────────────────────
    if secondary:
        from .secondary import render_markdown_table

        add("### 3.1 补充指标与不可评估项")
        add("")
        lines.extend(render_markdown_table(secondary))
        add("")
        add(f"> {secondary.get('note', '')}")
        add("")
        add("> 说明：标注为「不可评估」的指标是因为**缺少对照真值**，按赛题要求返回 null 并写明缺失项，"
            "不以 0 或 1 冒充结果。")
        add("")


def _section_scenarios(lines: list[str], result: EvaluationResult) -> None:
    add = lines.append
    # ── 4. 场景差异 ────────────────────────────────────────────────
    add("## 4. 场景差异")
    add("")
    add("| 场景簇 | Case 数 | 可评估 Case | A 正确数 | B 正确数 | 不可评估 | 主要变化 | 代表 Case |")
    add("|---|---:|---:|---:|---:|---:|---|---|")
    for name, _bucket in SCENARIO_CLUSTERS:
        cases = [cr for cr in result.results if _cluster_of(cr) == name]
        if not cases:
            add(f"| {name} | 0 | 0 | 0 | 0 | 0 | 未观测到 | — |")
            continue
        # 「主要变化」只来自 cr.comparison.direction（C-04）：
        # 旧实现用两侧布尔 agent_task_success 重算，UNKNOWN 会被算成"候选版退化"，
        # 于是同一份报告 §3 说"证据不足"、§4 说"退化"，自相矛盾。
        cluster_dirs = [cr.comparison.direction for cr in cases]
        improved = sum(1 for d in cluster_dirs if d == "candidate_better")
        regressed = sum(1 for d in cluster_dirs if d == "baseline_better")
        equal = sum(1 for d in cluster_dirs if d == "equal")
        # 审计 R4-02 残留：runs≠2 的 Case 也被标成 insufficient_evidence，但 §3 明确
        # 声明它们"不算证据不足"；§4 的「不可评估」列因此只统计**可配对且判不了**的 Case。
        insufficient = sum(
            1 for cr in cases
            if _ab_pair(cr) and cr.comparison.direction == "insufficient_evidence"
        )
        evaluable = [
            cr for cr in cases
            # 审计 F-19 / R2-09：只有真正的 A/B 对才计入两列；UNKNOWN 也不进。
            if _ab_pair(cr)
            and not cr.base.is_unknown and not cr.cand.is_unknown  # type: ignore[union-attr]
        ]
        a_ok = 0
        b_ok = 0
        for cr in evaluable:
            base_assess, cand_assess = cr.base, cr.cand
            if base_assess is None or cand_assess is None:  # 类型收窄：evaluable 已过滤
                continue
            a_ok += 1 if base_assess.agent_task_success else 0
            b_ok += 1 if cand_assess.agent_task_success else 0

        if improved and not regressed:
            change = f"候选版改善 {improved}"
        elif regressed and not improved:
            change = f"候选版退化 {regressed}"
        elif improved and regressed:
            change = f"改善 {improved} / 退化 {regressed}"
        elif insufficient == len(cases):
            change = "不可评估（证据不足）"
        elif insufficient:
            change = f"无明显差异 {equal}（另有 {insufficient} 个证据不足）"
        else:
            change = f"无明显差异 {equal}"
        labels = sorted({v.label for cr in cases for a in cr.assessments for v in a.violations})
        rep = cases[0].case.case_id
        add(f"| {name} | {len(cases)} | {len(evaluable)} | {a_ok} | {b_ok} | {insufficient} | {change}"
            f"{'（' + '、'.join(labels) + '）' if labels else ''} | {md_cell(rep)} |")
    add("")
    add("> 归类规则：一个 Case 若命中多个场景簇，按上表顺序归入**首个命中**的簇，"
        "因此每行 Case 数之和等于总 Case 数；括号内为簇内出现过的根因标签。")
    add("> 口径：「主要变化」只按 `version_comparison.direction` 聚合；"
        "`insufficient_evidence` 一律记为**不可评估**，既不算改善也不算退化，"
        "A/B 正确数也只在两侧都可评估的 Case 上统计。")
    add("")
    no_run_cases = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run_cases:
        add("> ⚠️ 该 Case 无可评估运行：" + "、".join(f"`{md_inline(c)}`" for c in no_run_cases)
            + "（runs 为空），已从正确数与方向统计中排除，需补数据后复测。")
        add("")


def _section_cases(lines: list[str], result: EvaluationResult) -> None:
    add = lines.append
    # ── 5. 典型 Bad Case 与证据链 ─────────────────────────────────
    add("## 5. 典型 Bad Case 与证据链")
    add("")
    regressions = [cr for cr in result.results if cr.comparison.direction == "baseline_better"]
    guards = [
        (cr, a, g)
        for cr in result.results for a in cr.assessments for g in a.guards
        if not a.agent_task_success or a.guards
    ]
    add("### 5.1 候选版退化示例")
    add("")
    if not regressions:
        add("本批数据未观测到候选版退化的 Case。")
    else:
        cr = regressions[0]
        a = cr.cand
        if a is None:  # 防御：理论上 direction=baseline_better 必然两侧都存在
            add("本批数据未观测到可展示的候选版退化运行。")
            add("")
        else:
            first = a.first_error
            guard_text = "是（工具正确拒绝）" if a.guards else "未触发工具拦截"
            add("| Case / run | 预期业务目标 | 首个错误事件 | 后续传播 | 工具是否正确拦截 | 证据事件 | 根因与置信度 |")
            add("|---|---|---|---|---|---|---|")
            add(f"| {md_cell(cr.case.case_id)} / `{md_cell(a.run_id)}` | "
                f"{cr.case.reference_result.get('acceptable_snapshot_state')}，"
                f"发票应存在={cr.case.reference_result.get('invoice_should_exist')} | "
                f"`{first.event_id if first else '—'}` | {downstream_text(a) or '未扩散'} | {guard_text} | "
                f"{', '.join(f'`{e}`' for e in a.evidence_ids)} | "
                f"{'; '.join(f'{v.label}({v.confidence:.1f})' for v in a.violations)} |")
            add("")
            add(f"说明：{first.explanation if first else '—'}")
    add("")

    add("### 5.2 保护性工具拦截示例")
    add("")
    if not guards:
        add("本批数据未观测到保护性工具拦截。")
    else:
        cr, a, g = guards[0]
        add("| Case / run | 拦截事件 | 错误码 | 为什么是正确拦截 | 对应的 Agent 侧根因 |")
        add("|---|---|---|---|---|")
        add(f"| {md_cell(cr.case.case_id)} / `{md_cell(a.run_id)}` | `{md_cell(g.event_id)}` | `{md_cell(g.code)}` | {md_cell(g.reason)} | "
            f"{'; '.join(a.labels) or '无'} |")
        add("")
        add("说明：工具拦截本身不计为故障；它反映的是 Agent 侧绕过了业务门禁，"
            "归因必须落到 Agent 的提交动作上。")
    add("")


def _section_release(
    lines: list[str], result: EvaluationResult, recommendation: str, reason: str,
) -> None:
    add = lines.append
    # ── 6. 发布建议与风险边界 ──────────────────────────────────────
    # 审计 R2-B-F-09：判定为 PASS/FAIL 但存在不确定性的运行必须在这份主报告里披露
    uncertain_rows = [(cr, a) for cr in result.results for a in cr.assessments if a.uncertainties]
    add("### 5.3 含不确定性的运行（判定成立，但结论有前提）")
    add("")
    if not uncertain_rows:
        add("本批没有带不确定性的运行：所有判定都在证据齐全的前提下给出。")
    else:
        add("| Case / Run | 判定 | 不确定性说明（首条） |")
        add("|---|---|---|")
        for cr, a in uncertain_rows[:12]:
            add(f"| {md_cell(cr.case.case_id)} / `{md_cell(a.run_id)}` | {a.verdict.value} "
                f"| {md_cell(a.uncertainties[0])} |")
        if len(uncertain_rows) > 12:
            add(f"| … | … | 其余 {len(uncertain_rows) - 12} 条见 results.json 的 `uncertainties` 字段 |")
    add("")

    add("## 6. 发布建议与风险边界")
    add("")
    add(f"- **建议**：{RECOMMENDATION_TEXT.get(recommendation, recommendation)}")
    add(f"- **主要依据**：{reason}")
    veto = [
        f"{md_cell(cr.case.case_id)}/{md_cell(a.run_id)}（{md_cell(v.label)} @ `{md_cell(v.event_id)}`）"
        for cr in result.results for a in cr.assessments for v in a.violations
        if v.label in HIGH_RISK_LABELS
    ]
    add(f"- **否决性风险**：{'；'.join(veto) if veto else '本批未观测到风控门禁类否决项'}")
    # 审计 R4 补充：单例权重按**可比较 A/B 对**算——runs≠2 的 Case 不参与方向判断，
    # 0-run 更是没有结论，都不应稀释"一个 Case 值多少个百分点"。
    sample = sum(1 for cr in result.results if ab_pair(cr)) or 0
    per_case_pp = f"{100.0 / sample:.1f}" if sample else "—"
    add(f"- **不确定性**：数据为合成沙箱；本批 {sample} 个可比较 A/B 对，单例权重约 {per_case_pp} 个百分点，"
        "公开训练集带标注但样本量小，"
        "无标注验证集无专家标注；因此不做统计推断，也不外推到隐藏测试集。")
    add("- **复测门槛**：修复后需同时满足 ① 回归包 `verify` 零回归 ② 高风险流程违规为 0 "
        "③ 训练集六项方法指标不下降。")
    add("")


def _section_regression_cases(lines: list[str], result: EvaluationResult) -> None:
    add = lines.append
    # ── 7. 改进和回归 Case ────────────────────────────────────────
    add("## 7. 改进和回归 Case")
    add("")
    add("| 问题 | 建议修复位置 | 应补充的回归 Case | 预期可验证行为 |")
    add("|---|---|---|---|")
    seen: set[str] = set()
    rows = 0
    all_labels = sorted({v.label for cr in result.results for a in cr.assessments for v in a.violations})
    packed = _packed_runs((result.manifest.get("dataset") or {}).get("sha256"))
    for cr in result.results:
        for a in cr.assessments:
            for v in a.violations:
                if v.label in seen or rows >= 8:
                    continue
                seen.add(v.label)
                rows += 1
                key = (cr.case.case_id, a.run_id)
                if key in packed:
                    # 审计 R4-09：该 run 已在快照里，不能再建议"加入"（会误导评审以为回归包未覆盖）。
                    suggestion = (f"`{md_cell(cr.case.case_id)}/{md_cell(a.run_id)}` 已在 `regression/invoice-pack` "
                                  "（快照已登记该 run），复测断言**保持当前方向**即可")
                    expected = "复测时该 Case 结论与该断言一致（无需再登记）"
                else:
                    suggestion = (f"`{md_cell(cr.case.case_id)}/{md_cell(a.run_id)}` 加入 `regression/invoice-pack`")
                    expected = "复测时该 Case 应变为 improved，且不出现 regressed"
                add(f"| {v.label}：{v.explanation[:60]}… | 规则 `{v.rule_id}` 覆盖的业务环节 | "
                    f"{suggestion} | {expected} |")
    if rows == 0:
        add("| 本批无可证实问题 | — | — | — |")
    if len(all_labels) > rows:
        # P3：截断必须写清"仅展示前 N / 共 M"
        add("")
        add(f"> 仅展示前 {rows} 类问题 / 共 {len(all_labels)} 类（其余问题按同样方式补回归 Case）。")
    add("")
    add("---")
    add("")
    add("报告不含真实客户信息、生产接口地址或密钥；全部示例均来自赛事方提供的合成资料包。")
    add("")


def write_official_report(
    result: EvaluationResult,
    path: Path,
    metrics: Metrics | None = None,
    secondary: dict[str, Any] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_official_report(result, metrics, secondary), encoding="utf-8")
    return path



