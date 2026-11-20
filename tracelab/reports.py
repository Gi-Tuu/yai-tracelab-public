"""报告层：机器可读 JSON + 面向人阅读的 Markdown。

报告立场：业务方看结论，研发看证据链，产品看发布建议。
所有结论都必须能顺藤摸到原始事件编号。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .attribution import business_result_text, downstream_text, process_text, suggestions
from .contracts import Verdict
from .evaluation import EvaluationResult, Metrics, summarise

SEVERITY_ORDER = {
    "RISK_CONTROL": 0, "BUSINESS_RESULT": 1, "TOOL_CALL": 2, "PROCESS": 3,
}


def _worst(cr) -> tuple[int, float]:
    dims = [SEVERITY_ORDER.get(v.dimension, 9) for a in cr.assessments for v in a.violations]
    worst = min(dims) if dims else 9
    return worst, -cr.comparison.quality_delta


def ab_pair(case_result: Any) -> bool:
    """该 Case 是否构成**可比较的 A/B 对**（与官方报告共用同一判定）。

    审计 R4-02/R4-03：报告层此前有 4 套方向计数（summarise 全量、官方 §2/§3/§4、
    受众报告），runs≠2 的 Case 在不同地方被当成"成对"或"证据不足"，同一份数据
    会给出互相否定的数字。这里提供唯一判定，各报告统一调用。
    """
    return (
        bool(case_result.case.is_paired)
        and len(case_result.assessments) == 2
        and case_result.base is not None
        and case_result.cand is not None
    )


def comparable_directions(result: EvaluationResult) -> dict[str, int]:
    """只统计可比较 A/B 对的方向分布（runs≠2 / 0-run 单独计数）。"""
    dirs: dict[str, int] = {}
    for cr in result.results:
        if not ab_pair(cr):
            continue
        dirs[cr.comparison.direction] = dirs.get(cr.comparison.direction, 0) + 1
    return dirs


def _integrity_gaps(result: EvaluationResult) -> list[str]:
    """读出 A（读取）/ B（契约）/ C（评测）三项完整性缺口。

    读不到 manifest/integrity 时视为"无缺口"：本函数只做**额外的否决**，
    不对没有完整性信息的历史调用方制造新的降级。
    """
    manifest = getattr(result, "manifest", None) or {}
    integrity = manifest.get("integrity") if isinstance(manifest, Mapping) else None
    if not isinstance(integrity, Mapping):
        return []

    def _failed(key: str) -> bool:
        return key in integrity and not integrity[key]

    gaps: list[str] = []
    if _failed("read_complete"):
        gaps.append("A（read_complete）：数据未读全（坏行/编码/截断）")
    if _failed("schema_valid"):
        gaps.append("B（schema_valid）：输入不符合数据契约")
    if _failed("evaluation_complete"):
        gaps.append("C（evaluation_complete）：存在降级（R901）或未完成评测的运行")
    return gaps


def md_cell(value: Any) -> str:
    """把任意数据字段安全放进 Markdown 表格单元格。

    审计 F-18：数据里的 `|` 会把表格撑破、换行可以伪造标题或结论。表格内统一
    转义竖线、折叠换行；正文用 `md_inline`（去掉换行，避免伪造结构）。
    """
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def md_inline(value: Any) -> str:
    """把任意数据字段安全放进 Markdown 正文/标题（只折叠换行，不改内容）。"""
    text = "" if value is None else str(value)
    return text.replace("\r", " ").replace("\n", " ")


def event_ref(event_id: str | None, seq: int | None = None) -> str:
    """渲染事件坐标；拒判/无坐标时给可读说明，而不是字面量 `None`（审计 F-32）。"""
    if not event_id:
        return "—（拒判：无单一可定位事件）"
    if seq is not None and seq >= 10**9:
        return f"`{event_id}`"
    return f"`{event_id}`" + (f"（seq={seq}）" if seq is not None else "")


def _recommendation_from_directions(
    dirs: Mapping[str, int],
    unknown_runs: int,
    unknown_cases: int,
    *,
    both_fail: int = 0,
    both_pass: int = 0,
    both_failed_any: int = 0,
    task_success: int | None = None,
    investigate_cases: int = 0,
) -> tuple[str, str]:
    """仅按版本方向分布给出建议（UNKNOWN 会削弱结论强度）。

    `both_fail` = 方向为 equal 但**两版都错**的 Case 数（C-06）。旧口径只看
    equal 计数，会把"两版同错"当成"无差异"，从而在全失败批上给出 release/gray。
    这里把它单列：只要存在两版同错，就不允许 release。

    审计 F-04：`both_failed_any` = **任何方向**下两侧都 FAIL 的 Case 数；
    `task_success` = 真正判对（PASS）的运行数。两者共同保证：
    "候选版少扣点分"不能把"一个正确都没跑出来"的批次说成可以发布。
    """
    baseline_better = dirs.get("baseline_better", 0)
    candidate_better = dirs.get("candidate_better", 0)
    equal = dirs.get("equal", 0)
    all_failed = task_success is not None and task_success == 0

    unknown_note = ""
    if unknown_runs or unknown_cases:
        unknown_note = (
            f"另有 {unknown_runs} 条运行 / {unknown_cases} 个可配对 Case 判定为 UNKNOWN（证据不足或评测器未能完成判断），"
            "这些样本不参与改善/退化排名，因此结论强度受限。"
        )
    both_fail_note = (
        f"其中 {both_fail} 个 Case 两版同错（错误构成相同），不能当作「无差异」或「已改善」。"
        if both_fail else ""
    )

    if candidate_better == 0 and baseline_better > 0:
        return "defer", (
            f"候选版在 {baseline_better} 个 Case 上退化、{candidate_better} 个改善，不具备发布条件。"
            + both_fail_note + unknown_note
        )
    if baseline_better == 0 and candidate_better > 0:
        # 审计 F-04：全批一条都没判对时，"候选少扣点分"不构成能力改善。
        if all_failed:
            return "investigate", (
                f"候选版在 {candidate_better} 个 Case 上扣分更少，但**没有任何一条运行被判定为正确处理**"
                f"（task_success=0；另有 {both_failed_any} 个 Case 两版都失败）。"
                "这是「两版都不可用」而不是「候选版可发布」，需先修复再评估。"
                + unknown_note
            )
        if both_fail or both_failed_any:
            return "defer", (
                f"候选版在 {candidate_better} 个 Case 上改善且未观察到退化，但仍有 "
                f"{both_failed_any} 个 Case 两版都失败"
                + (f"，其中 {both_fail} 个 Case 两版同错（错误构成相同）" if both_fail else "")
                + "；存在未修复的失败场景，暂不具备 release 条件。"
                + unknown_note
            )
        if investigate_cases:
            return "gray", (
                f"候选版在 {candidate_better} 个 Case 上改善，但另有 {investigate_cases} 个 Case 的单 Case "
                "建议为 investigate（证据/口径需人工复核）；聚合结论不得强于逐 Case 建议，建议小范围灰度。"
                + unknown_note
            )
        if unknown_runs or unknown_cases:
            return "gray", (
                f"候选版在 {candidate_better} 个 Case 上改善且未观察到退化，但"
                f"{unknown_note} 建议先补齐证据或小范围灰度观察，不宜直接 release。"
            )
        return "release", f"候选版在 {candidate_better} 个 Case 上改善且无退化，可以灰度发布并继续观察未见场景。"
    if baseline_better and candidate_better:
        return "gray", (
            f"候选版在 {candidate_better} 个 Case 改善、{baseline_better} 个退化，属于混合变化；"
            "建议按退化 Case 的风险类型分批灰度，先修复 RISK_CONTROL 级问题。"
            + both_fail_note + unknown_note
        )
    if unknown_cases or unknown_runs:
        return "investigate", (
            f"{unknown_cases} 个 Case / {unknown_runs} 条运行证据不足（UNKNOWN），无法形成版本结论；"
            "需补齐证据或修复评测流程后复测。"
        )
    if equal:
        if both_fail:
            return "investigate", (
                f"{both_fail} 个 Case 两版同错（错误构成相同），不能算「无差异」："
                "两版都没有正确完成任务，说明问题在两版中共存，需按错误类型分别修复后复测。"
            )
        if both_pass:
            return "gray", (
                f"{equal} 个 Case 无差异（两版均处理正确且质量分相近），"
                "未观察到候选版相对基线的能力变化。"
            )
        return "gray", f"{equal} 个 Case 无差异，未观察到候选版相对基线的能力变化。"
    return "investigate", "样本不足以形成发布建议。"


def aggregate_recommendation(result: EvaluationResult) -> tuple[str, str]:
    """聚合发布建议。

    UNKNOWN（证据不足 / 评测器异常）会削弱结论强度：
    只要存在 UNKNOWN，就不能把"零退化 + 有改善"直接说成可以发布。
    "两版同错"（equal_both_fail>0）同样必须否决 release——它意味着两版在同一个
    坑里，不能因为方向被记成 equal 就当作无差异。

    此外，manifest.integrity 里任何一项不完整（A 读取 / B 契约 / C 评测），
    结论都**不得为 release**——数据或评测本身没走完时，"没有退化"不代表"可以发布"。
    """
    summary = summarise(result)
    dirs = summary["directions"]
    # 审计 R2-15：unknown_cases 只算"可配对但判 UNKNOWN"；不可配对单独处理
    unknown_cases = int(summary.get("unknown_cases_paired", dirs.get("insufficient_evidence", 0)) or 0)
    unpaired_cases = int(summary.get("unpaired_cases", 0) or 0)
    unknown_runs = int(summary.get("task_unknown", 0) or 0)
    both_fail = int(summary.get("equal_both_fail", 0) or 0)
    both_pass = int(summary.get("equal_both_pass", 0) or 0)
    # 审计 F-04：聚合建议不得强于事实——"全失败批次"与"逐 Case 建议 investigate"
    # 都必须否决 release。
    both_failed_any = int(summary.get("both_failed_cases", 0) or 0)
    investigate_cases = int(summary.get("investigate_cases", 0) or 0)
    # 缺省与 0 必须区分：调用方（含测试替身）若不提供 task_success，
    # 不能当成"零条正确"而把 release 误降为 investigate。
    task_success_raw = summary.get("task_success")
    task_success = None if task_success_raw is None else int(task_success_raw)

    recommendation, reason = _recommendation_from_directions(
        dirs, unknown_runs, unknown_cases,
        both_fail=both_fail, both_pass=both_pass,
        both_failed_any=both_failed_any, task_success=task_success,
        investigate_cases=investigate_cases,
    )
    if unpaired_cases and recommendation == "release":
        # 有 Case 连 A/B 对都凑不齐时，"无退化"不构成发布证据
        recommendation = "gray"
        reason += f" 另有 {unpaired_cases} 个 Case 无法配对（runs≠2），不参与对比，结论强度受限。"
    gaps = _integrity_gaps(result)
    if not gaps:
        return recommendation, reason

    gap_text = "；".join(gaps)
    if recommendation == "release":
        # A/B 是数据侧不可信（读不全 / 契约不合规）→ 必须重新取数复核，investigate；
        # C 是部分运行没判出来（与 UNKNOWN 同等）→ 只支持灰度，降为 gray。
        data_side = any(gap.startswith(("A", "B")) for gap in gaps)
        recommendation = "investigate" if data_side else "gray"
        reason = (
            f"完整性校验未通过（{gap_text}），本批评测不构成完整证据基础，不能给出 release：" + reason
        )
    else:
        reason = f"{reason} 另外，完整性校验未通过（{gap_text}），结论强度进一步受限。"
    return recommendation, reason


def build_markdown(result: EvaluationResult, metrics: Metrics | None = None) -> str:
    """综合版报告（编排层：准备公共变量 → 逐节追加 → 拼接）。"""
    summary = summarise(result)
    manifest = result.manifest
    recommendation, reason = aggregate_recommendation(result)

    lines: list[str] = []
    _md_cover(lines)
    _md_section_experiment(lines, result, manifest)
    _md_section_overview(lines, result, summary, recommendation, reason)
    _md_section_metrics(lines, metrics)
    _md_section_versions(lines, result)
    _md_section_bad_cases(lines, result)
    _md_section_labels(lines, result, summary)
    _md_section_limits(lines)
    return "\n".join(lines)


def _md_cover(lines: list[str]) -> None:
    add = lines.append
    add("# YAI TraceLab 版本评测报告")
    add("")
    add("> 证据驱动、业务规则感知的开票 Agent 轨迹评测。所有结论均可在原始轨迹中按事件编号复核。")
    add("")
    add("> **数据性质（请先读）**：指标数字在**公开训练集（含参考标注）**上计算，属**拟合值**，"
        "不代表隐藏集泛化能力；逐 Case 结论只由轨迹证据推出，未使用大模型裁决。"
        "变形实验（语义保持 / 业务变化 / 拒判）与依据记录演示均使用**自造样本**。")
    add("")


def _md_section_experiment(lines: list[str], result: EvaluationResult, manifest: Mapping[str, Any]) -> None:
    add = lines.append
    # ------------------------------------------------------------ 实验清单
    add("## 一、实验清单（可复现性）")
    add("")
    add("| 项目 | 值 |")
    add("|---|---|")
    add(f"| 评测器版本 | `{manifest['tracelab_evaluator_version']}` |")
    add(f"| Trace IR 版本 | `{manifest['trace_ir_version']}` |")
    add(f"| 适配器 | `{manifest['adapter']['name']}@{manifest['adapter']['version']}` |")
    add(f"| 规则集版本 | `{manifest['rulepack_version']}`（{len(manifest['rulepack_rules'])} 条规则） |")
    add(f"| 数据文件 | `{manifest['dataset']['source']}` |")
    add(f"| 数据 SHA-256 | `{manifest['dataset']['sha256']}` |")
    add(f"| 规模 | {manifest['dataset']['cases']} Case / {manifest['dataset']['runs']} 运行 |")
    reading = manifest["integrity"].get("reading") or {}
    if reading:
        add(
            f"| 读取完整性 | 总行 {reading.get('lines_total')} / 解析 {reading.get('cases_parsed')} / "
            f"跳过 {reading.get('skipped')} / 截断 {reading.get('truncated')} |"
        )
    integrity = manifest["integrity"]
    add(f"| A. 读取完整 read_complete | {'✅' if integrity.get('read_complete') else '❌ 文件未读全'} |")
    add(f"| B. 契约合规 schema_valid | {'✅' if integrity.get('schema_valid') else '❌ 存在不符合数据契约的输入'} |")
    add(f"| C. 评测完整 evaluation_complete | {'✅' if integrity.get('evaluation_complete') else '❌ 存在未完成评测的运行'} |")
    add(f"| 综合判定 | {'✅ 三项皆通过' if integrity.get('complete') else '⚠️ **部分评测，结论受限**'} |")
    add(f"| Agent 版本 | {', '.join(f'`{v}`' for v in manifest['agent_versions'])} |")
    add(f"| 完整性告警 | {manifest['integrity']['issues']} 项（错误 {manifest['integrity']['errors']} 项） |")
    add(
        f"| UNKNOWN / 降级运行 | {integrity.get('unknown_runs', 0)} / {integrity.get('degraded_runs', 0)} |"
    )
    add(f"| 结果哈希 | `{result.result_hash()[:16]}` … |")
    add(f"| 评测耗时 | {result.elapsed_ms} ms |")
    add("")


def _md_section_overview(
    lines: list[str], result: EvaluationResult, summary: Mapping[str, Any],
    recommendation: str, reason: str,
) -> None:
    add = lines.append
    # ------------------------------------------------------------ 总览
    add("## 二、总览")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---|")
    add(f"| Case 数 | {summary['cases']} |")
    add(f"| 运行数 | {summary['runs']} |")
    add(f"| Agent 处理正确 | {summary['task_success']} / "
        f"{summary['runs'] - summary['task_unknown']}（可评估运行；"
        f"UNKNOWN {summary['task_unknown']} 条不进分母、另计） |")
    add(f"| 实际问题生成发票 | {summary['invoice_created']} / {summary['runs']}（观测运行，含不可评估） |")
    # C-11：平均质量分分母排除 UNKNOWN，报告必须把分母写出来
    quality_shown = "—" if summary["avg_quality"] is None else summary["avg_quality"]
    add(f"| 平均质量分（分母 {summary.get('avg_quality_scored_runs', summary['runs'])} 条可评估运行，"
        f"排除 UNKNOWN {summary.get('avg_quality_excluded_unknown', 0)} 条） | {quality_shown} |")
    add(f"| 识别到的正常工具拦截 | {summary['guards_detected']} 次 |")
    add(f"| 证据不足拒判 | {summary['abstained']} 次 |")
    if summary.get("equal_both_fail"):
        add(f"| 两版同错（方向 equal 但双方都判错） | {summary['equal_both_fail']} 个 Case |")
    add("")
    comparable = comparable_directions(result)
    add("**版本方向分布（仅可比较 A/B 对）**："
        + ("，".join(f"`{k}` {v}" for k, v in comparable.items()) or "无"))
    unpaired_ids = [
        cr.case.case_id for cr in result.results if cr.assessments and not ab_pair(cr)
    ]
    if unpaired_ids:
        add(f"> 另有 {len(unpaired_ids)} 个 Case 运行数≠2（{'、'.join(f'`{c_}`' for c_ in unpaired_ids[:5])}），"
            "不参与成对比较，也不计入 `insufficient_evidence`。")
    add("")
    add(f"**发布建议：`{recommendation}`** — {reason}")
    add("")


def _md_section_metrics(lines: list[str], metrics: Metrics | None) -> None:
    add = lines.append
    # ------------------------------------------------------------ 指标
    add("## 三、方法校验指标（仅在有参考标注的数据上计算）")
    add("")
    if metrics and metrics.values:
        add("| 指标 | 值 |")
        add("|---|---|")
        for key, value in metrics.values.items():
            shown = "—" if value is None else (f"{value:.2%}" if isinstance(value, float) and value <= 1 else value)
            add(f"| `{key}` | {shown} |")
        add("")
        add("> 未提供标注的数据不计入上表；不使用隐藏集外推，也不自造官方权重。")
    else:
        add("本批数据未提供参考标注，因此不计算与标注对比的指标——**也不以自造基准冒充准确率**。")
        add("")
        add("可复核的替代证据：每条结论都给出规则 ID、首个错误事件编号与证据事件编号，"
            "可由人工按事件编号逐条核对；同时在公开训练标注上运行 `--labels` 得到可量化的方法校验结果。")
    add("")


def _md_section_versions(lines: list[str], result: EvaluationResult) -> None:
    add = lines.append
    # ------------------------------------------------------------ 版本对比
    add("## 四、逐 Case 版本对比")
    add("")
    add("| Case | 场景 | 基线 | 候选 | 方向 | 建议 | 质量分变化 |")
    add("|---|---|---|---|---|---|---|")
    def _mark(a) -> str:
        """三态渲染：UNKNOWN 不是判错，必须用 ⚠️ 证据不足（C-04）。

        0-run Case（`a is None`）同理不能用 ❌ 冒充"Agent 做错了"。
        """
        if a is None:
            return "— 无可评估运行"
        if a.is_unknown:
            return f"⚠️ 证据不足（{','.join(a.labels) or 'UNKNOWN'}）"
        return "✅ 正确" if a.agent_task_success else f"❌ {','.join(a.labels) or '失败'}"

    for cr in result.results:
        base, cand = cr.base, cr.cand
        add(
            f"| {md_cell(cr.case.case_id)} | {md_cell(cr.case.scenario)} | {_mark(base)} | {_mark(cand)} "
            f"| `{cr.comparison.direction}` | `{cr.comparison.release_recommendation}` "
            f"| {cr.comparison.quality_delta:+.1f} |"
        )
    add("")
    no_run_cases = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run_cases:
        add("> ⚠️ 该 Case 无可评估运行：" + "、".join(f"`{md_inline(c)}`" for c in no_run_cases)
            + "（runs 为空），已从版本对比与指标统计中排除，需补数据后复测。")
        add("")


def _md_section_bad_cases(lines: list[str], result: EvaluationResult) -> None:
    add = lines.append
    # ------------------------------------------------------------ Bad Case 证据链
    # 审计 R2-15：不可配对 Case 单列，不能混进"证据不足（UNKNOWN）"
    unpaired_rows = [cr for cr in result.results if cr.assessments and not cr.case.is_paired]
    if unpaired_rows:
        add("## 四之二、无法配对比较的 Case（runs≠2）")
        add("")
        add("| Case | 运行数 | 处理 |")
        add("|---|---|---|")
        for cr in unpaired_rows[:14]:
            add(f"| {md_cell(cr.case.case_id)} | {len(cr.assessments)} | 不参与成对比较，也不计入 UNKNOWN |")
        add("")

    # 审计 R2-B-F-09：判定为 PASS/FAIL 但存在不确定性的运行必须显式披露
    uncertain_rows = [
        (cr, a) for cr in result.results for a in cr.assessments if a.uncertainties
    ]
    if uncertain_rows:
        add("## 四之三、含不确定性的运行（判定仍成立，但结论有前提）")
        add("")
        add("| Case / Run | 判定 | 不确定性说明（首条） |")
        add("|---|---|---|")
        for cr, a in uncertain_rows[:14]:
            add(f"| {md_cell(cr.case.case_id)} / {md_cell(a.run_id)} | {a.verdict.value} "
                f"| {md_cell(a.uncertainties[0])} |")
        add("")

    add("## 五、Bad Case 证据链（按风险严重度排序）")
    add("")
    bad: list[Any] = []
    for cr in result.results:
        for a in cr.assessments:
            # 审计 C-02：UNKNOWN 是"判不出来"，不是"失败运行"。用布尔映射会把
            # 证据不足的运行混进 Bad Case 清单，必须按内部三态筛选。
            if a.verdict is Verdict.FAIL:
                bad.append((cr, a))
    # 严重度必须取该 run **所有**违规里最重的一维，不能只看第一条（P3）
    bad.sort(key=lambda item: (
        min((SEVERITY_ORDER.get(v.dimension, 9) for v in item[1].violations), default=9),
        item[1].quality_score,
    ))

    if not bad:
        add("未发现失败运行。")
        add("")
    shown_bad = bad[:14]
    for cr, a in shown_bad:
        add(f"### {md_inline(cr.case.case_id)} / {md_inline(a.run_id)}（质量分 {a.quality_score}）")
        add("")
        add(f"- 用户请求：{md_inline(cr.case.user_request)}")
        add(f"- 业务结果：{business_result_text(cr.case, a)}")
        add(f"- 过程评价：{process_text(cr.case, a)}")
        for v in a.violations:
            add(f"- **根因 `{v.label}`**（规则 `{v.rule_id}@{v.rule_version}`，判定 `{v.certainty}`，置信度 {v.confidence}）")
            add(f"    - 首个错误事件：{event_ref(v.event_id, v.seq)}")
            add(f"    - 证据事件：{', '.join(f'`{e}`' for e in v.evidence_ids)}")
            add(f"    - 判定依据：{v.explanation}")
        if a.guards:
            add(f"- 正常拦截（不是故障）：{', '.join(f'`{g.event_id}` {g.code}' for g in a.guards)}")
        add(f"- 影响传播：{downstream_text(a)}")
        for s in suggestions(cr.case, a):
            add(f"- 改进建议：{s}")
        for un in a.uncertainties:
            add(f"- ⚠ 不确定性：{un}")
        add("")

    if len(bad) > len(shown_bad) > 0:
        # P3：截断必须写清"仅展示前 N / 共 M"，不能让读者以为报告覆盖了全部失败运行
        add(f"> 仅展示前 {len(shown_bad)} 条失败运行 / 共 {len(bad)} 条（按风险维度与质量分排序）。")
        add("")


def _md_section_labels(lines: list[str], result: EvaluationResult, summary: Mapping[str, Any]) -> None:
    add = lines.append
    # ------------------------------------------------------------ 聚合
    add("## 六、按根因标签聚合")
    add("")
    if summary["root_cause_counts"]:
        add("| 根因标签 | 命中次数 | 主责维度 |")
        add("|---|---|---|")
        for label, count in summary["root_cause_counts"].items():
            dims = [v.dimension for cr in result.results for a in cr.assessments for v in a.violations if v.label == label]
            add(f"| `{label}` | {count} | {dims[0] if dims else '—'} |")
    else:
        add("无反例。")
    add("")


def _md_section_limits(lines: list[str]) -> None:
    add = lines.append
    # ------------------------------------------------------------ 已知限制
    add("## 七、方法与局限（必须与结论一起阅读）")
    add("")
    add("- **确定性优先**：金额口径、字段完整性、确认门禁、重复风险、回调真实性等硬约束全部由规则判定，"
        "模型不参与裁决，因此这些结论可逐条复核。")
    add("- **正常拒绝识别**：工具对未确认提交、重复开票的保护性拒绝被单独记录为 guard，"
        "不计为接口故障，也不作为独立根因。")
    add("- **首因 vs 后果**：传播链只在参数真的流动到下游时建立，不按时间先后推因果。")
    add("- **允许拒判**：证据不足时输出 `INSUFFICIENT_EVIDENCE`，不生成貌似精确的根因。")
    add("- **单 Case 不代表版本质量**：所有版本结论均基于批量 Case 的分布，"
        "单 Case 结论在外推前需要更多样本支持。")
    add("- **合成数据边界**：本报告全部基于赛事方提供的合成沙箱数据，不代表真实业务系统表现。")
    add("")


def write_markdown(result: EvaluationResult, path: Path, metrics: Metrics | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_markdown(result, metrics), encoding="utf-8")
    return path

# ============================================================ 分受众报告
#
# 同一批结果，三类读者关心的事情不同：
#   业务：结论、影响面、要修什么、不能从报告里读什么
#   研发：规则命中、证据链、复现命令
#   产品：能力维度分布、版本趋势、改进优先级

#: 官方根因标签 → 能力域（用于按能力聚合，而不是按标签平铺）
CAPABILITY_DOMAIN: dict[str, str] = {
    "AMOUNT_BASIS": "金额口径",
    "AMOUNT_CALCULATION": "金额口径",
    "CONTEXT_UPDATE": "金额口径",
    "FIELD_FABRICATION": "字段完整性",
    "ENTITY_RESOLUTION": "字段完整性",
    "PRODUCT_MATCH": "字段完整性",
    "MULTI_ITEM_OMISSION": "批量与红票",
    "INTENT_CLASSIFICATION": "意图与任务类型",
    "DUPLICATE_CONTROL": "风控门禁",
    "CONFIRMATION_BYPASS": "风控门禁",
    "TOOL_ERROR_HANDLING": "工具与回调",
    "CALLBACK_MISINTERPRETATION": "工具与回调",
    "TOOL_ARGUMENT_ERROR": "工具与回调",
    "INSUFFICIENT_EVIDENCE": "证据不足",
}


def _runs(result: EvaluationResult) -> list[Any]:
    return [a for cr in result.results for a in cr.assessments]


def _label_counts(result: EvaluationResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in _runs(result):
        for v in a.violations:
            counts[v.label] = counts.get(v.label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _bad_cases(result: EvaluationResult) -> list[tuple[Any, Any]]:
    bad = [(cr, a) for cr in result.results for a in cr.assessments if a.verdict is Verdict.FAIL]
    # 与综合报告一致：按该 run 所有违规里最重的一维排序（P3）
    bad.sort(key=lambda item: (
        min((SEVERITY_ORDER.get(v.dimension, 9) for v in item[1].violations), default=9),
        item[1].quality_score,
    ))
    return bad


def _plain(text: str) -> str:
    """把技术术语翻成业务能读的说法（只用于业务版报告）。"""
    mapping = {
        "tax_exclusive": "不含税",
        "tax_inclusive": "含税",
        "RISK_CONTROL": "风控门禁",
        "BUSINESS_RESULT": "业务结果",
        "TOOL_CALL": "工具调用",
        "PROCESS": "处理过程",
        "context 明确记录": "记录显示",
    }
    for src, dst in mapping.items():
        text = text.replace(src, dst)
    return text


def build_business_markdown(
    result: EvaluationResult, metrics: Metrics | None = None, secondary: dict[str, Any] | None = None
) -> str:
    """给业务方：结论优先、去术语、明确不能得出的结论。"""
    summary = summarise(result)
    recommendation, reason = aggregate_recommendation(result)
    reason = _plain(reason)
    advice = {"release": "可以发布", "gray": "灰度发布", "defer": "暂缓发布", "investigate": "先查清再定"}

    lines: list[str] = []
    add = lines.append
    add("# 版本评测结论（业务版）")
    add("")
    add("> **数据性质**：指标在公开训练集上计算，属拟合值；逐 Case 结论由轨迹证据推出，非模型裁决。")
    add("")
    add(f"## 结论：**{advice.get(recommendation, recommendation)}**")
    add("")
    add(reason)
    add("")
    add("## 影响面")
    add("")
    add(f"- 本次评测覆盖 {summary['cases']} 个业务场景、{summary['runs']} 次运行")
    add(f"- 处理正确的运行：{summary['task_success']} 次；处理有问题的：{summary['task_failure']} 次；"
        f"证据不足（不可评估）：{summary['task_unknown']} 次")
    add(f"- 实际生成发票：{summary['invoice_created']} 次")
    add(f"- 其中被系统正确拦下的风险操作：{summary['guards_detected']} 次（拦截正确，不是故障）")
    no_run = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run:
        add(f"- 注意：{'、'.join(no_run)} 该 Case 无可评估运行（runs 为空），不参与任何统计。")
    add("")

    domain_counts: dict[str, int] = {}
    for label, n in _label_counts(result).items():
        domain = CAPABILITY_DOMAIN.get(label, "其他")
        domain_counts[domain] = domain_counts.get(domain, 0) + n
    add("## 问题集中在哪些业务环节")
    add("")
    add("| 业务环节 | 问题次数 | 可能造成的业务后果 |")
    add("|---|---|---|")
    consequence = {
        "风控门禁": "未确认/重复开票，可能造成错开与重复开票",
        "金额口径": "金额或税额错误，直接影响票面",
        "字段完整性": "开错对象、开错项目或凭猜测开票",
        "批量与红票": "漏开、错开或冲销方向错误",
        "意图与任务类型": "把请求做成了另一件事",
        "工具与回调": "重复提交、误报成功",
        "证据不足": "现有记录不足以判断，需要补日志",
    }
    for domain, n in sorted(domain_counts.items(), key=lambda kv: -kv[1]):
        add(f"| {domain} | {n} | {consequence.get(domain, '—')} |")
    add("")

    add("## 必须优先处理的场景")
    add("")
    bad = _bad_cases(result)
    if not bad:
        add("没有发现处理错误的运行。")
    shown_bad = bad[:5]
    for cr, a in shown_bad:
        first = a.first_error
        add(f"### {md_inline(cr.case.case_id)}：{md_inline(cr.case.scenario)}")
        add("")
        add(f"- 用户要什么：{md_inline(cr.case.user_request)}")
        add(f"- 出了什么问题：{_plain(business_result_text(cr.case, a))}")
        add(f"- 影响是否扩散：{downstream_text(a)}")
        if a.guards:
            add(f"- 提醒：其中 {len(a.guards)} 次工具拒绝是**正确的保护**，不需要修接口")
        if first is not None:
            add(f"- 业务侧可对照的记录编号：{event_ref(first.event_id, first.seq)}")
        add("")
    if len(bad) > len(shown_bad) > 0:
        # P3：分受众报告同样要写清截断范围
        add(f"仅展示前 {len(shown_bad)} 个场景 / 共 {len(bad)} 个问题场景（按风险严重度排序）。")
        add("")
    add("")

    add("## 这份报告**不能**得出的结论")
    add("")
    add("1. 单个场景的结果不能代表整体版本质量，结论只在批量层面成立。")
    add("2. 数据是赛事方提供的**合成沙箱**数据，不代表生产环境表现。")
    add("3. 没有真实系统验证，不构成上线依据。")
    if not metrics:
        add("4. 本批数据未提供专家标注，因此报告不给准确率数字。")
    add("")
    return "\n".join(lines)


def build_engineering_markdown(
    result: EvaluationResult, metrics: Metrics | None = None, secondary: dict[str, Any] | None = None
) -> str:
    """给研发：规则命中、证据链、拒判、复现命令。"""
    manifest = result.manifest
    lines: list[str] = []
    add = lines.append
    add("# 评测技术报告（研发版）")
    add("")
    add("> **数据性质**：指标在公开训练集上计算，属拟合值，不代表隐藏集泛化能力；逐 Case 结论均由轨迹证据推出。")
    add("")
    add("## 实验清单")
    add("")
    add("| 项目 | 值 |")
    add("|---|---|")
    add(f"| 评测器 | `{manifest['tracelab_evaluator_version']}` |")
    add(f"| Trace IR | `{manifest['trace_ir_version']}` |")
    add(f"| 适配器 | `{manifest['adapter']['name']}@{manifest['adapter']['version']}` |")
    add(f"| 规则集 | `{manifest['rulepack_version']}`（{len(manifest['rulepack_rules'])} 条） |")
    add(f"| 数据 SHA-256 | `{manifest['dataset']['sha256']}` |")
    add(f"| 结果哈希 | `{result.result_hash()[:16]}…` |")
    reading = manifest["integrity"].get("reading") or {}
    add(f"| 读取完整性 | 总行 {reading.get('lines_total')} / 解析 {reading.get('cases_parsed')} / "
        f"跳过 {reading.get('skipped')} / 截断 {reading.get('truncated')} |")
    add("")
    no_run = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run:
        add("> ⚠️ 该 Case 无可评估运行：" + "、".join(f"`{c}`" for c in no_run)
            + "（runs 为空），已从指标与版本对比中排除。")
        add("")

    add("## 规则命中")
    add("")
    counts = _label_counts(result)
    if not counts:
        add("无违规命中。")
    else:
        add("| 根因标签 | 命中 | 能力域 | 涉及 run |")
        add("|---|---|---|---|")
        for label, n in counts.items():
            runs = [a.run_id for a in _runs(result) if label in a.labels]
            add(f"| `{label}` | {n} | {CAPABILITY_DOMAIN.get(label, '其他')} | {', '.join(runs)} |")
    add("")

    add("## 证据链（失败运行）")
    add("")
    for _cr, a in _bad_cases(result):
        add(f"### {a.run_id}")
        add("")
        for v in a.violations:
            add(f"- `{v.label}` ← 规则 `{v.rule_id}@{v.rule_version}`（{v.certainty}，conf {v.confidence}）")
            add(f"    - 首因事件 `{v.event_id}`（seq={v.seq}）")
            add(f"    - 证据事件 {', '.join(f'`{e}`' for e in v.evidence_ids) or '—'}")
            add(f"    - 依据：{v.explanation}")
        for g in a.guards:
            add(f"- guard `{g.event_id}` `{g.code}`：{g.reason}")
        if a.propagation:
            add(f"- 传播：{downstream_text(a)}")
        for u in a.uncertainties:
            add(f"- 不确定性：{u}")
        add("")

    if secondary:
        from .secondary import render_markdown_table

        add("## 补充指标（含不可评估项）")
        add("")
        lines.extend(render_markdown_table(secondary))
        add("")
        add(f"> {secondary.get('note', '')}")
        add("")

    add("## 拒判与不确定性")
    add("")
    abstained = [a for a in _runs(result) if "INSUFFICIENT_EVIDENCE" in a.labels]
    if not abstained:
        add("本次没有拒判。")
    else:
        add("| run | 原因 |")
        add("|---|---|")
        for a in abstained:
            add(f"| {md_cell(a.run_id)} | {md_cell(a.uncertainties[0] if a.uncertainties else '—')} |")
    add("")

    add("## 复现命令")
    add("")
    add("```bash")
    add("python -m tracelab evaluate --cases <cases.jsonl> --labels <labels.jsonl> --out out/x")
    add("python -m tracelab ablate --cases <cases.jsonl> --labels <labels.jsonl> --out out/ablation")
    add("python -m tracelab regression verify --pack regression/invoice-pack/regression-pack.json \\")
    add("  --cases <cases.jsonl> --out out/regression-verify")
    add("```")
    add("")
    return "\n".join(lines)


def build_product_markdown(
    result: EvaluationResult, metrics: Metrics | None = None, secondary: dict[str, Any] | None = None
) -> str:
    """给产品：能力维度分布、版本趋势、改进优先级。"""
    recommendation, reason = aggregate_recommendation(result)
    lines: list[str] = []
    add = lines.append
    add("# 版本能力分析（产品版）")
    add("")
    add("> **数据性质**：指标在公开训练集上计算，属拟合值；结论来自轨迹证据，未使用大模型裁决。")
    add("")
    add(f"**当前建议：`{recommendation}`** — {reason}")
    add("")

    add("## 能力维度分布")
    add("")
    domain_counts: dict[str, int] = {}
    for label, n in _label_counts(result).items():
        domain = CAPABILITY_DOMAIN.get(label, "其他")
        domain_counts[domain] = domain_counts.get(domain, 0) + n
    total = sum(domain_counts.values()) or 1
    add("| 能力域 | 命中 | 占比 |")
    add("|---|---|---|")
    for domain, n in sorted(domain_counts.items(), key=lambda kv: -kv[1]):
        add(f"| {domain} | {n} | {n / total:.0%} |")
    add("")

    add("## 版本方向矩阵")
    add("")
    add("| Case | 场景 | 方向 | 质量分变化 |")
    add("|---|---|---|---|")
    for cr in result.results:
        add(f"| {md_cell(cr.case.case_id)} | {md_cell(cr.case.scenario)} | `{cr.comparison.direction}` "
            f"| {cr.comparison.quality_delta:+.1f} |")
    add("")
    dirs = comparable_directions(result)
    add("方向汇总（仅可比较 A/B 对）：" + ("，".join(f"{k} {v}" for k, v in dirs.items()) or "无"))
    unpaired = [
        cr.case.case_id for cr in result.results if cr.assessments and not ab_pair(cr)
    ]
    if unpaired:
        add(f"> {len(unpaired)} 个 Case 运行数≠2，不参与方向比较。")
    add("")
    no_run = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run:
        add("> ⚠️ 该 Case 无可评估运行：" + "、".join(f"`{c}`" for c in no_run)
            + "（runs 为空），方向与能力统计已排除这些 Case。")
        add("")

    add("## 改进优先级（按扣分总额）")
    add("")
    impact: dict[str, float] = {}
    for a in _runs(result):
        for v in a.violations:
            impact[v.label] = impact.get(v.label, 0.0) + v.points
    if not impact:
        add("无可量化的扣分项。")
    else:
        add("| 优先级 | 根因标签 | 能力域 | 累计扣分 | 建议动作 |")
        add("|---|---|---|---|---|")
        for i, (label, pts) in enumerate(sorted(impact.items(), key=lambda kv: -kv[1]), 1):
            add(f"| P{i} | `{label}` | {CAPABILITY_DOMAIN.get(label, '其他')} | {pts:g} | "
                f"{_priority_action(label)} |")
    add("")

    add("## 待验证问题（下一轮评测要覆盖）")
    add("")
    add("- 未见场景下的拒判率是多少？拒判是否集中在某一类请求？")
    add("- 冒烟修复后，回归包是否出现 `improved` 且无 `regressed`？")
    add("- 候选版在灰度流量上的分布是否与合成数据一致？")
    add("")
    return "\n".join(lines)


def _priority_action(label: str) -> str:
    return {
        "CONFIRMATION_BYPASS": "把确认记录做成提交前硬门禁",
        "DUPLICATE_CONTROL": "风险标记命中即阻断，不允许降级为等待",
        "AMOUNT_BASIS": "口径抽取后回填并二次断言",
        "AMOUNT_CALCULATION": "金额计算统一 Decimal 并加回归",
        "INTENT_CLASSIFICATION": "任务类型与取消意图显式分类 + 低置信转澄清",
        "TOOL_ERROR_HANDLING": "未知态禁止自动重试，统一转人工",
        "CALLBACK_MISINTERPRETATION": "对外话术只从回调结果生成",
        "TOOL_ARGUMENT_ERROR": "提交参数复用已确认预览快照",
        "FIELD_FABRICATION": "缺字段强制走追问分支",
        "ENTITY_RESOLUTION": "多候选强制交互消歧",
        "PRODUCT_MATCH": "商品匹配低置信转人工",
        "MULTI_ITEM_OMISSION": "批量条目守恒断言",
        "CONTEXT_UPDATE": "多轮修改绑定轮次号",
        "INSUFFICIENT_EVIDENCE": "补充可观测字段以便定位",
    }.get(label, "补充规则与回归用例")


AUDIENCE_BUILDERS = {
    "business": build_business_markdown,
    "engineering": build_engineering_markdown,
    "product": build_product_markdown,
}


def write_audience_reports(
    result: EvaluationResult,
    out_dir: Path,
    metrics: Metrics | None = None,
    secondary: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """一次产出三份受众报告，路径按受众命名。

    审计 F-46：与 `write_markdown` 保持一致，先建目录；公共 API 不能因为
    "调用方没手动 mkdir" 就抛 FileNotFoundError。
    """
    out: dict[str, Path] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, builder in AUDIENCE_BUILDERS.items():
        path = out_dir / f"report-{name}.md"
        path.write_text(builder(result, metrics, secondary), encoding="utf-8")
        out[name] = path
    return out




