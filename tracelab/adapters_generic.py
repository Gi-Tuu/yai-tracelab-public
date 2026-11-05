"""异构 Agent 适配器：步骤别名映射 + 宽松 schema，展示低成本接入。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters_common import (
    STEP_ALIASES,
    _as_mapping,
    _load_jsonl,
    _looks_like_business_step,
    _safe_int,
)
from .contracts import STEP_IDS, Case, Dataset, Event, IntegrityIssue, Run, ToolCall


class GenericJSONLAdapter:
    """异构格式适配器：证明"接入新 Agent 不需要改核心"。

    它故意用一套**和官方完全不同的字段名与嵌套**：

    ```
    {"case": {"id", "scenario", "request", "context", "goal"},
     "trajectories": [
        {"label", "agent", "final_state", "invoice_created",
         "steps": [{"id", "step", "state", "in", "out", "tools", "error", "retries"}]}
     ]}
    ```

    全部差异（字段名、缺 `seq`、缺 `module/action`、`tools` 用 `tool/args/resp`）
    都收敛在本类内部；规则引擎、归因层、报告层一行都不用改。
    """

    name = "generic"
    version = "generic-heterogeneous/1"

    def load(self, path: Path) -> Dataset:
        # 与 official 共用同一条读取/收尾流水线：坏字节隔离、重复主键、空文件、
        # 跳过计数四处口径必须一致（C-23/C-25）。
        return _load_jsonl(
            path,
            self._parse_case,
            adapter_name=self.name,
            adapter_version=self.version,
        )

    def _parse_case(
        self, obj: dict[str, Any], lineno: int, issues: list[IntegrityIssue]
    ) -> Case | None:
        """解析单条异构记录；结构上不可能成为 Case 的记录返回 None（由 load 计入 skipped）。"""
        record_scope = f"line:{lineno}"
        head_raw = obj.get("case")
        if head_raw is not None and not isinstance(head_raw, dict):
            issues.append(IntegrityIssue(
                record_scope, "BAD_CASE_HEAD",
                f"case 期望 object，实际 {type(head_raw).__name__}", "error", "schema",
            ))
            return None
        head: dict[str, Any] = head_raw if isinstance(head_raw, dict) else {}

        traj_raw = obj.get("trajectories")
        if traj_raw is not None and not isinstance(traj_raw, list):
            issues.append(IntegrityIssue(
                record_scope, "BAD_TRAJECTORIES",
                f"trajectories 期望 array，实际 {type(traj_raw).__name__}", "error", "schema",
            ))
            return None
        trajectories: list[Any] = traj_raw if isinstance(traj_raw, list) else []

        case_id = str(head.get("id", ""))
        scope = f"case:{case_id}"
        if not case_id:
            issues.append(IntegrityIssue(scope, "MISSING_CASE_ID", "缺少 case.id", "error"))

        runs: list[Run] = []
        for idx, traj in enumerate(trajectories):
            if not isinstance(traj, dict):
                issues.append(IntegrityIssue(
                    scope, "BAD_TRAJECTORY_TYPE",
                    f"trajectory 期望 object，实际 {type(traj).__name__}", "error", "schema",
                ))
                continue
            run = self._parse_trajectory(traj, case_id, idx, issues)
            if len(run.events) == 0:
                issues.append(IntegrityIssue(f"{scope}/run:{run.run_id}", "NO_EVENTS", "没有事件", "error"))
            runs.append(run)
        if len(runs) != 2:
            issues.append(IntegrityIssue(scope, "UNPAIRED_RUNS", f"期望 2 条轨迹，实际 {len(runs)}", "warn"))

        scenario = str(head.get("scenario", ""))
        user_request = str(head.get("request", ""))
        ctx_raw = head.get("context")
        ctx = _as_mapping(ctx_raw, scope, "BAD_CONTEXT", issues) if ctx_raw is not None else {}
        goal_raw = head.get("goal")
        goal = _as_mapping(goal_raw, scope, "BAD_REFERENCE", issues) if goal_raw is not None else {}
        dataset_version = str(obj.get("format_version", self.version))
        raw = {
            "case_id": case_id,
            "scenario": scenario,
            "user_request": user_request,
            "context": dict(ctx),
            "reference_result": dict(goal),
            "runs": [r.raw for r in runs],
            "dataset_version": dataset_version,
            "data_origin": "heterogeneous_demo",
        }
        return Case(
            case_id=case_id,
            scenario=scenario,
            user_request=user_request,
            context=ctx,
            reference_result=goal,
            runs=tuple(runs),
            dataset_version=dataset_version,
            data_origin="heterogeneous_demo",
            raw=raw,
        )

    def _parse_trajectory(
        self, traj: dict[str, Any], case_id: str, idx: int, issues: list[IntegrityIssue]
    ) -> Run:
        """只做编排：run 身份 → steps 容器 → run 元数据（审计 B1）。"""
        run_id = str(traj.get("run_id") or f"{case_id}-{traj.get('label') or idx}")
        scope = f"case:{case_id}/run:{run_id}"
        events = _parse_generic_steps(traj.get("steps"), scope=scope, run_id=run_id, issues=issues)
        meta = _parse_generic_run_meta(traj, scope=scope, run_id=run_id, issues=issues)
        return Run(run_id=run_id, events=events, **meta)

def _parse_generic_tools(step: dict[str, Any], *, step_scope: str, issues: list[IntegrityIssue]) -> list[ToolCall]:
    """解析单个 tier 的 tools 容器与 args/resp（审计 B1）。"""
    tools_raw = step.get("tools")
    if tools_raw is None:
        tool_items: list[Any] = []
    elif isinstance(tools_raw, list):
        tool_items = tools_raw
    else:
        issues.append(IntegrityIssue(
            step_scope, "BAD_TOOLS", f"tools 期望 array，实际 {type(tools_raw).__name__}", "error", "schema"
        ))
        tool_items = []
    tools: list[ToolCall] = []
    for tool in tool_items:
        if not isinstance(tool, dict):
            issues.append(IntegrityIssue(
                step_scope, "BAD_TOOL_TYPE",
                f"tool 期望 object，实际 {type(tool).__name__}", "error", "schema",
            ))
            continue
        args_raw = tool.get("args")
        resp_raw = tool.get("resp")
        args = _as_mapping(args_raw, step_scope, "BAD_TOOL_ARGUMENTS", issues) if args_raw is not None else {}
        resp = _as_mapping(resp_raw, step_scope, "BAD_TOOL_RESULT", issues) if resp_raw is not None else {}
        tools.append(ToolCall(
            name=str(tool.get("tool", "unknown")),
            arguments=args,
            result=resp,
            attempt=tool.get("attempt"),
            raw=tool,
        ))
    return tools

def _parse_generic_step(
    step: dict[str, Any], *, scope: str, seq: int, run_id: str, issues: list[IntegrityIssue]
) -> Event:
    """解析单个 step：步骤名映射/未知步骤记录、in/out/retries、Event 组装（审计 B1）。"""
    raw_step_name = str(step.get("step", ""))
    # 审计 R2-01：异构 Agent 的步骤名常与数据字典不同名；先按别名表映射，
    # 映射不到时保留原名，由后续规则/归因显式披露。
    step_id = STEP_ALIASES.get(raw_step_name, raw_step_name)
    step_scope = f"{scope}/step:{step_id or seq}"
    if step_id and step_id not in STEP_IDS:
        business_like = _looks_like_business_step(step_id)
        issues.append(IntegrityIssue(
            step_scope,
            "UNKNOWN_STEP_ID" if business_like else "UNKNOWN_NON_BUSINESS_STEP",
            f"步骤名 {raw_step_name!r} 未映射到数据字典步骤：{step_id!r}；"
            + ("该阶段事实不会被任何规则使用，请补充 STEP_ALIASES 映射"
               if business_like else "按非业务步骤忽略"),
            "error" if business_like else "warn", "schema",
        ))

    tools = _parse_generic_tools(step, step_scope=step_scope, issues=issues)
    error = step.get("error")
    in_raw = step.get("in")
    out_raw = step.get("out")
    observed_input = (
        _as_mapping(in_raw, step_scope, "BAD_OBSERVED_INPUT", issues) if in_raw is not None else {}
    )
    observed_output = (
        _as_mapping(out_raw, step_scope, "BAD_OBSERVED_OUTPUT", issues) if out_raw is not None else {}
    )
    retries_raw = step.get("retries")
    retry_count = 0 if retries_raw is None else _safe_int(
        retries_raw, 0, field="retries", scope=step_scope, issues=issues
    )
    event_id = str(step.get("id") or f"{run_id}-{seq:02d}")
    step_status = str(step.get("state", "SUCCEEDED"))
    result_code = str(step.get("code", "E100"))
    error_code = str(error) if error is not None else None
    raw_event = {
        "event_id": event_id,
        "seq": seq,
        "step_id": step_id,
        "module": step_id,
        "action": step_id,
        "step_status": step_status,
        "result_code": result_code,
        "error_code": error_code,
        "error_message": None,
        "retry_count": retry_count,
        "observed_input": dict(observed_input),
        "observed_output": dict(observed_output),
        "tool_calls": [tool.raw for tool in tools],
        "started_at": "", "finished_at": "", "duration_ms": 0,
    }
    return Event(
        event_id=event_id, seq=seq, step_id=step_id, module=step_id, action=step_id,
        step_status=step_status, result_code=result_code,
        error_code=error_code, error_message=None,
        retry_count=retry_count, observed_input=observed_input,
        observed_output=observed_output, tool_calls=tuple(tools),
        started_at="", finished_at="", duration_ms=0, raw=raw_event,
    )

def _parse_generic_steps(
    steps_raw: Any, *, scope: str, run_id: str, issues: list[IntegrityIssue]
) -> tuple[Event, ...]:
    """steps 容器类型检查 + 逐个 step 调度（审计 B1）。"""
    if steps_raw is None:
        steps: list[Any] = []
    elif isinstance(steps_raw, list):
        steps = steps_raw
    else:
        issues.append(IntegrityIssue(
            scope, "BAD_STEPS", f"steps 期望 array，实际 {type(steps_raw).__name__}", "error", "schema"
        ))
        steps = []
    events: list[Event] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            issues.append(IntegrityIssue(
                scope, "BAD_STEP_TYPE",
                f"step 期望 object，实际 {type(step).__name__}", "error", "schema",
            ))
            continue
        events.append(_parse_generic_step(step, scope=scope, seq=index, run_id=run_id, issues=issues))
    return tuple(events)

def _parse_generic_run_meta(
    traj: dict[str, Any], *, scope: str, run_id: str, issues: list[IntegrityIssue]
) -> dict[str, Any]:
    """run 级元数据：final_state / agent / task / trace / invoice_created（审计 B1）。"""
    final_state = str(traj.get("final_state", ""))
    workflow_terminal = final_state in ("SUCCEEDED", "FAILED", "CANCELED", "TRANSFERRED_TO_HUMAN")
    invoice_raw = traj.get("invoice_created")
    if invoice_raw is None:
        invoice_created = False
    elif isinstance(invoice_raw, bool):
        invoice_created = invoice_raw
    else:
        issues.append(IntegrityIssue(
            scope, "BAD_FIELD_TYPE",
            f"invoice_created 期望 boolean，实际 {type(invoice_raw).__name__}", "error", "schema",
        ))
        invoice_created = False
    agent_version = str(traj.get("agent", ""))
    task_id = str(traj.get("task", f"T-{run_id}"))
    trace_id = str(traj.get("trace", f"TR-{run_id}"))
    raw_run = {
        "run_id": run_id,
        "agent_version": agent_version,
        "task_id": task_id,
        "trace_id": trace_id,
        "final_state": final_state,
        "snapshot_state": final_state,
        "workflow_terminal": workflow_terminal,
        "invoice_created": invoice_created,
        "resource_usage": {},
    }
    return {
        "agent_version": agent_version,
        "task_id": task_id,
        "trace_id": trace_id,
        "final_state": final_state,
        "snapshot_state": final_state,
        "workflow_terminal": workflow_terminal,
        "invoice_created": invoice_created,
        "resource_usage": {},
        "raw": raw_run,
    }

