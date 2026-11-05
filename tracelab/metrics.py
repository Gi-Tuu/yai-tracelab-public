"""标注指标：标注格式校验、精确率/召回率/首因/证据/方向一致率。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .evaluation import EvaluationResult
else:  # 审计 F6-03：独立导入本模块时，get_type_hints 也要能解析注解
    EvaluationResult = Any  # type: ignore[no-redef]

# ------------------------------------------------------------------------ 指标

class LabelFormatError(ValueError):
    """参考标注结构非法（缺少必需字段 / 类型不符 / 重复主键）。

    审计 F-28 / R2-13：旧实现直接 `obj["case_id"]` / `gold_case["run_assessments"]`，
    标注缺字段会让 CLI 在写完半成品后栈回溯；同一 case_id 两行还会静默后者覆盖前者，
    指标分母凭空变小且无告警。现在在读取阶段就报错，由 CLI 映射为退出码 2。
    """


def validate_label_fields(payload: Mapping[str, Any], *, where: str) -> None:
    """标注运行条目的字段级 schema（审计 F5-01）。

    旧实现只校验到 `run_id`：`root_causes` 里混进字符串时，`score_against_labels`
    会直接抛 `TypeError`（栈回溯、退出码 1），而不是"输入错误、退出码 2"。
    """
    for name in ("agent_task_success", "business_invoice_created"):
        if not isinstance(payload.get(name), bool):
            raise LabelFormatError(f"{where} 缺少布尔字段 {name}（实际 {payload.get(name)!r}）")
    causes = payload.get("root_causes", [])
    if not isinstance(causes, list):
        raise LabelFormatError(f"{where} 的 root_causes 必须是数组（实际 {type(causes).__name__}）")
    for index, cause in enumerate(causes):
        if not isinstance(cause, Mapping):
            raise LabelFormatError(
                f"{where}.root_causes[{index}] 必须是对象（实际 {type(cause).__name__}）"
            )
        label = cause.get("label")
        if not isinstance(label, str) or not label.strip():
            raise LabelFormatError(f"{where}.root_causes[{index}] 缺少非空字符串 label")
        first = cause.get("first_fault_event_id")
        if first is not None and not isinstance(first, str):
            raise LabelFormatError(
                f"{where}.root_causes[{index}].first_fault_event_id 必须是字符串或 null"
            )
        evidence = cause.get("evidence_event_ids", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise LabelFormatError(
                f"{where}.root_causes[{index}].evidence_event_ids 必须是字符串数组"
            )


def load_reference_labels(path: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise LabelFormatError(f"第 {lineno} 行不是合法 JSON：{exc}") from exc
        if not isinstance(obj, Mapping):
            raise LabelFormatError(f"第 {lineno} 行必须是 JSON 对象，实际 {type(obj).__name__}")
        case_id = obj.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise LabelFormatError(f"第 {lineno} 行缺少合法的 case_id（实际 {case_id!r}）")
        if case_id in labels:
            raise LabelFormatError(f"第 {lineno} 行 case_id {case_id!r} 重复定义（前一行已出现）")
        runs = obj.get("run_assessments")
        if not isinstance(runs, list):
            raise LabelFormatError(f"第 {lineno} 行 run_assessments 必须是数组（实际 {type(runs).__name__}）")
        for index, item in enumerate(runs):
            where = f"第 {lineno} 行 run_assessments[{index}]"
            if not isinstance(item, Mapping) or not isinstance(item.get("run_id"), str) or not item.get("run_id"):
                raise LabelFormatError(f"{where} 缺少合法 run_id")
            validate_label_fields(item, where=where)
        labels[case_id] = dict(obj)
    return labels


@dataclass
class Metrics:
    values: dict[str, Any] = field(default_factory=dict)
    per_run: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"values": self.values, "per_run": self.per_run}


def _score_first_fault(
    assessment: Any,
    causes: list[Mapping[str, Any]],
    seen: set[tuple[str, str, str, str]],
    case_id: str,
    run_id: str,
) -> tuple[int, int, int]:
    """首因事件 Exact Match 的计数（审计 B3 拆分）。

    返回 `(hit, total, undefined)`：金标首因为空 → 只计 undefined、不进分母；
    金标按 `(Case, Run, Label, Event)` 四元组去重，同一金标不重复计分。
    """
    hit = total = undefined = 0
    for cause in causes:
        expect = cause.get("first_fault_event_id")
        if not expect:
            undefined += 1
            continue
        key = (str(case_id), str(run_id), str(cause.get("label")), str(expect))
        if key in seen:
            continue
        seen.add(key)
        total += 1
        match = next(
            (v for v in assessment.violations if v.label == cause.get("label")), None
        )
        if match is not None and match.event_id == expect:
            hit += 1
    return hit, total, undefined


def _score_evidence(assessment: Any, target_run: Any) -> tuple[int, int]:
    """证据 ID 有效性计数：evidence_ids 是否真的存在于该 run 的事件里（审计 B3 拆分）。"""
    run_event_ids = {event.event_id for event in target_run.events} if target_run else set()
    valid = total = 0
    for event_id in assessment.evidence_ids:
        total += 1
        valid += int(event_id in run_event_ids)
    return valid, total


def _score_direction(case_result: Any, gold_direction: str | None) -> tuple[int, int]:
    """版本方向计数（审计 B3 拆分）：UNKNOWN/非双 run/拒判方向都不进分母。"""
    base, cand = case_result.base, case_result.cand
    if not (
        gold_direction
        and base is not None
        and cand is not None
        and not base.is_unknown
        and not cand.is_unknown
        and len(case_result.assessments) == 2
        and case_result.comparison.direction != "insufficient_evidence"
    ):
        return 0, 0
    hit = int(case_result.comparison.direction == gold_direction)
    return hit, 1


def _emit_unknown_metric(assessment: Any) -> dict[str, Any]:
    return {
        "run_id": assessment.run_id,
        "verdict": assessment.verdict.value,
        "scored": False,
        "reason": "UNKNOWN：证据不足或评测器未完成判断，不计入准确率分母",
    }


def _emit_scored_metric(
    assessment: Any, gold: Mapping[str, Any], ours: set[str], gold_labels: set[str]
) -> dict[str, Any]:
    return {
        "run_id": assessment.run_id,
        "verdict": assessment.verdict.value,
        "scored": True,
        "task_success": assessment.agent_task_success,
        "gold_task_success": bool(gold["agent_task_success"]),
        "invoice": assessment.business_invoice_created,
        "gold_invoice": bool(gold["business_invoice_created"]),
        "labels": sorted(ours),
        "gold_labels": sorted(gold_labels),
        "first_error": assessment.first_error.event_id if assessment.first_error else None,
        "quality_score": assessment.quality_score,
    }


def score_against_labels(result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]) -> Metrics:
    """与训练参考标注对比（编排层：配对 → 逐项计数 → 组装）。

    三态对齐规则：UNKNOWN 的运行**不进入准确率分母**——评测器没判断出来，
    既不是"判对"也不是"判错"。UNKNOWN 数量单独统计。
    `label_exact_match_rate`（旧名 `label_coverage`，已废弃但保留为同值别名）与
    `root_cause_label_macro_f1` 同口径跳过 UNKNOWN。

    标注配对必须显式：`labels_matched` / `labels_unmatched` 写进 metrics.values，
    CLI 在 unmatched>0 时告警——否则"分母静默变小"没人看得见。
    """
    metrics = Metrics()
    total = 0
    unknown_runs = 0
    task_hit = invoice_hit = direction_hit = 0
    direction_total = 0
    first_hit = first_total = first_undefined = 0
    label_tp = label_fp = label_fn = 0
    evidence_valid = evidence_total = 0
    scored_case_ids: set[str] = set()
    seen_first_fault: set[tuple[str, str, str, str]] = set()

    for cr in result.results:
        gold_case = labels.get(cr.case.case_id)
        if not gold_case:
            continue
        run_gold = {x["run_id"]: x for x in gold_case["run_assessments"]}
        for assessment in cr.assessments:
            gold = run_gold.get(assessment.run_id)
            if not gold:
                continue
            if assessment.is_unknown:
                unknown_runs += 1
                metrics.per_run.append(_emit_unknown_metric(assessment))
                continue
            total += 1
            scored_case_ids.add(cr.case.case_id)
            task_hit += int(assessment.agent_task_success == bool(gold["agent_task_success"]))
            invoice_hit += int(
                assessment.business_invoice_created == bool(gold["business_invoice_created"])
            )
            ours = set(assessment.labels)
            gold_labels = {rc["label"] for rc in gold.get("root_causes", [])}
            label_tp += len(ours & gold_labels)
            label_fp += len(ours - gold_labels)
            label_fn += len(gold_labels - ours)

            hit, counted, undefined = _score_first_fault(
                assessment, list(gold.get("root_causes", [])), seen_first_fault,
                cr.case.case_id, assessment.run_id,
            )
            first_hit += hit
            first_total += counted
            first_undefined += undefined

            valid, evidence_count = _score_evidence(assessment, cr.case.run(assessment.run_id))
            evidence_valid += valid
            evidence_total += evidence_count

            metrics.per_run.append(
                _emit_scored_metric(assessment, gold, ours, gold_labels)
            )

        direction_hit_add, direction_total_add = _score_direction(
            cr, gold_case.get("version_direction_on_this_case")
        )
        direction_hit += direction_hit_add
        direction_total += direction_total_add

    labels_matched, labels_unmatched = _label_run_matching(result, labels)

    def ratio(num: int, den: int) -> float | None:
        """无有效分母时返回 None，绝不伪造 0%。"""
        return round(num / den, 4) if den else None

    label_exact_match_rate = ratio(_runs_scored_with_all_labels(result, labels), total)
    metrics.values = {
        "cases_scored": len(scored_case_ids),
        "runs_scored": total,
        "runs_unknown": unknown_runs,
        "agent_task_success_accuracy": ratio(task_hit, total),
        "business_invoice_created_accuracy": ratio(invoice_hit, total),
        "version_direction_accuracy": ratio(direction_hit, direction_total),
        "version_direction_scored": direction_total,
        "first_fault_event_exact_match": ratio(first_hit, first_total),
        "first_fault_event_scored": first_total,
        "first_fault_exact_match_undefined": first_undefined,
        "root_cause_label_precision": ratio(label_tp, label_tp + label_fp),
        "root_cause_label_recall": ratio(label_tp, label_tp + label_fn),
        "root_cause_label_macro_f1": _macro_f1(result, labels),
        "evidence_id_validity": ratio(evidence_valid, evidence_total),
        "label_exact_match_rate": label_exact_match_rate,
        "label_coverage": label_exact_match_rate,
        "labels_matched": labels_matched,
        "labels_unmatched": labels_unmatched,
    }
    return metrics

def _label_run_matching(
    result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]
) -> tuple[int, int]:
    """统计标注条目与本次评测运行的配对情况（C-13）。

    * matched：标注里的 (case_id, run_id) 在本次评测结果里找到了对应运行；
    * unmatched：找不到（run_id 不一致，或整个 Case 不在本批数据里）。

    unmatched>0 时指标分母会变小，这是事实而非错误；但不允许"静默"发生，
    因此 CLI 会据此打警告，metrics.values 里也保留计数。
    """
    runs_by_case = {
        cr.case.case_id: {a.run_id for a in cr.assessments} for cr in result.results
    }
    matched = unmatched = 0
    for case_id, gold in labels.items():
        known = runs_by_case.get(case_id)
        for gold_run in gold.get("run_assessments", []):
            run_id = gold_run.get("run_id")
            if known is not None and run_id in known:
                matched += 1
            else:
                unmatched += 1
    return matched, unmatched


def _runs_scored_with_all_labels(result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]) -> int:
    """标签完全命中的**已评分**运行数（label_coverage 的分子）。

    必须与分母同口径：UNKNOWN 运行不进入分母，也不能进入分子——否则会出现
    "分子含 UNKNOWN、分母不含"的错位，label_coverage 可能 >100%。
    """
    hits = 0
    for cr in result.results:
        gold = labels.get(cr.case.case_id)
        if not gold:
            continue
        run_gold = {x["run_id"]: x for x in gold["run_assessments"]}
        for a in cr.assessments:
            if a.is_unknown:
                continue
            g = run_gold.get(a.run_id)
            if g and set(a.labels) == {rc["label"] for rc in g.get("root_causes", [])}:
                hits += 1
    return hits


def _macro_f1(result: EvaluationResult, labels: Mapping[str, Mapping[str, Any]]) -> float | None:
    """根因标签 Macro-F1（只在有标注的 Case 上算，且**跳过 UNKNOWN 运行**）。

    UNKNOWN 的标签集是"评测器没能判断"，既不是预测对也不是预测错：
    计为 FP 会凭空制造假阳性，计为 FN 会凭空制造漏报，两种都会污染指标。
    """
    all_labels = sorted({lab for g in labels.values() for ra in g["run_assessments"] for lab in
                         (rc["label"] for rc in ra.get("root_causes", []))})
    if not all_labels:
        return None
    per_label = []
    for lab in all_labels:
        tp = fp = fn = 0
        for cr in result.results:
            gold = labels.get(cr.case.case_id)
            if not gold:
                continue
            run_gold = {x["run_id"]: x for x in gold["run_assessments"]}
            for a in cr.assessments:
                if a.is_unknown:
                    continue
                g = run_gold.get(a.run_id)
                if not g:
                    continue
                gold_set = {rc["label"] for rc in g.get("root_causes", [])}
                ours = set(a.labels)
                tp += int(lab in ours and lab in gold_set)
                fp += int(lab in ours and lab not in gold_set)
                fn += int(lab not in ours and lab in gold_set)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        if precision is None or recall is None:
            per_label.append(0.0 if (tp + fp + fn) else None)
            continue
        per_label.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    usable = [x for x in per_label if x is not None]
    return round(sum(usable) / len(usable), 4) if usable else None
