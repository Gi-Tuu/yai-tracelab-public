"""复赛扩展结果：规范化与结构校验（纯标准库）。

对齐 `复赛提交结果.schema.json` 的关键语义：
- 一 Case 恰好两个 run_assessments，与 Case 的 run_id 一一对应；
- PASS/FAIL 必须有布尔结论；REJECT/NO_RESULT 必须 null 并给 reason；
- 证据事件 ID 必须能在该 run 的事件里找到；
- Judge 调用关联（call_linkage）必须可追溯。
"""

from __future__ import annotations

from typing import Any

from .judge_input import FORBIDDEN_KEYS
from .load import FinalRoundFormatError

VERDICTS = frozenset({"PASS", "FAIL", "REJECT", "NO_RESULT"})
RESULT_REQUIRED: tuple[str, ...] = (
    "case_id",
    "business_type",
    "dataset_version",
    "data_purpose",
    "annotation_version",
    "rubric_version",
    "config_version",
    "run_assessments",
    "version_comparison",
)
ASSESSMENT_REQUIRED: tuple[str, ...] = (
    "run_id",
    "agent_version",
    "judge_id",
    "judge_version",
    "verdict",
    "agent_task_success",
    "business_result_correct",
    "reason",
    "dimensions",
    "evidence_event_ids",
    "first_fault_event_id",
    "root_causes",
    "uncertainty",
    "duration_ms",
    "call_linkage",
)
LINKAGE_KEYS: tuple[str, ...] = (
    "task_id",
    "call_id",
    "attempt_id",
    "input_snapshot_id",
    "judge_input_sha256",
    "model",
    "prompt_version",
    "execution_origin",
)
ROOT_CAUSE_KEYS: tuple[str, ...] = (
    "label",
    "first_fault_event_id",
    "evidence_event_ids",
    "explanation",
)
DIMENSION_KEYS: tuple[str, ...] = ("name", "conclusion", "reason", "evidence_event_ids")


def _event_ids(run: dict[str, Any]) -> set[str]:
    return {str(event.get("event_id")) for event in run.get("events") or [] if event.get("event_id")}


def _normalize_dimensions(raw: Any, verdict: str, reason: str) -> list[dict[str, Any]]:
    if isinstance(raw, list) and raw:
        dimensions = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            dimensions.append(
                {
                    "name": str(item.get("name") or "judge_dimension"),
                    "conclusion": str(item.get("conclusion") or ("FAIL" if verdict == "FAIL" else "UNKNOWN")),
                    "reason": str(item.get("reason") or reason),
                    "evidence_event_ids": [str(x) for x in item.get("evidence_event_ids") or []],
                }
            )
        if dimensions:
            return dimensions
    conclusion = "NOT_APPLICABLE" if verdict in {"REJECT", "NO_RESULT"} else ("PASS" if verdict == "PASS" else "FAIL")
    return [
        {
            "name": "judge_verdict",
            "conclusion": conclusion,
            "reason": reason,
            "evidence_event_ids": [],
        }
    ]


def normalize_assessment(
    run: dict[str, Any],
    verdict: dict[str, Any],
    *,
    judge_id: str,
    judge_version: str,
    call_linkage: dict[str, Any],
    duration_ms: float | None = None,
) -> dict[str, Any]:
    """把 Judge 的原始返回规范化为扩展结果单 run 条目。"""
    if not isinstance(verdict, dict):
        raise FinalRoundFormatError("Judge 返回必须是 object")
    value = str(verdict.get("verdict") or "").upper()
    if value not in VERDICTS:
        raise FinalRoundFormatError(f"未知 verdict: {verdict.get('verdict')!r}")
    reason = str(verdict.get("reason") or "").strip()
    if not reason:
        raise FinalRoundFormatError("verdict 必须给出 reason")
    if value in {"REJECT", "NO_RESULT"}:
        success = None
        business_ok = None
        invoice_created = None
        uncertainty = verdict.get("uncertainty") or {
            "level": "high" if value == "REJECT" else "unknown",
            "reason": reason,
        }
    else:
        success = verdict.get("agent_task_success")
        if not isinstance(success, bool):
            raise FinalRoundFormatError(f"{value} 必须给出布尔 agent_task_success")
        business_ok = verdict.get("business_result_correct")
        if business_ok is not None and not isinstance(business_ok, bool):
            raise FinalRoundFormatError("business_result_correct 必须是 bool 或 null")
        invoice_created = verdict.get("business_invoice_created")
        if invoice_created is not None and not isinstance(invoice_created, bool):
            raise FinalRoundFormatError("business_invoice_created 必须是 bool 或 null")
        uncertainty = verdict.get("uncertainty") or {"level": "low", "reason": "单调确定性判断"}
    if not isinstance(uncertainty, dict) or not uncertainty.get("level") or not uncertainty.get("reason"):
        raise FinalRoundFormatError("uncertainty 必须是含 level/reason 的 object")

    evidence = [str(x) for x in verdict.get("evidence_event_ids") or []]
    first_fault = verdict.get("first_fault_event_id")
    if first_fault is not None:
        first_fault = str(first_fault)
    root_causes = []
    for item in verdict.get("root_causes") or []:
        if not isinstance(item, dict):
            continue
        root_causes.append(
            {
                "label": str(item.get("label") or ""),
                "first_fault_event_id": item.get("first_fault_event_id"),
                "evidence_event_ids": [str(x) for x in item.get("evidence_event_ids") or []],
                "explanation": str(item.get("explanation") or ""),
            }
        )
    assessment: dict[str, Any] = {
        "run_id": run["run_id"],
        "agent_version": run["agent_version"],
        "judge_id": judge_id,
        "judge_version": judge_version,
        "verdict": value,
        "agent_task_success": success,
        "business_result_correct": business_ok,
        "reason": reason,
        "dimensions": _normalize_dimensions(verdict.get("dimensions"), value, reason),
        "evidence_event_ids": evidence,
        "first_fault_event_id": first_fault,
        "root_causes": root_causes,
        "uncertainty": {"level": str(uncertainty["level"]), "reason": str(uncertainty["reason"])},
        "duration_ms": duration_ms if duration_ms is not None else verdict.get("duration_ms"),
        "call_linkage": call_linkage,
    }
    if "business_invoice_created" in verdict or run.get("invoice_created") is not None:
        assessment["business_invoice_created"] = invoice_created
    return assessment


def compare_versions(
    first: dict[str, Any] | list[dict[str, Any]],
    second: dict[str, Any] | None = None,
    *,
    baseline_business_type: str | None = None,
    candidate_business_type: str | None = None,
    baseline_rubric_version: str | None = None,
    candidate_rubric_version: str | None = None,
) -> dict[str, Any]:
    """复赛版本比较：只用四态语义，不把布尔差异换算成无依据质量分。"""
    baseline: dict[str, Any] | None
    candidate: dict[str, Any] | None
    if isinstance(first, list):
        if len(first) != 2:
            return {"direction": "insufficient_evidence", "reason": "版本比较需要恰好两条 run 评估"}
        baseline, candidate = first[0], first[1]
    else:
        baseline, candidate = first, second
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        return {"direction": "insufficient_evidence", "reason": "版本比较缺少完整评估记录"}
    if baseline_business_type and candidate_business_type and baseline_business_type != candidate_business_type:
        return {"direction": "insufficient_evidence", "reason": "两侧业务目标不同，不能直接比较版本优劣"}
    if baseline_rubric_version and candidate_rubric_version and baseline_rubric_version != candidate_rubric_version:
        return {"direction": "insufficient_evidence", "reason": "两侧规则版本不同，不能直接比较版本优劣"}
    va = str(baseline.get("verdict") or "")
    vb = str(candidate.get("verdict") or "")
    if va not in VERDICTS or vb not in VERDICTS:
        return {"direction": "insufficient_evidence", "reason": "存在无效判定，无法比较版本优劣"}
    if va in {"REJECT", "NO_RESULT"} or vb in {"REJECT", "NO_RESULT"}:
        return {
            "direction": "insufficient_evidence",
            "reason": f"存在拒判/无效结果（{va} vs {vb}），不强制给出版本优劣",
        }
    if va == "PASS" and vb == "FAIL":
        return {"direction": "baseline_better", "reason": "baseline PASS 而 candidate FAIL"}
    if va == "FAIL" and vb == "PASS":
        return {"direction": "candidate_better", "reason": "baseline FAIL 而 candidate PASS"}
    if va == "PASS" and vb == "PASS":
        return {
            "direction": "equal",
            "reason": "两侧均 PASS，仅能记录业务目标达成相同，不能断言过程质量完全一致",
        }
    return {
        "direction": "insufficient_evidence",
        "reason": "两侧均 FAIL，缺少可比较的过程证据或业务结果差异",
    }


def build_case_result(
    case: dict[str, Any],
    assessments: list[dict[str, Any]],
    *,
    annotation_version: str,
    rubric_version: str,
    config_version: str,
    legacy_result_ref: str | None = None,
    version_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "case_id": case["case_id"],
        "business_type": case["business_type"],
        "dataset_version": case["dataset_version"],
        "data_purpose": case["data_purpose"],
        "annotation_version": annotation_version,
        "rubric_version": rubric_version,
        "config_version": config_version,
        "run_assessments": assessments,
        "version_comparison": version_comparison
        or {"direction": "insufficient_evidence", "reason": "Judge 为占位/未执行，不产生版本优劣结论"},
    }
    if legacy_result_ref is not None:
        result["legacy_result_ref"] = legacy_result_ref
    return result


def _check_evidence(ids: Any, event_ids: set[str], where: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(ids, list):
        return [f"{where}.evidence_event_ids 必须是数组"]
    for item in ids:
        if str(item) not in event_ids:
            issues.append(f"{where} 引用了不存在的事件 {item!r}")
    return issues




def validate_assessment_evidence(assessment: Any, run: dict[str, Any]) -> list[str]:
    """校验单条评估的证据引用是否全部落在当前 Run 的事件里。

    这是 Controls 与普通 Case 共用的证据核对核心；上层只负责把失败转成
    NO_RESULT。空 Run 的事件集合同样会拒绝非空引用。
    """
    if not isinstance(assessment, dict):
        return ["assessment 不是 object"]
    event_ids = _event_ids(run)
    issues: list[str] = []
    issues.extend(_check_evidence(assessment.get("evidence_event_ids"), event_ids, "assessment"))
    first_fault = assessment.get("first_fault_event_id")
    if first_fault is not None and str(first_fault) not in event_ids:
        issues.append(f"assessment.first_fault_event_id 不存在: {first_fault!r}")
    for index, cause in enumerate(assessment.get("root_causes") or []):
        where = f"assessment.root_causes[{index}]"
        if not isinstance(cause, dict):
            issues.append(f"{where} 不是 object")
            continue
        issues.extend(_check_evidence(cause.get("evidence_event_ids"), event_ids, where))
        cause_first = cause.get("first_fault_event_id")
        if cause_first is not None and str(cause_first) not in event_ids:
            issues.append(f"{where}.first_fault_event_id 不存在: {cause_first!r}")
    for index, dimension in enumerate(assessment.get("dimensions") or []):
        where = f"assessment.dimensions[{index}]"
        if not isinstance(dimension, dict):
            issues.append(f"{where} 不是 object")
            continue
        issues.extend(_check_evidence(dimension.get("evidence_event_ids"), event_ids, where))
    return issues


def validate_case_result(result: Any, case: dict[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    if not isinstance(result, dict):
        return ["结果不是 JSON object"]
    for key in RESULT_REQUIRED:
        if key not in result:
            issues.append(f"缺少字段 {key}")
    if issues:
        return issues
    if result["business_type"] not in {"invoice", "ai_consultant"}:
        issues.append(f"未知 business_type: {result['business_type']!r}")
    assessment = result["run_assessments"]
    if not isinstance(assessment, list) or len(assessment) != 2:
        return issues + ["run_assessments 必须恰好 2 条"]
    if case is not None:
        if result["case_id"] != case["case_id"]:
            issues.append("case_id 与 Case 不一致")
        if result["business_type"] != case["business_type"]:
            issues.append("business_type 与 Case 不一致")
        case_runs = {run["run_id"]: run for run in case["runs"]}
        result_runs = {entry.get("run_id") for entry in assessment if isinstance(entry, dict)}
        if set(case_runs) != result_runs:
            issues.append(f"run_assessments 与 Case run_id 不匹配：{sorted(case_runs)} != {sorted(map(str, result_runs))}")
    else:
        case_runs = {}

    for index, entry in enumerate(assessment):
        where = f"run_assessments[{index}]"
        if not isinstance(entry, dict):
            issues.append(f"{where} 不是 object")
            continue
        for key in ASSESSMENT_REQUIRED:
            if key not in entry:
                issues.append(f"{where} 缺少 {key}")
        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            issues.append(f"{where}.verdict 非法: {verdict!r}")
            continue
        if not str(entry.get("reason") or "").strip():
            issues.append(f"{where}.reason 不能为空")
        if verdict in {"REJECT", "NO_RESULT"}:
            if entry.get("agent_task_success") is not None:
                issues.append(f"{where}.{verdict} 时 agent_task_success 必须为 null")
            if entry.get("business_result_correct") is not None:
                issues.append(f"{where}.{verdict} 时 business_result_correct 必须为 null")
        else:
            if not isinstance(entry.get("agent_task_success"), bool):
                issues.append(f"{where}.{verdict} 时 agent_task_success 必须为布尔")
            if verdict == "PASS" and entry.get("agent_task_success") is not True:
                issues.append(f"{where}.PASS 时 agent_task_success 必须为 true")
            if verdict == "FAIL" and entry.get("agent_task_success") is not False:
                issues.append(f"{where}.FAIL 时 agent_task_success 必须为 false")
        if entry.get("business_result_correct") is not None and not isinstance(
            entry.get("business_result_correct"), bool
        ):
            issues.append(f"{where}.business_result_correct 必须是 bool/null")
        if entry.get("business_invoice_created") is not None and not isinstance(
            entry.get("business_invoice_created"), bool
        ):
            issues.append(f"{where}.business_invoice_created 必须是 bool/null")
        if verdict in {"REJECT", "NO_RESULT"} and "business_invoice_created" in entry and entry.get(
            "business_invoice_created"
        ) is not None:
            issues.append(f"{where}.{verdict} 时 business_invoice_created 必须为 null")
        if not isinstance(entry.get("dimensions"), list) or not entry["dimensions"]:
            issues.append(f"{where}.dimensions 必须是非空数组")
        else:
            for d_index, dimension in enumerate(entry["dimensions"]):
                dwhere = f"{where}.dimensions[{d_index}]"
                if not isinstance(dimension, dict):
                    issues.append(f"{dwhere} 不是 object")
                    continue
                for key in DIMENSION_KEYS:
                    if key not in dimension:
                        issues.append(f"{dwhere} 缺少 {key}")
                if dimension.get("conclusion") not in {"PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"}:
                    issues.append(f"{dwhere}.conclusion 非法")
                issues.extend(_check_evidence(dimension.get("evidence_event_ids"), _event_ids(case_runs.get(entry.get("run_id"), {})), dwhere))
        run = case_runs.get(entry.get("run_id"), {})
        event_ids = _event_ids(run)
        issues.extend(_check_evidence(entry.get("evidence_event_ids"), event_ids, where))
        first_fault = entry.get("first_fault_event_id")
        if first_fault is not None and event_ids and str(first_fault) not in event_ids:
            issues.append(f"{where}.first_fault_event_id 不存在: {first_fault!r}")
        for r_index, cause in enumerate(entry.get("root_causes") or []):
            rwhere = f"{where}.root_causes[{r_index}]"
            if not isinstance(cause, dict):
                issues.append(f"{rwhere} 不是 object")
                continue
            for key in ROOT_CAUSE_KEYS:
                if key not in cause:
                    issues.append(f"{rwhere} 缺少 {key}")
            issues.extend(_check_evidence(cause.get("evidence_event_ids"), event_ids, rwhere))
            cause_first = cause.get("first_fault_event_id")
            if cause_first is not None and event_ids and str(cause_first) not in event_ids:
                issues.append(f"{rwhere}.first_fault_event_id 不存在: {cause_first!r}")
        linkage = entry.get("call_linkage")
        if not isinstance(linkage, dict):
            issues.append(f"{where}.call_linkage 必须是 object")
        else:
            for key in LINKAGE_KEYS:
                if not str(linkage.get(key) or "").strip():
                    issues.append(f"{where}.call_linkage 缺少 {key}")
        uncertainty = entry.get("uncertainty")
        if not isinstance(uncertainty, dict) or uncertainty.get("level") not in {
            "low",
            "medium",
            "high",
            "unknown",
        } or not str(uncertainty.get("reason") or "").strip():
            issues.append(f"{where}.uncertainty 非法")
        leaks = sorted(set(entry) & FORBIDDEN_KEYS - {"verdict", "agent_task_success", "business_result_correct", "business_invoice_created", "first_fault_event_id", "root_causes", "uncertainty"})
        if leaks:
            issues.append(f"{where} 含非法字段: {leaks}")
    comparison = result.get("version_comparison")
    if not isinstance(comparison, dict) or comparison.get("direction") not in {
        "baseline_better",
        "candidate_better",
        "equal",
        "insufficient_evidence",
    } or not str(comparison.get("reason") or "").strip():
        issues.append("version_comparison 必须是 {direction, reason}")
    return issues
