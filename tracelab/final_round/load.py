"""复赛数据加载与结构校验（纯标准库，不依赖官方资料包路径）。

复赛 Case 是"一 Case 两 run"的 A/B 轨迹；这里只做**结构/一致性**校验，
不做业务判断，也不读取参考标注（标注在独立文件里，Judge 流程无权使用）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASE_REQUIRED: tuple[str, ...] = (
    "dataset_version",
    "data_origin",
    "case_id",
    "business_type",
    "domain",
    "data_purpose",
    "near_duplicate_group",
    "user_request",
    "context",
    "reference_result",
    "runs",
)
RUN_REQUIRED: tuple[str, ...] = (
    "run_id",
    "agent_version",
    "task_id",
    "trace_id",
    "events",
    "final_state",
    "snapshot_state",
    "workflow_terminal",
    "observation_complete",
)
EVENT_REQUIRED: tuple[str, ...] = (
    "event_id",
    "seq",
    "step_id",
    "step_status",
    "observed_input",
    "observed_output",
    "tool_calls",
)
REFERENCE_REQUIRED: tuple[str, ...] = (
    "business_goal",
    "pass_criteria",
    "required_evidence",
    "rule_source",
    "reference_semantics",
)
BUSINESS_TYPES = frozenset({"invoice", "ai_consultant"})
CASE_FORBIDDEN_KEYS = frozenset(
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
    }
)


class FinalRoundFormatError(ValueError):
    """复赛输入/输出结构不合规。"""


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_case(case: Any) -> list[str]:
    """返回单个 Case 的结构问题列表（空列表=通过）。"""
    issues: list[str] = []
    if not isinstance(case, dict):
        return ["Case 不是 JSON object"]
    for key in CASE_REQUIRED:
        if key not in case:
            issues.append(f"缺少字段 {key}")
    if issues:
        return issues

    if case["business_type"] not in BUSINESS_TYPES:
        issues.append(f"未知 business_type: {case['business_type']!r}")
    if not _is_str(case["case_id"]):
        issues.append("case_id 必须是非空字符串")
    if not _is_str(case["dataset_version"]):
        issues.append("dataset_version 必须是非空字符串")
    if not _is_str(case["data_purpose"]):
        issues.append("data_purpose 必须是非空字符串")
    if not _is_str(case["user_request"]):
        issues.append("user_request 必须是非空字符串")
    if not isinstance(case["context"], dict):
        issues.append("context 必须是 object")
    reference = case["reference_result"]
    if not isinstance(reference, dict):
        issues.append("reference_result 必须是 object")
    else:
        for key in REFERENCE_REQUIRED:
            if key not in reference:
                issues.append(f"reference_result 缺少 {key}")
        if not isinstance(reference.get("pass_criteria"), list):
            issues.append("reference_result.pass_criteria 必须是数组")
        if not isinstance(reference.get("required_evidence"), list):
            issues.append("reference_result.required_evidence 必须是数组")
        if not isinstance(reference.get("rule_source"), dict):
            issues.append("reference_result.rule_source 必须是 object")

    # Case 里不应出现任何逐 run 的参考结论（防答案随输入泄漏）
    for key in CASE_FORBIDDEN_KEYS:
        if key in case:
            issues.append(f"Case 顶层不应包含参考字段 {key}")

    runs = case["runs"]
    if not isinstance(runs, list) or len(runs) != 2:
        issues.append(f"runs 必须恰好 2 条，实际 {len(runs) if isinstance(runs, list) else type(runs).__name__}")
        return issues

    run_ids: list[str] = []
    event_ids: list[str] = []
    for index, run in enumerate(runs):
        where = f"runs[{index}]"
        if not isinstance(run, dict):
            issues.append(f"{where} 不是 object")
            continue
        for key in RUN_REQUIRED:
            if key not in run:
                issues.append(f"{where} 缺少 {key}")
        if not _is_str(run.get("run_id")):
            issues.append(f"{where}.run_id 必须是非空字符串")
        else:
            if run["run_id"] in run_ids:
                issues.append(f"run_id 重复: {run['run_id']}")
            run_ids.append(str(run["run_id"]))
        for key in ("task_id", "trace_id", "agent_version"):
            if not _is_str(run.get(key)):
                issues.append(f"{where}.{key} 必须是非空字符串")
        if not isinstance(run.get("workflow_terminal"), bool):
            issues.append(f"{where}.workflow_terminal 必须是 bool")
        if not isinstance(run.get("observation_complete"), bool):
            issues.append(f"{where}.observation_complete 必须是 bool")
        # invoice_created 是 run 结果字段，允许存在，但不进入 Judge 输入
        events = run.get("events")
        if not isinstance(events, list) or not events:
            issues.append(f"{where}.events 必须是非空数组")
            continue
        last_seq = 0
        for e_index, event in enumerate(events):
            ewhere = f"{where}.events[{e_index}]"
            if not isinstance(event, dict):
                issues.append(f"{ewhere} 不是 object")
                continue
            for key in EVENT_REQUIRED:
                if key not in event:
                    issues.append(f"{ewhere} 缺少 {key}")
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                issues.append(f"{ewhere}.seq 必须严格递增（当前 {seq!r}，上一条 {last_seq}）")
            else:
                last_seq = seq
            event_id = event.get("event_id")
            if not _is_str(event_id):
                issues.append(f"{ewhere}.event_id 必须是非空字符串")
            elif event_id in event_ids:
                issues.append(f"event_id 重复: {event_id}")
            else:
                event_ids.append(str(event_id))
            if not isinstance(event.get("observed_input"), dict):
                issues.append(f"{ewhere}.observed_input 必须是 object")
            if not isinstance(event.get("observed_output"), dict):
                issues.append(f"{ewhere}.observed_output 必须是 object")
            if not isinstance(event.get("tool_calls"), list):
                issues.append(f"{ewhere}.tool_calls 必须是数组")
    return issues


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    """跨 Case 一致性校验：dataset_version / data_purpose 不应混批，case_id 必须唯一。"""
    issues: list[str] = []
    seen_ids: set[str] = set()
    for case in cases:
        case_id = case.get("case_id") if isinstance(case, dict) else None
        if isinstance(case_id, str) and case_id:
            if case_id in seen_ids:
                issues.append(f"case_id 重复：{case_id}")
            seen_ids.add(case_id)
    versions = {case.get("dataset_version") for case in cases}
    purposes = {case.get("data_purpose") for case in cases}
    if len(versions) > 1:
        issues.append(f"dataset_version 不一致：{sorted(map(str, versions))}")
    if len(purposes) > 1:
        issues.append(f"data_purpose 不一致：{sorted(map(str, purposes))}")
    return issues


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """读取 JSONL；空行跳过，非法 JSON 直接抛错（带行号）。"""
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FinalRoundFormatError(f"{path}:{lineno} 不是合法 JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise FinalRoundFormatError(f"{path}:{lineno} 不是 JSON object")
        rows.append(obj)
    return rows


def load_cases(path: str | Path, *, strict: bool = True) -> list[dict[str, Any]]:
    """加载复赛 Case JSONL 并校验；strict=False 时仅返回数据。"""
    cases = read_jsonl(path)
    issues: list[str] = []
    for index, case in enumerate(cases):
        case_issues = validate_case(case)
        issues.extend(f"第 {index + 1} 个 Case: {msg}" for msg in case_issues)
    issues.extend(validate_cases(cases))
    if strict and issues:
        raise FinalRoundFormatError("\n".join(issues[:50]))
    return cases
