"""复赛 Judge 指标：配对覆盖、严格标注类型、有效性统计（纯标准库）。

原则：
- 只对成功配对的 run 计算一致率；
- 未配对 / 重复 / 非法记录单独计数；
- 专家布尔必须是真正的 bool；字符串 "false" 不能隐式转成 False；
- 指标写清分子、分母；没有金标时分母为 0 或 N/A。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

VERDICTS = frozenset({"PASS", "FAIL", "REJECT", "NO_RESULT"})


def strict_bool(value: Any) -> bool | None:
    """只接受真正的 bool；其他类型（包括 "false"）视为缺失/非法。"""
    return value if isinstance(value, bool) else None


def _run_items(rows: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    stats: dict[str, Any] = {
        "total_cases": 0,
        "total_runs": 0,
        "invalid_records": 0,
        "duplicate_case_ids": [],
        "duplicate_run_ids": [],
    }
    seen_cases: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            stats["invalid_records"] += 1
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            stats["invalid_records"] += 1
            continue
        stats["total_cases"] += 1
        if case_id in seen_cases:
            stats["duplicate_case_ids"].append(case_id)
        seen_cases.add(case_id)
        assessments = row.get("run_assessments")
        if not isinstance(assessments, list):
            stats["invalid_records"] += 1
            continue
        for item in assessments:
            if not isinstance(item, dict):
                stats["invalid_records"] += 1
                continue
            run_id = item.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                stats["invalid_records"] += 1
                continue
            stats["total_runs"] += 1
            key = (case_id, run_id)
            if key in index:
                stats["duplicate_run_ids"].append(f"{case_id}/{run_id}")
                continue
            index[key] = item
    return index, stats


def pair_runs(
    result_rows: list[dict[str, Any]],
    label_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result_index, result_stats = _run_items(result_rows)
    label_index, label_stats = _run_items(label_rows)
    matched_keys = sorted(set(result_index) & set(label_index))
    pairs = [
        {
            "case_id": case_id,
            "run_id": run_id,
            "result": result_index[(case_id, run_id)],
            "label": label_index[(case_id, run_id)],
        }
        for case_id, run_id in matched_keys
    ]
    stats = {
        "a_total_cases": result_stats["total_cases"],
        "a_total_runs": result_stats["total_runs"],
        "b_total_cases": label_stats["total_cases"],
        "b_total_runs": label_stats["total_runs"],
        "matched": len(pairs),
        "only_a": len(set(result_index) - set(label_index)),
        "only_b": len(set(label_index) - set(result_index)),
        "duplicate_a": result_stats["duplicate_run_ids"],
        "duplicate_b": label_stats["duplicate_run_ids"],
        "duplicate_case_a": result_stats["duplicate_case_ids"],
        "duplicate_case_b": label_stats["duplicate_case_ids"],
        "invalid_a": result_stats["invalid_records"],
        "invalid_b": label_stats["invalid_records"],
    }
    return pairs, stats


def _event_set(events_by_run: dict[Any, set[str]] | None, case_id: str, run_id: str) -> set[str] | None:
    if not events_by_run:
        return None
    composite = (case_id, run_id)
    if composite in events_by_run:
        return set(events_by_run[composite])
    if run_id in events_by_run:
        return set(events_by_run[run_id])
    return None


def _reference_stats(refs: Any, events: set[str] | None) -> dict[str, int | float | None]:
    ids = [str(item) for item in refs] if isinstance(refs, (list, tuple)) else []
    total = len(ids)
    located = missing = unverifiable = 0
    if events is not None:
        for item in ids:
            if item in events:
                located += 1
            else:
                missing += 1
    else:
        unverifiable = total
    denominator = located + missing
    return {
        "total": total,
        "located": located,
        "missing": missing,
        "unverifiable": unverifiable,
        "value": located / denominator if denominator else None,
    }


def compute_metrics(
    pairs: list[dict[str, Any]],
    *,
    events_by_run: dict[Any, set[str]] | None = None,
) -> dict[str, Any]:
    verdict_total = verdict_hit = 0
    task_total = task_hit = 0
    business_total = business_hit = 0
    first_defined = first_hit = 0
    first_both_null = 0
    first_expected = first_located = first_exact = 0
    invalid_label_values = 0
    evidence_total = evidence_valid = evidence_checked = 0
    evidence_categories = {
        "top_level": {"total": 0, "located": 0, "missing": 0, "unverifiable": 0},
        "dimensions": {"total": 0, "located": 0, "missing": 0, "unverifiable": 0},
        "root_causes": {"total": 0, "located": 0, "missing": 0, "unverifiable": 0},
        "first_fault": {"total": 0, "located": 0, "missing": 0, "unverifiable": 0},
    }
    confusion: Counter[tuple[str, str]] = Counter()
    verdict_dist: Counter[str] = Counter()
    gold_dist: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []
    for pair in pairs:
        item, gold = pair["result"], pair["label"]
        verdict = str(item.get("verdict"))
        gold_verdict = str(gold.get("verdict"))
        verdict_total += 1
        verdict_dist[verdict] += 1
        gold_dist[gold_verdict] += 1
        confusion[(gold_verdict, verdict)] += 1
        verdict_hit += verdict == gold_verdict
        ours_task = strict_bool(item.get("agent_task_success"))
        gold_task = strict_bool(gold.get("agent_task_success"))
        if gold.get("agent_task_success") is not None and gold_task is None:
            invalid_label_values += 1
        if ours_task is not None and gold_task is not None:
            task_total += 1
            task_hit += ours_task == gold_task
        ours_business = strict_bool(item.get("business_invoice_created"))
        gold_business = strict_bool(gold.get("business_invoice_created"))
        if ours_business is None:
            ours_business = strict_bool(item.get("business_result_correct"))
        if gold_business is None:
            gold_business = strict_bool(gold.get("business_result_correct"))
        if gold.get("business_invoice_created") is not None and gold_business is None:
            invalid_label_values += 1
        if ours_business is not None and gold_business is not None:
            business_total += 1
            business_hit += ours_business == gold_business
        ours_first = item.get("first_fault_event_id")
        gold_first = gold.get("first_fault_event_id")
        if ours_first is not None or gold_first is not None:
            first_defined += 1
            first_hit += str(ours_first) == str(gold_first)
        else:
            first_both_null += 1
        if gold_first is not None:
            first_expected += 1
            if ours_first is not None:
                first_located += 1
                if str(ours_first) == str(gold_first):
                    first_exact += 1
        events = _event_set(events_by_run, str(pair["case_id"]), str(pair["run_id"]))
        ref_groups = {
            "top_level": item.get("evidence_event_ids") or [],
            "dimensions": [
                evidence
                for dimension in (item.get("dimensions") or [])
                if isinstance(dimension, dict)
                for evidence in (dimension.get("evidence_event_ids") or [])
            ],
            "root_causes": [
                evidence
                for cause in (item.get("root_causes") or [])
                if isinstance(cause, dict)
                for evidence in (cause.get("evidence_event_ids") or [])
            ],
            "first_fault": [item.get("first_fault_event_id")] if item.get("first_fault_event_id") is not None else [],
        }
        for category, refs in ref_groups.items():
            stats = _reference_stats(refs, events)
            for key in ("total", "located", "missing", "unverifiable"):
                evidence_categories[category][key] += int(stats[key] or 0)
        for evidence in ref_groups["top_level"]:
            evidence_total += 1
            if events is not None:
                evidence_checked += 1
                evidence_valid += str(evidence) in events
        if verdict != gold_verdict or (
            ours_task is not None and gold_task is not None and ours_task != gold_task
        ):
            mismatches.append(
                {
                    "case_id": pair["case_id"],
                    "run_id": pair["run_id"],
                    "judge_verdict": verdict,
                    "gold_verdict": gold_verdict,
                    "judge_task": item.get("agent_task_success"),
                    "gold_task": gold.get("agent_task_success"),
                    "reason": str(item.get("reason") or "")[:300],
                }
            )
    valid_verdicts = sum(count for verdict, count in verdict_dist.items() if verdict in VERDICTS)
    effective = sum(count for verdict, count in verdict_dist.items() if verdict in {"PASS", "FAIL", "REJECT"})
    no_result = verdict_dist.get("NO_RESULT", 0)
    invalid = sum(count for verdict, count in verdict_dist.items() if verdict not in VERDICTS)
    return {
        "runs": verdict_total,
        "total_runs": verdict_total,
        "valid_verdicts": valid_verdicts,
        "effective_judgments": effective,
        "no_result": no_result,
        "invalid_verdicts": invalid,
        "verdict_agreement": {"hit": verdict_hit, "total": verdict_total, "value": verdict_hit / verdict_total if verdict_total else None},
        "task_agreement": {"hit": task_hit, "total": task_total, "value": task_hit / task_total if task_total else None},
        "business_agreement": {"hit": business_hit, "total": business_total, "value": business_hit / business_total if business_total else None},
        "first_fault": {
            "hit": first_hit,
            "total": first_defined,
            "both_null": first_both_null,
            "coverage": first_defined / verdict_total if verdict_total else None,
            "expected": first_expected,
            "located": first_located,
            "exact_match": first_exact,
            "location_coverage": first_located / first_expected if first_expected else None,
            "exact_match_rate": first_exact / first_expected if first_expected else None,
        },
        "first_fault_expected": first_expected,
        "first_fault_located": first_located,
        "first_fault_exact_match": first_exact,
        "first_fault_both_null": first_both_null,
        "evidence_by_category": {
            category: {
                **stats,
                "value": (stats["located"] / (stats["located"] + stats["missing"]))
                if (stats["located"] + stats["missing"])
                else None,
            }
            for category, stats in evidence_categories.items()
        },
        "evidence_reference_validity": {
            "valid": evidence_valid,
            "total": evidence_total,
            "value": evidence_valid / evidence_checked if evidence_checked else None,
        },
        "invalid_label_values": invalid_label_values,
        "confusion": {f"{g}->{j}": n for (g, j), n in sorted(confusion.items())},
        "judge_distribution": dict(verdict_dist),
        "gold_distribution": dict(gold_dist),
        "mismatches": mismatches,
    }
