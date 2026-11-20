"""自造轨迹样本生成器：供**实验命令**与**测试**共用同一份事实来源。

为什么放在产品侧：变形实验（`python -m tracelab experiments`）需要一份**不依赖官方资料包**的
自造样本，才能让评委在任意环境复现实验数字。测试侧改为从本模块再导出，避免两份样本漂移。

约定：所有金额/编号/事件都是虚构的；不读取、不复制官方资料包的任何记录。
"""

from __future__ import annotations

from typing import Any

from tracelab.contracts import Case, Event, Run, ToolCall


def make_event(
    event_id: str,
    seq: int,
    step_id: str,
    *,
    status: str = "SUCCEEDED",
    result_code: str = "E100",
    error_code: str | None = None,
    retry_count: int = 0,
    observed_input: dict[str, Any] | None = None,
    observed_output: dict[str, Any] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> Event:
    tools = tuple(
        ToolCall(
            name=t["name"],
            arguments=t.get("arguments") or {},
            result=t.get("result") or {},
            attempt=t.get("attempt"),
            raw=t,
        )
        for t in (tool_calls or [])
    )
    raw = {
        "event_id": event_id, "seq": seq, "step_id": step_id, "module": step_id,
        "action": step_id, "step_status": status, "result_code": result_code,
        "error_code": error_code, "error_message": None, "retry_count": retry_count,
        "observed_input": observed_input or {}, "observed_output": observed_output or {},
        "tool_calls": tool_calls or [], "started_at": "", "finished_at": "", "duration_ms": 1,
    }
    return Event(
        event_id=event_id, seq=seq, step_id=step_id, module=step_id, action=step_id,
        step_status=status, result_code=result_code, error_code=error_code, error_message=None,
        retry_count=retry_count, observed_input=observed_input or {}, observed_output=observed_output or {},
        tool_calls=tools, started_at="", finished_at="", duration_ms=1, raw=raw,
    )


def make_run(
    run_id: str,
    events: list[Event],
    *,
    final_state: str = "WAITING_USER",
    invoice_created: bool = False,
    agent_version: str = "baseline-v1.0",
) -> Run:
    terminal = final_state in ("SUCCEEDED", "FAILED", "CANCELED")
    raw = {
        "run_id": run_id, "agent_version": agent_version, "task_id": f"T-{run_id}",
        "trace_id": f"TR-{run_id}", "final_state": final_state, "snapshot_state": final_state,
        "workflow_terminal": terminal,
        "invoice_created": invoice_created, "resource_usage": {},
    }
    return Run(
        run_id=run_id, agent_version=agent_version, task_id=f"T-{run_id}", trace_id=f"TR-{run_id}",
        events=tuple(events), final_state=final_state, snapshot_state=final_state,
        workflow_terminal=terminal, invoice_created=invoice_created,
        resource_usage={}, raw=raw,
    )


def make_case(
    case_id: str = "CASE-1",
    *,
    context: dict[str, Any] | None = None,
    reference_result: dict[str, Any] | None = None,
    runs: list[Run] | None = None,
) -> Case:
    raw = {
        "case_id": case_id, "scenario": "测试", "user_request": "测试请求",
        "context": context or {}, "reference_result": reference_result or {},
        "runs": [r.raw for r in (runs or [])], "dataset_version": "test", "data_origin": "synthetic",
    }
    return Case(
        case_id=case_id, scenario="测试", user_request="测试请求",
        context=context or {}, reference_result=reference_result or {},
        runs=tuple(runs or []), dataset_version="test", data_origin="synthetic", raw=raw,
    )


def basic_flow(
    prefix: str = "C1",
    *,
    amount_basis: str = "tax_exclusive",
    amount_value: float = 1000.0,
    computed: dict[str, float] | None = None,
    buyer_id: str | None = "CUST-001",
    item_id: str = "ITEM-006",
    task_type: str = "BLUE",
    intent: str = "ISSUE",
    risk_flags: list[str] | None = None,
    validation_passed: bool = True,
    create_allowed: bool = True,
    invoice_confirmed: bool = True,
    preview_amounts: dict[str, float] | None = None,
    submit_amounts: dict[str, float] | None = None,
    submit_status: str = "accepted",
    submit_status_step: str = "SUCCEEDED",
    submit_error: str | None = None,
    retry_count: int = 0,
    submit_attempts: int = 1,
    callback_status: str | None = "success",
    final_state: str = "SUCCEEDED",
    invoice_created: bool = True,
    extra_steps: list[str] | None = None,
) -> list[Event]:
    computed = computed or {"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0}
    preview_amounts = preview_amounts or dict(computed)
    submit_amounts = submit_amounts or dict(computed)
    events = [
        make_event(f"{prefix}-01", 1, "RECEIVE_REQUEST"),
        make_event(f"{prefix}-02", 2, "CREATE_TASK"),
        make_event(f"{prefix}-03", 3, "RESOLVE_ENTERPRISE",
                   observed_output={"seller_id": "SELLER-DEMO-01"}),
        make_event(f"{prefix}-04", 4, "PARSE_INTENT",
                   observed_output={"intent": intent, "invoice_task_type": task_type, "confidence": 0.9}),
        make_event(f"{prefix}-05", 5, "COLLECT_INVOICE_DATA", observed_output={
            "buyer_id": buyer_id, "item_id": item_id, "amount_value": amount_value,
            "amount_basis": amount_basis, "computed_amounts": computed,
            "line_count": 1, "line_items": [{"item_id": item_id, "exclusive_amount": computed["exclusive_amount"]}],
            "original_invoice_id": None, "missing_fields": [],
        }),
        make_event(f"{prefix}-06", 6, "VALIDATE_DATA", observed_output={
            "validation_passed": validation_passed, "risk_flags": risk_flags or [],
            "confirmation_required": True, "create_allowed": create_allowed,
        }),
        make_event(f"{prefix}-07", 7, "CONFIRM_INVOICE", status="SUCCEEDED",
                   observed_output={"invoice_confirmed": invoice_confirmed,
                                    "preview": {"amounts": preview_amounts}}),
        make_event(
            f"{prefix}-08", 8, "SUBMIT_INVOICE",
            status=submit_status_step, error_code=submit_error, retry_count=retry_count,
            observed_input={"amounts": submit_amounts, "user_confirmed": invoice_confirmed},
            observed_output={"submit_status": submit_status, "request_id": "REQ-1" if submit_status == "accepted" else None},
            tool_calls=[{
                "name": "invoice.submit", "attempt": i + 1,
                "arguments": {"amounts": submit_amounts},
                "result": {"submit_status": submit_status},
            } for i in range(submit_attempts)],
        ),
    ]
    if callback_status is not None:
        events.append(make_event(
            f"{prefix}-09", 9, "WAIT_INVOICE_CALLBACK",
            status="SUCCEEDED" if callback_status == "success" else "FAILED",
            error_code=None if callback_status == "success" else "SIM_REJECTED",
            observed_output={"callback_status": callback_status,
                             "invoice_id": "INV-1" if callback_status == "success" else None},
            tool_calls=[{"name": "invoice.callback", "arguments": {"request_id": "REQ-1"},
                         "result": {"status": callback_status}}],
        ))
    terminal = {
        "SUCCEEDED": ("COMPLETE_TASK", "E200"),
        "FAILED": ("FAIL_TASK", "E400"),
        "CANCELED": ("CANCEL_TASK", "E800"),
    }.get(final_state, ("FAIL_TASK", "E400"))
    events.append(make_event(f"{prefix}-10", 10, terminal[0], result_code=terminal[1],
                             observed_output={"invoice_created": invoice_created}))
    return events


def reference(**overrides: Any) -> dict[str, Any]:
    base = {
        "invoice_task_type": "BLUE",
        "buyer_id": "CUST-001",
        "item_id": "ITEM-006",
        "amount_value": 1000.0,
        "amount_basis": "tax_exclusive",
        "expected_amounts": {"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0},
        "acceptable_snapshot_state": "SUCCEEDED",
        "invoice_should_exist": True,
    }
    base.update(overrides)
    return base


def context(**overrides: Any) -> dict[str, Any]:
    base = {
        "seller_id": "SELLER-DEMO-01",
        "buyer_candidates": [{"buyer_id": "CUST-001", "profile_complete": True}],
        "item_candidates": [{"item_id": "ITEM-006", "display_name": "软件服务费", "configured_rate": 0.06}],
        "user_confirmation_recorded": True,
        "sandbox_rule_set": "INVOICE-RULES-1.0",
    }
    base.update(overrides)
    return base

