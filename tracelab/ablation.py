"""消融实验：把"差异化主张"变成可证伪的数字。

我们对外主张是：**开票业务语义的确定性规则，是评测准确率的来源**。
如果这个主张错了，实验结果会直接打脸。因此本模块按四个变体跑同一批数据：

| 变体 | 含义 |
|---|---|
| `full` | 全部 15 条规则（我们的方案） |
| `terminal_only` | 只看终态是否等于题内可接受终态（不做任何过程分析的上界） |
| `generic_health` | 只保留与开票业务无关的通用健康检查（确认门禁、重复风险、超时重试、回调真实性、参数漂移） |
| `business_only` | 只保留必须比对题内业务目标的规则（金额口径/计算、字段、消歧、商品、批量、任务类型、多轮更新） |

结论只认数字：`business_only` 与 `generic_health` 的差值，就是"领域语义"这件事本身的贡献。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import Dataset, EvalProfile
from .evaluation import EvaluationResult, evaluate_dataset, score_against_labels
from .rules import RULES

#: 不需要题内业务目标即可判断的"通用 Agent 健康/协议一致性"规则
GENERIC_RULE_IDS = frozenset({"R009", "R010", "R011", "R012", "R013", "R902"})

#: 必须比对题内业务目标才能判断的领域语义规则
BUSINESS_RULE_IDS = frozenset({"R001", "R002", "R003", "R004", "R005", "R006", "R007", "R008", "R014", "R015"})

PROFILES: tuple[EvalProfile, ...] = (
    EvalProfile("full", f"全部 {len(RULES)} 条确定性规则（提交方案）"),
    EvalProfile("terminal_only", "只看终态是否等于题内可接受终态（无过程分析）", terminal_only=True),
    EvalProfile("generic_health", "只保留通用 Agent 健康/协议一致性规则，不看业务语义", GENERIC_RULE_IDS),
    EvalProfile("business_only", "只保留依赖题内业务目标的领域语义规则", BUSINESS_RULE_IDS),
)

METRIC_LABELS: dict[str, str] = {
    "agent_task_success_accuracy": "任务处理正确率",
    "business_invoice_created_accuracy": "发票生成状态准确率",
    "version_direction_accuracy": "版本方向准确率",
    "first_fault_event_exact_match": "首因事件 Exact Match",
    "root_cause_label_macro_f1": "根因标签 Macro-F1",
    "evidence_id_validity": "证据 ID 有效性",
}


@dataclass
class ProfileOutcome:
    profile: EvalProfile
    result: EvaluationResult
    metrics: Mapping[str, Any]
    #: 漏判：真值是"处理错误"，变体却判成功
    false_negatives: list[dict[str, Any]]
    #: 误报：真值是"处理正确"，变体却判失败
    false_positives: list[dict[str, Any]]
    #: 真值有版本方向、但该变体判成 equal 的 Case（静默漏报回归）
    silent_regressions: list[str]


@dataclass
class AblationReport:
    outcomes: list[ProfileOutcome]
    gold_directions: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": [
                {
                    "name": o.profile.name,
                    "description": o.profile.description,
                    "metrics": dict(o.metrics),
                    "false_negatives": o.false_negatives,
                    "false_positives": o.false_positives,
                    "silent_regressions": o.silent_regressions,
                }
                for o in self.outcomes
            ],
            "gold_direction_count": len(self.gold_directions),
        }


def _gold_by_run(labels: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for lb in labels.values():
        for ra in lb.get("run_assessments", []):
            out[ra["run_id"]] = {
                "case_id": lb["case_id"],
                "ok": bool(ra.get("agent_task_success")),
                "labels": sorted(rc["label"] for rc in ra.get("root_causes", [])),
            }
    return out


def _pct(value: Any) -> str:
    """百分比统一格式：`None` → ``"—"``，绝不把 None 塞进 `f"{v:.1%}"`。

    C-22：指标算不出来时（缺标注/无方向分母）`score_against_labels` 会返回 None，
    旧实现用 `f"{v:.1%}"` 直接格式化，抛
    `TypeError: unsupported format string passed to NoneType`，把合法输入跑崩。
    """
    return "—" if value is None else f"{value:.1%}"


def _cell(value: Any) -> str:
    """指标单元格：浮点走百分比，None 走 ``"—"``，其它原样。"""
    if value is None or isinstance(value, float):
        return _pct(value)
    return str(value)


def _run_profile(dataset: Dataset, profile: EvalProfile, labels: Mapping[str, Mapping[str, Any]]) -> ProfileOutcome:
    result = evaluate_dataset(dataset, profile)
    metrics = score_against_labels(result, labels).values
    return ProfileOutcome(profile, result, metrics, [], [], [])


def run_ablation(dataset: Dataset, labels: Mapping[str, Mapping[str, Any]]) -> AblationReport:
    outcomes = [_run_profile(dataset, p, labels) for p in PROFILES]
    gold = _gold_by_run(labels)

    for outcome in outcomes:
        for cr in outcome.result.results:
            for a in cr.assessments:
                truth = gold.get(a.run_id)
                if truth is None:
                    continue
                if a.is_unknown:
                    # 审计 R2-18：UNKNOWN 是"判不了"，不是"判错"，不能计入误报/漏报
                    continue
                if truth["ok"] and not a.agent_task_success:
                    outcome.false_positives.append({
                        "run_id": a.run_id,
                        "predicted_labels": a.labels,
                        "gold_labels": truth["labels"],
                    })
                elif not truth["ok"] and a.agent_task_success:
                    outcome.false_negatives.append({
                        "run_id": a.run_id,
                        "predicted_labels": a.labels,
                        "gold_labels": truth["labels"],
                    })
        outcome.silent_regressions = [
            cr.case.case_id
            for cr in outcome.result.results
            # C-22：标注可以是部分覆盖的，缺标注的 Case 不能 KeyError
            if labels.get(cr.case.case_id, {}).get("version_direction_on_this_case") not in (None, "equal")
            and cr.comparison.direction == "equal"
        ]

    gold_dirs = {
        cid: lb["version_direction_on_this_case"]
        for cid, lb in labels.items()
        if lb.get("version_direction_on_this_case")
    }
    return AblationReport(outcomes, gold_dirs)


# ------------------------------------------------------------------ 报告渲染

def build_markdown(report: AblationReport, dataset: Dataset) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 消融实验：领域语义规则到底值多少准确率")
    add("")
    add("> 目的：把「开票业务语义 + 确定性规则是准确率来源」这一主张变成**可证伪**的数字。")
    add("> 同一批数据、同一评测器，只改规则集，观察指标怎么动。")
    add("")
    add(f"数据：`{Path(dataset.source).name}`，{len(dataset)} Case / "
        f"{sum(len(c.runs) for c in dataset)} 运行，SHA-256 `{dataset.sha256[:16]}…`")
    add("")

    add("## 一、变体定义")
    add("")
    add("| 变体 | 说明 | 规则数 |")
    add("|---|---|---|")
    for o in report.outcomes:
        count = "全部" if o.profile.rule_ids is None and not o.profile.terminal_only else (
            "0（不使用规则）" if o.profile.terminal_only else len(o.profile.rule_ids or ())
        )
        add(f"| `{o.profile.name}` | {o.profile.description} | {count} |")
    add("")

    full = report.outcomes[0]
    add("## 二、指标对比")
    add("")
    header = "| 指标 | " + " | ".join(f"`{o.profile.name}`" for o in report.outcomes) + " |"
    add(header)
    add("|---" * (len(report.outcomes) + 1) + "|")
    for key, label in METRIC_LABELS.items():
        cells = [_cell(o.metrics.get(key)) for o in report.outcomes]
        add(f"| {label} | " + " | ".join(cells) + " |")
    # 审计 R2-06：不同变体的可评估分母不同（少规则会多出 UNKNOWN），
    # 只给百分比、不给分母会把"分母缩小"误读成"能力上升"。
    for key, label in (("runs_scored", "可评估运行数"), ("runs_unknown", "UNKNOWN 运行数")):
        cells = [str(o.metrics.get(key) if o.metrics.get(key) is not None else "—") for o in report.outcomes]
        add(f"| {label} | " + " | ".join(cells) + " |")
    add("")

    # 关键差值
    business = next(o for o in report.outcomes if o.profile.name == "business_only")
    generic = next(o for o in report.outcomes if o.profile.name == "generic_health")
    terminal = next(o for o in report.outcomes if o.profile.name == "terminal_only")

    def delta(key: str, o: ProfileOutcome) -> str:
        a, b = full.metrics.get(key), o.metrics.get(key)
        if not isinstance(a, float) or not isinstance(b, float):
            return "—"
        # 审计 R2-06：分母不同（可评估运行数不同）时差值不可比，必须写清而不是照减
        if full.metrics.get("runs_scored") != o.metrics.get("runs_scored"):
            return "—（分母不同，不可比）"
        return f"{(a - b) * 100:+.1f} pp"

    add("## 三、关键结论（相对 `full` 的差距）")
    add("")
    add("| 对照 | 任务正确率差距 | 首因 Exact Match 差距 |")
    add("|---|---|---|")
    for o in (terminal, generic, business):
        add(f"| `{o.profile.name}` | {delta('agent_task_success_accuracy', o)} | "
            f"{delta('first_fault_event_exact_match', o)} |")
    add("")
    add(
        f"- **只看终态**（通用做法上界）：任务正确率 "
        f"{_pct(terminal.metrics.get('agent_task_success_accuracy'))}。"
        f"它漏掉的样本里，终态是「对的」，错在业务语义。"
    )
    add(
        f"- **通用健康检查**（确认门禁/重复风险/超时/回调/参数漂移）："
        f"{_pct(generic.metrics.get('agent_task_success_accuracy'))}，说明"
        f"「Agent 有没有按协议办事」能抓到一部分，但抓不到业务口径。"
    )
    business_metric = business.metrics.get("agent_task_success_accuracy")
    generic_metric = generic.metrics.get("agent_task_success_accuracy")
    business_acc = float(business_metric) if isinstance(business_metric, float) else None
    generic_acc = float(generic_metric) if isinstance(generic_metric, float) else None
    comparable = (
        business_acc is not None
        and generic_acc is not None
        and business.metrics.get("runs_scored") == generic.metrics.get("runs_scored")
    )
    if comparable and business_acc is not None and generic_acc is not None:
        gap = f"{(business_acc - generic_acc) * 100:+.1f} pp"
        gap_text = f"与通用健康检查相差 {gap}，这才是「开票业务语义」本身的贡献"
    else:
        gap_text = (
            "与通用健康检查**不可直接相减**——两者可评估运行数不同"
            f"（business_only {business.metrics.get('runs_scored')} 条，"
            f"generic_health {generic.metrics.get('runs_scored')} 条，"
            f"business_only 另有 {business.metrics.get('runs_unknown')} 条 UNKNOWN）；"
            "差值混入了「拒判口径变化」，只能作方向性参考"
        )
    add(
        f"- **领域语义规则**：{_pct(business_acc)}，{gap_text}。"
    )
    add(
        "  但要注意：`business_only` 的任务正确率有一部分来自**终态兜底**"
        "（规则没抓到、终态却和题内预期不符时会被判为失败）。它的首因 Exact Match 只有 "
        f"{_pct(business.metrics.get('first_fault_event_exact_match'))}，"
        "说明「判对成败」与「定位到具体哪一步错了」是两件事——后者依赖被移除的通用规则。"
    )
    add("")

    add("## 四、错误分解（按真值分类）")
    add("")
    add("| 变体 | 漏判（真值错、判成对） | 误报（真值对、判成错） |")
    add("|---|---|---|")
    for o in report.outcomes:
        add(f"| `{o.profile.name}` | {len(o.false_negatives)} | {len(o.false_positives)} |")
    add("")
    add("漏判才是评测系统真正危险的方向：它会让一个有问题的版本被放行。")
    add("")
    for o in report.outcomes:
        if not o.false_negatives:
            continue
        add(f"### `{o.profile.name}` 漏判明细（{len(o.false_negatives)} 条）")
        add("")
        add("| run | 变体给出 | 真值根因 |")
        add("|---|---|---|")
        for item in o.false_negatives:
            given = ", ".join(item["predicted_labels"]) or "判为正确"
            gold = ", ".join(item["gold_labels"]) or "—"
            add(f"| `{item['run_id']}` | {given} | {gold} |")
        add("")

    add("## 五、静默回归漏报（版本其实变了，但变体判 `equal`）")
    add("")
    add(f"标注中方向非 equal 的 Case 共 {len(report.gold_directions)} 个。")
    add("")
    add("| 变体 | 漏报数 | 漏报 Case |")
    add("|---|---|---|")
    for o in report.outcomes:
        add(f"| `{o.profile.name}` | {len(o.silent_regressions)} | {', '.join(o.silent_regressions) or '无'} |")
    add("")
    add("> 「只比两个版本的成功率」的典型失效方式：结果对但过程违规的 Case，两版终态相同，"
        "方法会判 `equal`，从而漏掉一次真实退化。上表量化了这件事。")
    add("")

    add("## 六、可证伪声明")
    add("")
    add("这份实验对我们自己的主张是有风险的，因此把判据写在前面：")
    add("")
    add("- 若 `business_only` 与 `generic_health` 的任务正确率差距 ≤ 5 pp，"
        "则「领域语义是主要贡献」不成立，方案重心应转向可解释性与成本。")
    add("- 若 `terminal_only` 已达 90% 以上，则规则引擎的边际价值不足，不值得作为核心竞争力。")
    sample = len(dataset)
    per_case_pp = f"{100.0 / sample:.1f}" if sample else "—"
    add(f"- 本实验样本 n={sample}（每例 {per_case_pp} pp），只能做**定性演示与配对差**，"
        "不做统计推断，也不外推到隐藏测试集。")
    add("")
    add("当前结果：`terminal_only` 与 `full` 的差距为 "
        f"{delta('agent_task_success_accuracy', terminal)}，`generic_health` 与 `full` 的差距为 "
        f"{delta('agent_task_success_accuracy', generic)}。")
    add("")
    return "\n".join(lines)


def write_ablation(report: AblationReport, dataset: Dataset, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "ablation.md"
    js = out_dir / "ablation.json"
    md.write_text(build_markdown(report, dataset), encoding="utf-8")
    js.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js

