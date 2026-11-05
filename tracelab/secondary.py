"""补充指标：算得出来的算，算不出来的显式标"不可评估"。

背景：任务书 P1 列了几项进阶指标（正常工具拦截误报率、错误传播路径准确率、
拒判质量、Case 回归发现率）。但官方资料包**并不提供**传播路径真值、拒判正例，
如果硬算成 0 或 1，就是在编数字——任务书也明确要求"无法计算应返回 null 或
明确的不可评估状态，而不是随意填零"。

因此本模块给每个指标三态：

* `computed`        有真值、有分母，直接给数
* `structurally_zero` 结论由实现结构决定（例如"从不把拦截当故障"），
                     说明实现正确但不等价于实测泛化
* `not_evaluable`   缺真值，返回 None 并写明缺什么

报告里必须原样展示三态，不允许把 `not_evaluable` 渲染成 0。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import Dataset
from .evaluation import EvaluationResult

COMPUTED = "computed"
STRUCTURAL = "structurally_zero"
NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True)
class MetricResult:
    name: str
    title: str
    status: str
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    reason: str = ""
    caveat: str = ""
    #: 审计 R2-08：需要同时展示精确率与召回率的指标（例如传播路径），
    #: 不能只报一个数字让读者以为覆盖率是 100%。
    precision: float | None = None
    recall: float | None = None

    @property
    def display(self) -> str:
        if self.status == NOT_EVALUABLE or self.value is None:
            return "不可评估"
        if (self.precision is not None or self.recall is not None) and self.denominator:
            # 有产出边时同时展示 P/R；一条边都没产出时只有一个"测得出来的 0"，
            # 不写成 "P 0.0% · R 0.0%" 以免看起来像"两个指标都算了"。
            return f"P {_pct(self.precision)} · R {_pct(self.recall)}"
        return f"{self.value:.1%}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "precision": self.precision,
            "recall": self.recall,
            "display": self.display,
            "reason": self.reason,
            "caveat": self.caveat,
        }


# ------------------------------------------------------------------ 计算

def _pct(value: float | None) -> str:
    """比率展示：`None` → ``"—"``（算不出来就明说，不用 0 或崩溃冒充）。"""
    return "—" if value is None else f"{value:.1%}"


def _gold_evidence(
    result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]
) -> dict[tuple[str, str], set[str]]:
    """官方标注证据事件：键改为 `(case_id, run_id)`（审计 R4-10）。

    旧实现只用 `run_id` 做键：跨 Case 同名 run_id 会静默覆盖；覆盖率分母又只统计
    "恰好匹配上的 run"，未匹配金标被悄悄丢弃 → 覆盖率可被抬到 100%。
    """
    out: dict[tuple[str, str], set[str]] = {}
    for cr in result.results:
        gold_case = labels.get(cr.case.case_id)
        if not gold_case:
            continue
        for ra in gold_case.get("run_assessments", []):
            ids: set[str] = set()
            for rc in ra.get("root_causes", []):
                ids.update(rc.get("evidence_event_ids") or [])
            out[(cr.case.case_id, str(ra.get("run_id")))] = ids
    return out


def guard_false_positive_rate(result: EvaluationResult) -> MetricResult:
    """把工具的保护性拒绝**误报为工具/接口故障**的比例。

    定义边界很重要：Agent 在 `SUBMIT_INVOICE` 上违规（未确认就提交），
    而工具恰好在同一个事件上拒绝——此时把该事件根因写成 `CONFIRMATION_BYPASS`
    是**正确**的（违规动作与拦截发生在同一事件）。真正算误报的是：
    把拦截归到工具侧标签（`TOOL_ERROR_HANDLING` / `CALLBACK_MISINTERPRETATION`），
    也就是把风控漏洞洗成接口 bug。本指标量的就是后者。
    """
    tool_side_labels = {"TOOL_ERROR_HANDLING", "CALLBACK_MISINTERPRETATION"}
    runs_with_guard = 0
    blamed = 0
    cases: list[dict[str, Any]] = []
    for cr in result.results:
        for a in cr.assessments:
            if not a.guards:
                continue
            runs_with_guard += 1
            guard_ids = {g.event_id for g in a.guards}
            bad = [
                v.event_id for v in a.violations
                if v.label in tool_side_labels and v.event_id in guard_ids
            ]
            if bad:
                blamed += 1
                cases.append({"run_id": a.run_id, "event_ids": bad})
    if runs_with_guard == 0:
        return MetricResult(
            "guard_false_positive_rate", "正常工具拦截误报率", NOT_EVALUABLE,
            reason="本批数据没有任何保护性工具拦截样本，分母为 0。",
        )
    value = blamed / runs_with_guard
    return MetricResult(
        "guard_false_positive_rate", "正常工具拦截误报率",
        COMPUTED,
        value=round(value, 4), numerator=blamed, denominator=runs_with_guard,
        reason=(
            f"{runs_with_guard} 条运行含保护性拦截，其中 {blamed} 条把拦截误归为工具侧故障。"
            + (f"（误归 run：{[c['run_id'] for c in cases]}）" if cases else "")
        ),
        caveat=(
            "只统计「拦截事件被归到工具侧标签」；Agent 自身在提交事件上的违规"
            "（如 CONFIRMATION_BYPASS）与拦截同事件出现属正常，不计入误报。"
        ),
    )


def evidence_coverage_of_gold(result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]) -> MetricResult:
    """我方证据事件覆盖官方标注证据的比例（有真值，可算）。

    审计 R4-10：分母用**全部**官方证据事件（含本次评测没覆盖到的 run），
    未匹配的金标 run 单独计数并写进 reason/caveat——否则"只评一部分 Case"会得到虚假 100%。
    """
    gold = _gold_evidence(result, labels)
    ours: dict[tuple[str, str], set[str]] = {
        (cr.case.case_id, a.run_id): set(a.evidence_ids)
        for cr in result.results
        for a in cr.assessments
    }
    total = sum(len(ids) for ids in gold.values())
    hit = sum(len(ids & ours.get(key, set())) for key, ids in gold.items())
    matched_runs = sum(1 for key in gold if key in ours)
    unmatched_runs = len(gold) - matched_runs
    if total == 0:
        return MetricResult(
            "evidence_coverage_of_gold", "证据覆盖率", NOT_EVALUABLE,
            reason="没有可用的官方证据事件标注。",
        )
    reason = f"官方标注证据事件共 {total} 个，我方证据链覆盖 {hit} 个。"
    caveat = "官方证据是「支持结论的最小集合」，不要求穷尽，因此这不是召回率的严格定义。"
    if unmatched_runs:
        reason += f"（{matched_runs}/{len(gold)} 条金标运行与本次评测匹配）"
        caveat += (
            f" 另有 {unmatched_runs} 条金标运行未参与本次评测，其证据事件仍计入分母；"
            "只看匹配上的运行会高估覆盖率。"
        )
    return MetricResult(
        "evidence_coverage_of_gold", "证据覆盖率", COMPUTED,
        value=round(hit / total, 4), numerator=hit, denominator=total,
        reason=reason, caveat=caveat,
    )


def _study(dataset: Dataset) -> list[Any]:
    from .mutation import run_injection_study

    return run_injection_study(dataset)


def propagation_accuracy(study: list[Any]) -> MetricResult:
    """错误传播路径准确率：真值来自缺陷注入（我们自己知道哪些步骤携带错误金额）。

    审计 R2-08 / R2-B-F-29：同时给出**边级精确率**与**事件级召回率**。
    旧实现只把精确率作为 `value`，召回率仅存在于 reason 文本里，而 Markdown 表格
    在分子/分母都存在时会丢弃 reason——读者只看到 100%，看不到漏掉的真值传播。
    边按 `(from, to)` 去重后计数（分子=命中真值事件的边数，分母=产出去重边数）。
    """
    expected = sum(r.propagation_expected for r in study)
    hit = sum(r.propagation_hit for r in study)
    hit_edges = sum(getattr(r, "propagation_hit_edges", r.propagation_hit) for r in study)
    edges = sum(r.propagation_edges for r in study)
    recall = round(hit / expected, 4) if expected else None
    if not expected:
        return MetricResult(
            "propagation_accuracy", "错误传播路径准确率", NOT_EVALUABLE,
            reason="本批数据没有期望传播的注入样本，分母为 0。",
        )
    if not edges:
        return MetricResult(
            "propagation_accuracy", "错误传播路径准确率", COMPUTED,
            value=0.0, precision=0.0, recall=recall,
            numerator=0, denominator=0,
            reason=(
                f"注入真值下：期望传播事件 {expected} 个，命中 {hit} 个，但评测器没有产出任何传播边；"
                f"边级精确率按 0 计，召回率 {_pct(recall)}。"
            ),
            caveat=(
                "精确率的分母是评测器自己产出的传播边；一条边都没有时按 0 分记，"
                "不按「不可评估」处理（否则会掩盖传播定位能力为零）。"
            ),
        )
    precision = round(hit_edges / edges, 4)
    return MetricResult(
        "propagation_accuracy", "错误传播路径准确率", COMPUTED,
        value=precision, precision=precision, recall=recall,
        numerator=hit_edges, denominator=edges,
        reason=(
            f"注入真值下：期望传播事件 {expected} 个，命中 {hit} 个；"
            f"产出去重传播边 {edges} 条，其中命中真值事件 {hit_edges} 条；"
            f"边级精确率 {precision:.1%}，事件级召回率 {_pct(recall)}。"
        ),
        caveat="真值由注入构造（不是官方标注）；精确率与召回率必须一起看。",
    )


def abstention_quality(study: list[Any], result: EvaluationResult) -> MetricResult:
    """拒判质量：用"证据被移除"的注入真值衡量，并给出误拒判率。"""
    removal = next((r for r in study if r.injection == "evidence_removal"), None)
    intact_unknown = sum(1 for cr in result.results for a in cr.assessments if a.is_unknown)
    intact_total = sum(len(cr.assessments) for cr in result.results)
    # EVAL-07：分母为 0 时误拒判率是"算不出来"，必须是 None（报告显示"—"），
    # 不能写成 0.0 —— 那等于宣称"零误拒判"，是凭空造证据。
    false_positive = round(intact_unknown / intact_total, 4) if intact_total else None

    if removal is None or removal.effective == 0:
        return MetricResult(
            "abstention_precision", "拒判质量（召回）", NOT_EVALUABLE,
            reason="没有可用的「证据移除」注入样本。",
        )
    value = removal.rate
    return MetricResult(
        "abstention_precision", "拒判质量（召回）", COMPUTED,
        value=value, numerator=removal.detected, denominator=removal.effective,
        reason=(
            f"移除关键证据的 {removal.effective} 条注入运行中，{removal.detected} 条被显式标记证据不足；"
            f"未注入运行上的误拒判率 {_pct(false_positive)}（{intact_unknown}/{intact_total}）。"
        ),
        caveat="真值由注入构造；误拒判率与召回需一起看，单看召回会被「什么都拒判」策略刷高。",
    )

def regression_discovery_rate_from(study: list[Any]) -> MetricResult:
    """回归发现率：用缺陷注入造真值，可算，但有明确局限。"""
    from .mutation import overall

    try:
        stats = overall(study)
    except Exception as exc:  # noqa: BLE001 - 指标算不出来要显式降级
        return MetricResult(
            "regression_discovery_rate", "Case 回归发现率（按注入实例）", NOT_EVALUABLE,
            reason=f"注入实验执行失败：{type(exc).__name__}: {exc}",
        )
    if not stats["tried"]:
        return MetricResult(
            "regression_discovery_rate", "Case 回归发现率（按注入实例）", NOT_EVALUABLE,
            reason="没有可注入的正确运行。",
        )
    if not stats["effective"]:
        # EVAL-05：分母改用有效注入后，全部空操作时不得用 0 或 1 冒充
        return MetricResult(
            "regression_discovery_rate", "Case 回归发现率（按注入实例）", NOT_EVALUABLE,
            reason=(
                f"尝试注入 {stats['tried']} 次，但全部是无效注入（空操作），"
                "有效分母为 0，检出率不可评估。"
            ),
        )
    return MetricResult(
        "regression_discovery_rate", "Case 回归发现率（按注入实例）", COMPUTED,
        value=stats["detection_rate"], numerator=stats["detected"], denominator=stats["effective"],
        reason=(
            f"有效注入 {stats['effective']} 次（无效 {stats['invalid']} 次已排除），"
            f"检出 {stats['detected']} 次。"
        ),
        caveat=("分母是**注入实例**（每个 Case 的每条正确运行 × 每种注入），不是 Case 数；注入类型由本方案设计，天然偏向已被规则覆盖的类型，不等于隐藏测试集表现。"),
    )


def compute_all(result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    study = _study(result.dataset)
    metrics = [
        guard_false_positive_rate(result),
        evidence_coverage_of_gold(result, labels),
        propagation_accuracy(study),
        abstention_quality(study, result),
        regression_discovery_rate_from(study),
    ]
    return {
        "metrics": [m.to_dict() for m in metrics],
        "note": (
            "口径：computed=有真值可算（传播类同时给 P/R）；not_evaluable=分母不存在；"
            "not_evaluable=缺真值，按任务书要求返回 null 并写明缺失项，不以 0 冒充。"
        ),
    }


# ------------------------------------------------------------------ 渲染

def render_markdown_table(payload: Mapping[str, Any]) -> list[str]:
    rows = payload.get("metrics") or []
    lines = [
        "| 补充指标 | 结果 | 状态 | 分母/说明 | 局限 |",
        "|---|---|---|---|---|",
    ]
    status_text = {
        COMPUTED: "已计算",
        STRUCTURAL: "结构性成立",
        NOT_EVALUABLE: "**不可评估**",
    }
    for m in rows:
        denom = (
            f"{m['numerator']:g}/{m['denominator']:g}" if m.get("numerator") is not None
            and m.get("denominator") is not None else m.get("reason", "")
        )
        lines.append(
            f"| {m['title']} | {m['display']} | {status_text.get(m['status'], m['status'])} | {denom} | "
            f"{m.get('caveat') or m.get('reason') or '—'} |"
        )
    return lines





