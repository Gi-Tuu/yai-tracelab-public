"""双轨结果：初赛原格式映射 + 扩展结果关联。

仅对 `business_type=invoice` 生成初赛原格式（该格式要求布尔和固定字段）；
AI 顾问没有初赛对应格式，只保留扩展结果并显式标注 `legacy_not_applicable`。

正式交付优先使用初赛评测器实际生成的 `submission.jsonl`。本模块的映射入口
只接受评估记录里显式携带的原格式质量分/扣分依据；缺失时返回 None，不编造数字。
"""

from __future__ import annotations

from typing import Any

LEGACY_LABELS = frozenset(
    {
        "INTENT_CLASSIFICATION",
        "ENTITY_RESOLUTION",
        "FIELD_FABRICATION",
        "AMOUNT_BASIS",
        "AMOUNT_CALCULATION",
        "PRODUCT_MATCH",
        "TOOL_ARGUMENT_ERROR",
        "MULTI_ITEM_OMISSION",
        "CONTEXT_UPDATE",
        "DUPLICATE_CONTROL",
        "CONFIRMATION_BYPASS",
        "TOOL_ERROR_HANDLING",
        "CALLBACK_MISINTERPRETATION",
        "INSUFFICIENT_EVIDENCE",
    }
)
RELEASE_BY_DIRECTION = {
    "candidate_better": "release",
    "baseline_better": "defer",
    "equal": "gray",
    "insufficient_evidence": "investigate",
}


def _legacy_assessment(case_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    success = item.get("agent_task_success")
    invoice_created = item.get("business_invoice_created")
    if not isinstance(success, bool) or not isinstance(invoice_created, bool):
        return None
    # 原格式数字必须有明确来源；不从 Judge verdict 反推质量分/扣分。
    quality_score = item.get("legacy_quality_score")
    if not isinstance(quality_score, (int, float)) or isinstance(quality_score, bool):
        return None
    penalty_items = item.get("legacy_penalty_items")
    if not isinstance(penalty_items, list):
        return None
    if not success and not penalty_items:
        return None
    suggestions = item.get("legacy_improvement_suggestions")
    if not success and not (isinstance(suggestions, list) and suggestions):
        return None
    reason = str(item.get("reason") or "").strip() or "Judge 未提供理由"
    dimensions = item.get("dimensions") or []
    dim_text = "；".join(
        f"{d.get('name')}={d.get('conclusion')}" for d in dimensions if isinstance(d, dict)
    ) or reason
    root_causes = []
    for cause in item.get("root_causes") or []:
        if not isinstance(cause, dict):
            continue
        label = str(cause.get("label") or "")
        if label not in LEGACY_LABELS:
            continue
        root_causes.append(
            {
                "label": label,
                "first_fault_event_id": cause.get("first_fault_event_id"),
                "evidence_event_ids": [str(x) for x in cause.get("evidence_event_ids") or []],
                "explanation": str(cause.get("explanation") or reason),
            }
        )
    return {
        "run_id": item["run_id"],
        "agent_task_success": success,
        "business_invoice_created": invoice_created,
        "quality_score": float(quality_score),
        "business_result_assessment": reason,
        "process_assessment": dim_text,
        "penalty_items": penalty_items,
        "root_causes": root_causes,
        "downstream_impact": str(item.get("legacy_downstream_impact") or "见扩展结果中的传播证据"),
        "improvement_suggestions": suggestions or [],
        "evidence_event_ids": [str(x) for x in item.get("evidence_event_ids") or []],
        "explanation": reason,
    }


def to_legacy_case(result: dict[str, Any]) -> dict[str, Any] | None:
    """将一条扩展结果映射为初赛原格式；缺少原格式依据时返回 None（不编造）。"""
    if result.get("business_type") != "invoice":
        return None
    assessments = []
    for item in result.get("run_assessments") or []:
        if not isinstance(item, dict):
            return None
        mapped = _legacy_assessment(str(result.get("case_id")), item)
        if mapped is None:
            return None
        assessments.append(mapped)
    if len(assessments) != 2:
        return None
    comparison = result.get("version_comparison") or {}
    direction = str(comparison.get("direction") or "insufficient_evidence")
    if direction not in RELEASE_BY_DIRECTION:
        direction = "insufficient_evidence"
    return {
        "case_id": result["case_id"],
        "run_assessments": assessments,
        "version_comparison": {
            "direction": direction,
            "release_recommendation": RELEASE_BY_DIRECTION[direction],
            "reason": str(comparison.get("reason") or "扩展结果未给出可比理由"),
        },
    }


def link_extended(result: dict[str, Any], legacy_ref: str | None, *, note: str = "") -> dict[str, Any]:
    linked = dict(result)
    if legacy_ref:
        linked["legacy_result_ref"] = legacy_ref
    if note:
        linked["legacy_note"] = note
    return linked


def _duplicate_ids(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        case_id = str(row.get("case_id") or "")
        if not case_id:
            continue
        if case_id in seen and case_id not in duplicates:
            duplicates.append(case_id)
        seen.add(case_id)
    return duplicates


def validate_dual_track(
    legacy_rows: list[dict[str, Any]],
    extended_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """D2 完整性核对：ID 唯一、关联目标存在、业务类型一致、缺失配对计数。"""
    legacy_ids = {str(row.get("case_id")) for row in legacy_rows if row.get("case_id")}
    extended_ids = [str(row.get("case_id") or "") for row in extended_rows]
    duplicate_legacy = _duplicate_ids(legacy_rows)
    duplicate_extended = _duplicate_ids(extended_rows)
    missing_ref: list[str] = []
    broken_ref: list[str] = []
    mismatched_business: list[str] = []
    linked = 0
    for row in extended_rows:
        case_id = str(row.get("case_id") or "")
        ref = row.get("legacy_result_ref")
        if row.get("business_type") != "invoice":
            if ref:
                mismatched_business.append(case_id)
            continue
        if not ref:
            missing_ref.append(case_id)
            continue
        target = str(ref).split("#", 1)[-1]
        if target not in legacy_ids:
            broken_ref.append(case_id)
            continue
        if target != case_id:
            mismatched_business.append(case_id)
            continue
        linked += 1
    missing_extended = sorted(legacy_ids - set(extended_ids))
    return {
        "legacy_total": len(legacy_rows),
        "extended_total": len(extended_rows),
        "linked": linked,
        "missing_legacy_ref": missing_ref,
        "broken_legacy_ref": broken_ref,
        "mismatched_business": mismatched_business,
        "missing_extended": missing_extended,
        "duplicate_legacy_ids": duplicate_legacy,
        "duplicate_extended_ids": duplicate_extended,
    }
