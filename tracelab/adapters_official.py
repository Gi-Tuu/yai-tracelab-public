"""官方 JSONL 适配器：按赛事给定 schema 解析为 Trace IR。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters_common import (
    REQUIRED_EVENT_FIELDS,
    _as_mapping,
    _load_jsonl,
    _looks_like_business_step,
    _optional_str,
    _required_bool,
    _required_object,
    _required_str,
    _safe_int,
)
from .contracts import ADAPTER_OFFICIAL_VERSION, STEP_IDS, Case, Dataset, Event, IntegrityIssue, Run, ToolCall

#: 事件里登记为"日期时间"的字段（只作为字符串保留，不参与判定）。
_DATETIME_FIELDS = ("started_at", "finished_at")

def _parse_tool_call(raw: dict[str, Any], scope: str, issues: list[IntegrityIssue]) -> ToolCall:
    attempt = raw.get("attempt")
    arguments = raw.get("arguments")
    result = raw.get("result")
    if arguments is not None and not isinstance(arguments, dict):
        issues.append(IntegrityIssue(scope, "BAD_TOOL_ARGUMENTS",
                                     f"tool arguments 期望 object，实际 {type(arguments).__name__}",
                                     "error", "schema"))
        arguments = {}
    if result is not None and not isinstance(result, dict):
        issues.append(IntegrityIssue(scope, "BAD_TOOL_RESULT",
                                     f"tool result 期望 object，实际 {type(result).__name__}",
                                     "error", "schema"))
        result = {}
    return ToolCall(
        name=str(raw.get("name") or raw.get("tool") or "unknown"),
        arguments=arguments or {},
        result=result or {},
        attempt=attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None,
        raw=raw,
    )

def _parse_tool_calls(
    raw_tools: list[Any], scope: str, issues: list[IntegrityIssue]
) -> tuple[ToolCall, ...]:
    """解析 tool_calls 数组：非 object 元素**记错误**而不是静默丢弃（C-26）。

    旧实现用 `if isinstance(t, dict)` 直接过滤，导致"数据里有 3 个工具调用、
    实际只解析到 1 个"这件事在 IR 上完全不可见；现在每个坏元素都记
    `BAD_TOOL_CALL_TYPE`（error/schema），并在 detail 里保留序号与总数，
    `Dataset.stats["bad_tool_calls"]` 另有一份汇总计数。
    """
    tools: list[ToolCall] = []
    total = len(raw_tools)
    for index, item in enumerate(raw_tools):
        if not isinstance(item, dict):
            issues.append(IntegrityIssue(
                scope, "BAD_TOOL_CALL_TYPE",
                f"tool_calls[{index}] 期望 object，实际 {type(item).__name__}"
                f"（共 {total} 个元素）",
                "error", "schema",
            ))
            continue
        tools.append(_parse_tool_call(item, scope, issues))
    return tuple(tools)

def _parse_event(raw: dict[str, Any], scope: str, issues: list[IntegrityIssue]) -> Event:
    missing = [f for f in REQUIRED_EVENT_FIELDS if f not in raw]
    if missing:
        issues.append(IntegrityIssue(scope, "MISSING_EVENT_FIELDS", f"缺字段 {missing}", "error", "schema"))

    observed_input = raw.get("observed_input")
    if observed_input is not None and not isinstance(observed_input, dict):
        issues.append(IntegrityIssue(scope, "BAD_OBSERVED_INPUT",
                                     f"observed_input 期望 object，实际 {type(observed_input).__name__}",
                                     "error", "schema"))
        observed_input = {}
    observed_output = raw.get("observed_output")
    if observed_output is not None and not isinstance(observed_output, dict):
        issues.append(IntegrityIssue(scope, "BAD_OBSERVED_OUTPUT",
                                     f"observed_output 期望 object，实际 {type(observed_output).__name__}",
                                     "error", "schema"))
        observed_output = {}

    raw_tools = raw.get("tool_calls")
    if raw_tools is not None and not isinstance(raw_tools, list):
        issues.append(IntegrityIssue(scope, "BAD_TOOL_CALLS",
                                     f"tool_calls 期望 array，实际 {type(raw_tools).__name__}",
                                     "error", "schema"))
        raw_tools = []

    raw_event_id = raw.get("event_id")
    if isinstance(raw_event_id, str):
        event_id = raw_event_id
    else:
        event_id = ""
        if "event_id" in raw:
            issues.append(IntegrityIssue(
                scope, "BAD_FIELD_TYPE",
                f"event_id 期望 string，实际 {type(raw_event_id).__name__}", "error", "schema",
            ))

    step_id = str(raw.get("step_id", ""))
    if step_id and step_id not in STEP_IDS:
        # 未知步骤名不会被 state 的任何阶段集合接收 → 该阶段事实凭空消失。
        # 审计 F-02 / R2-01：只有"看起来承载业务事实"的未知步骤才升级为 error，
        # 通知/日志类步骤记 warn（否则任何额外步骤都会把整批判成不完整）。
        business_like = _looks_like_business_step(step_id)
        issues.append(IntegrityIssue(
            scope,
            "UNKNOWN_STEP_ID" if business_like else "UNKNOWN_NON_BUSINESS_STEP",
            f"step_id 不在数据字典的流程步骤内：{step_id!r}；"
            + ("该阶段事实不会被任何规则使用" if business_like else "按非业务步骤忽略"),
            "error" if business_like else "warn", "schema",
        ))

    return Event(
        event_id=event_id,
        seq=_safe_int(raw.get("seq"), 0, field="seq", scope=scope, issues=issues,
                      missing="seq" not in raw, minimum=0),
        step_id=step_id,
        module=str(raw.get("module", "")),
        action=str(raw.get("action", "")),
        step_status=str(raw.get("step_status", "")),
        result_code=str(raw.get("result_code", "")),
        error_code=_optional_str(raw, "error_code", scope=scope, issues=issues),
        error_message=_optional_str(raw, "error_message", scope=scope, issues=issues),
        retry_count=_safe_int(raw.get("retry_count"), 0, field="retry_count", scope=scope, issues=issues,
                              missing="retry_count" not in raw, minimum=0),
        observed_input=observed_input or {},
        observed_output=observed_output or {},
        tool_calls=_parse_tool_calls(raw_tools or [], scope, issues),
        started_at=str(raw.get("started_at", "")),
        finished_at=str(raw.get("finished_at", "")),
        duration_ms=_safe_int(raw.get("duration_ms"), 0, field="duration_ms", scope=scope, issues=issues,
                              missing="duration_ms" not in raw, minimum=0),
        raw=raw,
    )

def _parse_run(raw: dict[str, Any], case_id: str, issues: list[IntegrityIssue]) -> Run:
    scope = f"case:{case_id}/run:{raw.get('run_id', '?')}"
    raw_events = raw.get("events")
    if raw_events is None:
        issues.append(IntegrityIssue(scope, "MISSING_EVENTS", "run 缺少 events 数组", "error", "schema"))
        raw_events = []
    elif not isinstance(raw_events, list):
        issues.append(IntegrityIssue(scope, "BAD_EVENTS_TYPE",
                                     f"events 期望 array，实际 {type(raw_events).__name__}", "error", "schema"))
        raw_events = []

    events: list[Event] = []
    for item in raw_events:
        if not isinstance(item, dict):
            issues.append(IntegrityIssue(scope, "BAD_EVENT_TYPE",
                                         f"事件期望 object，实际 {type(item).__name__}", "error", "schema"))
            continue
        events.append(_parse_event(item, f"{scope}/{item.get('event_id', '?')}", issues))

    return Run(
        run_id=_required_str(raw, "run_id", scope=scope, issues=issues),
        agent_version=_required_str(raw, "agent_version", scope=scope, issues=issues),
        task_id=str(raw.get("task_id", "")),
        trace_id=str(raw.get("trace_id", "")),
        events=tuple(events),
        final_state=str(raw.get("final_state", "")),
        snapshot_state=str(raw.get("snapshot_state") or raw.get("final_state") or ""),
        # 业务布尔只认真 bool：bool("false") / bool("0") 都为 True，不能静默伪装成正常状态
        workflow_terminal=_required_bool(raw, "workflow_terminal", scope=scope, issues=issues),
        invoice_created=_required_bool(raw, "invoice_created", scope=scope, issues=issues),
        resource_usage=_required_object(
            raw, "resource_usage", scope=scope, code="BAD_RESOURCE_USAGE", issues=issues
        ),
        raw=raw,
    )

class OfficialJSONLAdapter:
    """官方学生资料包 student-1.0 的 JSON Lines 适配器。

    严格保留原始对象（`Case.raw` / `Run.raw` / `Event.raw`），规范化只读不写。
    不设置任何行数上限；读取数量与文件行数必须一致，否则记为完整性错误。
    """

    name = "official"
    version = ADAPTER_OFFICIAL_VERSION

    def load(self, path: Path) -> Dataset:
        return _load_jsonl(
            path,
            lambda obj, _lineno, issues: self._parse_case(obj, issues),
            adapter_name=self.name,
            adapter_version=self.version,
        )

    def _parse_case(self, obj: dict[str, Any], issues: list[IntegrityIssue]) -> Case:
        case_id = _required_str(obj, "case_id", scope="case:?", issues=issues)
        scope = f"case:{case_id or '?'}"
        if not case_id:
            issues.append(IntegrityIssue(scope, "MISSING_CASE_ID", "缺少 case_id", "error", "schema"))

        runs: list[Run] = []
        raw_runs = obj.get("runs")
        if not isinstance(raw_runs, list) or not raw_runs:
            issues.append(IntegrityIssue(scope, "MISSING_RUNS", "缺少 runs 或为空", "error", "schema"))
            raw_runs = []
        for raw_run in raw_runs:
            if not isinstance(raw_run, dict):
                issues.append(IntegrityIssue(
                    scope, "BAD_RUN_TYPE",
                    f"run 期望 object，实际 {type(raw_run).__name__}", "error", "schema",
                ))
                continue
            run = _parse_run(raw_run, case_id, issues)
            self._check_run(run, scope, issues)
            runs.append(run)

        if len(runs) != 2:
            issues.append(
                IntegrityIssue(scope, "UNPAIRED_RUNS", f"期望 2 条配对运行，实际 {len(runs)}", "warn")
            )

        return Case(
            case_id=case_id,
            scenario=str(obj.get("scenario", "")),
            user_request=str(obj.get("user_request", "")),
            context=_as_mapping(obj.get("context"), scope, "BAD_CONTEXT", issues),
            reference_result=_as_mapping(obj.get("reference_result"), scope, "BAD_REFERENCE", issues),
            runs=tuple(runs),
            dataset_version=str(obj.get("dataset_version", "")),
            data_origin=str(obj.get("data_origin", "")),
            raw=obj,
        )

    def _check_run(self, run: Run, case_scope: str, issues: list[IntegrityIssue]) -> None:
        scope = f"{case_scope}/run:{run.run_id}"
        if not run.events:
            issues.append(IntegrityIssue(scope, "NO_EVENTS", "运行没有任何事件", "error", "schema"))
            return

        seen_ids: set[str] = set()
        for e in run.events:
            missing = [f for f in REQUIRED_EVENT_FIELDS if f not in e.raw]
            if missing:
                issues.append(IntegrityIssue(f"{scope}/{e.event_id}", "MISSING_EVENT_FIELDS", f"缺字段 {missing}", "warn"))
            if not e.event_id:
                issues.append(IntegrityIssue(scope, "MISSING_EVENT_ID", "事件缺少 event_id", "error"))
            if e.event_id in seen_ids:
                issues.append(IntegrityIssue(f"{scope}/{e.event_id}", "DUPLICATE_EVENT_ID", "event_id 在 run 内重复", "error"))
            seen_ids.add(e.event_id)

        seqs = [e.seq for e in run.events]
        if seqs != sorted(seqs):
            # 乱序只降为 warn：规则按 step_id 索引，顺序不伪造业务事实，
            # 且部分 Agent 的并行步骤天然不保证落盘顺序。
            issues.append(IntegrityIssue(scope, "SEQ_NOT_SORTED", "事件未按 seq 升序", "warn"))
        if len(set(seqs)) != len(seqs):
            # 重复 seq 升为 error（C-27）：两个不同事件声称同一位置，
            # "首因事件/因果先后"就不再可判定，属于 schema 级问题而非提示。
            issues.append(IntegrityIssue(
                scope, "SEQ_DUPLICATE",
                "seq 重复：多个事件声称同一位置，首因与因果顺序不可判定", "error", "schema",
            ))

        # 重试次数与工具调用次数必须自洽
        for e in run.events:
            if e.retry_count and len(e.tool_calls) not in (0, e.retry_count + 1):
                issues.append(
                    IntegrityIssue(
                        f"{scope}/{e.event_id}", "RETRY_TOOLCOUNT_MISMATCH",
                        f"retry_count={e.retry_count} 但 tool_calls={len(e.tool_calls)}", "warn",
                    )
                )

