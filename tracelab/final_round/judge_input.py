"""Judge 输入白名单、输入快照与防泄漏检查（纯标准库）。

复赛明确要求：Judge 只能看业务目标/范围/输入资料/适用规则/单 run 可观测轨迹；
禁止当前 run 的人工结论、首错、根因、专家解释、分数、版本优劣，也禁止读参考文件查答案。

普通 Case 与专项 Control 必须走同一个 ``normalize_judge_input``：
先筛字段、再校验事件、最后由程序计算摘要；外部旧摘要只用于核对，不作为唯一依据。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .load import FinalRoundFormatError

CONTEXT_KEYS: tuple[str, ...] = (
    "enterprise_id",
    "accounting_period",
    "current_date",
    "facts",
    "input_materials",
    "scope",
)
OBJECTIVE_KEYS: tuple[str, ...] = (
    "business_goal",
    "pass_criteria",
    "required_evidence",
    "rule_source",
    "reference_semantics",
)
BUSINESS_TYPES: frozenset[str] = frozenset({"invoice", "ai_consultant"})

JUDGE_INPUT_KEYS: tuple[str, ...] = (
    "business_type",
    "domain",
    "user_request",
    "context",
    "business_objective",
    "observable_trace",
)
#: Judge 输入中绝不允许出现的"答案类"字段（递归检查）。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "run_assessments",
        "verdict",
        "agent_task_success",
        "business_result_correct",
        "business_invoice_created",
        "first_fault_event_id",
        "root_causes",
        "explanation",
        "downstream_impact",
        "review_status",
        "review_confirmation_source",
        "status_label",
        "annotation_version",
        "uncertainty",
    }
)


class JudgeInputContractError(FinalRoundFormatError):
    """Judge 输入不符合统一契约。"""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _scan_forbidden(obj: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                found.append(child)
            found.extend(_scan_forbidden(value, child))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_scan_forbidden(value, f"{path}[{index}]"))
    return found


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """事件只保留可观测字段；不把 run 级结果（invoice_created）塞进来。"""
    allowed = (
        "event_id",
        "seq",
        "step_id",
        "module",
        "action",
        "step_status",
        "started_at",
        "finished_at",
        "duration_ms",
        "result_code",
        "error_code",
        "error_message",
        "retry_count",
        "observed_input",
        "observed_output",
        "tool_calls",
    )
    payload = {key: event[key] for key in allowed if key in event}
    leaks = _scan_forbidden(payload)
    if leaks:
        raise JudgeInputContractError(f"Judge 输入事件含答案字段: {', '.join(leaks[:5])}")
    return payload


def validate_observable_trace(events: Any, *, run_id: str | None = None) -> list[str]:
    """校验事件编号/顺序/与来源 run 的对应关系（不校验业务对错）。"""
    issues: list[str] = []
    if not isinstance(events, list) or not events:
        return ["observable_trace 必须是非空数组"]
    seen: set[str] = set()
    last_seq = 0
    ids: list[str] = []
    for index, event in enumerate(events):
        where = f"observable_trace[{index}]"
        if not isinstance(event, dict):
            issues.append(f"{where} 不是 object")
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            issues.append(f"{where}.event_id 必须是非空字符串")
        elif event_id in seen:
            issues.append(f"event_id 重复: {event_id}")
        else:
            seen.add(event_id)
            ids.append(event_id)
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
            issues.append(f"{where}.seq 必须严格递增（当前 {seq!r}，上一条 {last_seq}）")
        else:
            last_seq = seq
    if run_id and ids:
        matching = [event_id for event_id in ids if event_id.startswith(f"{run_id}-")]
        if matching and len(matching) != len(ids):
            issues.append(f"事件编号与来源 run 不一致：run_id={run_id}")
    return issues


def _normalize_trace(raw: dict[str, Any], linkage: dict[str, Any]) -> list[dict[str, Any]]:
    trace = raw.get("observable_trace")
    legacy = raw.get("observable_run")
    if trace is None and isinstance(legacy, dict):
        trace = legacy.get("events")
    elif legacy is not None and trace is not None:
        # 两个字段同时存在时，必须表达同一组事件，避免"看一份、存另一份"。
        legacy_events = legacy.get("events") if isinstance(legacy, dict) else legacy
        if legacy_events != trace:
            raise JudgeInputContractError("observable_trace 与 observable_run 内容不一致")
    if not isinstance(trace, list):
        raise JudgeInputContractError("缺少 observable_trace（或 observable_run.events）")
    issues = validate_observable_trace(trace, run_id=str(linkage.get("run_id") or "") or None)
    if issues:
        raise JudgeInputContractError("；".join(issues[:6]))
    return [_sanitize_event(event) for event in trace]


def normalize_judge_input(
    raw: Any,
    *,
    linkage: dict[str, Any] | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """统一两类入参：字段白名单、格式校验、事件校验、摘要计算与旧摘要核对。"""
    if not isinstance(raw, dict):
        raise JudgeInputContractError("Judge 输入必须是 object")
    linkage = dict(linkage or {})
    # 先对完整入参做一次递归禁字段扫描，防止调用方把答案藏在非白名单字段里。
    leaks = _scan_forbidden(raw)
    if leaks:
        raise JudgeInputContractError(f"Judge 输入含答案字段: {', '.join(leaks[:6])}")
    business_type = raw.get("business_type")
    if not isinstance(business_type, str) or not business_type.strip():
        raise JudgeInputContractError("business_type 必须是非空字符串")
    if business_type not in BUSINESS_TYPES:
        raise JudgeInputContractError(
            f"未知 business_type: {business_type!r}；仅支持 {sorted(BUSINESS_TYPES)}"
        )
    if not isinstance(raw.get("user_request"), str) or not raw["user_request"].strip():
        raise JudgeInputContractError("user_request 必须是非空字符串")
    context = raw.get("context")
    if not isinstance(context, dict):
        raise JudgeInputContractError("context 必须是 object")
    objective = raw.get("business_objective")
    if objective is not None and not isinstance(objective, dict):
        raise JudgeInputContractError("business_objective 必须是 object 或 null")

    judge_input = {
        "business_type": raw["business_type"],
        "domain": raw.get("domain"),
        "user_request": raw["user_request"],
        "context": {key: context[key] for key in CONTEXT_KEYS if key in context},
        "business_objective": {
            key: (objective or {})[key] for key in OBJECTIVE_KEYS if key in (objective or {})
        },
        "observable_trace": _normalize_trace(raw, linkage),
    }
    # 只允许白名单字段进入 Judge；多余字段一律丢弃，不静默透传。
    normalized = {key: judge_input[key] for key in JUDGE_INPUT_KEYS}
    digest = sha256_json({"judge_input": normalized, "linkage": linkage})
    if expected_sha256:
        raw_digest = sha256_json({"judge_input": raw, "linkage": linkage})
        if expected_sha256 not in {digest, raw_digest}:
            raise JudgeInputContractError(
                "Judge 输入摘要不匹配：外部摘要与规范化输入/原始输入均不一致"
            )
    return {
        "judge_input": normalized,
        "linkage_only": linkage,
        "judge_input_sha256": digest,
        "input_snapshot_id": f"js-{digest[:16]}",
    }


def build_judge_input(case: dict[str, Any], run: dict[str, Any], *, run_index: int) -> dict[str, Any]:
    """构造单 run 的 Judge 输入与 linkage；返回值可直接序列化为快照。"""
    reference = case.get("reference_result") or {}
    context = case.get("context") or {}
    objective = {key: reference[key] for key in OBJECTIVE_KEYS if key in reference}
    raw_input = {
        "business_type": case["business_type"],
        "domain": case.get("domain"),
        "user_request": case["user_request"],
        "context": {
            key: context[key] for key in CONTEXT_KEYS if key in context
        },
        "business_objective": objective,
        "observable_trace": [_sanitize_event(event) for event in run.get("events") or []],
    }
    linkage = {
        "case_id": case["case_id"],
        "run_id": run["run_id"],
        "task_id": run["task_id"],
        "trace_id": run["trace_id"],
        "agent_version": run["agent_version"],
        "business_type": case["business_type"],
        "domain": case.get("domain"),
        "dataset_version": case["dataset_version"],
        "data_purpose": case["data_purpose"],
        "run_index": run_index,
    }
    return normalize_judge_input(raw_input, linkage=linkage)
