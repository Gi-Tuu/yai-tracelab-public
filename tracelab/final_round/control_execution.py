"""专项 Control 的单输入执行：Judge 调用、元数据捕获与评估规范化。"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

from .control_inputs import judge_meta
from .judge import JudgeClient
from .results import normalize_assessment, validate_assessment_evidence


def pseudo_run(judge_input: dict[str, Any], linkage: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": linkage.get("run_id") or "CTRL-RUN",
        "agent_version": linkage.get("agent_version") or "unknown",
        "events": judge_input.get("observable_trace") or [],
    }


def input_error_outcome(prepared: dict[str, Any]) -> dict[str, Any]:
    error = str(prepared.get("input_error") or "输入契约校验失败")
    raw = {
        "verdict": "NO_RESULT",
        "agent_task_success": None,
        "business_result_correct": None,
        "reason": f"Judge 输入校验失败，未调用模型：{error}",
        "dimensions": [
            {
                "name": "judge_input_contract",
                "conclusion": "NOT_APPLICABLE",
                "reason": error,
                "evidence_event_ids": [],
            }
        ],
        "evidence_event_ids": [],
        "first_fault_event_id": None,
        "root_causes": [],
        "uncertainty": {"level": "unknown", "reason": error},
    }
    return {
        "side": prepared["side"],
        "index": prepared["index"],
        "judge_input": {},
        "linkage": {},
        "judge_input_sha256": "",
        "raw": raw,
        "assessment": {
            "run_id": "CTRL-RUN",
            "agent_version": "unknown",
            "judge_id": prepared["identity"].get("judge_id", "unknown"),
            "judge_version": prepared["identity"].get("judge_version", "unknown"),
            "verdict": "NO_RESULT",
            "agent_task_success": None,
            "business_result_correct": None,
            "reason": raw["reason"],
            "dimensions": raw["dimensions"],
            "evidence_event_ids": [],
            "first_fault_event_id": None,
            "root_causes": [],
            "uncertainty": raw["uncertainty"],
            "duration_ms": 0.0,
            "call_linkage": {
                "task_id": f"CTRL-{prepared['control']['control_id']}",
                "call_id": f"{prepared['control']['control_id']}/{prepared['side']}/{prepared['index']}",
                "attempt_id": "attempt-0",
                "input_snapshot_id": "js-invalid",
                "judge_input_sha256": "",
                "model": prepared["identity"].get("model", "unknown"),
                "prompt_version": prepared["identity"].get("prompt_version", "unknown"),
                "execution_origin": prepared["identity"].get("execution_origin", "UNKNOWN"),
            },
        },
        "error": f"JudgeInputContractError: {error}",
        "duration_ms": 0.0,
        "identity": prepared["identity"],
        "call_meta": {},
        "input_error": True,
        "output_error": False,
    }


def _no_result_raw(reason: str) -> dict[str, Any]:
    return {
        "verdict": "NO_RESULT",
        "agent_task_success": None,
        "business_result_correct": None,
        "reason": reason,
        "dimensions": [
            {
                "name": "judge_evidence_contract",
                "conclusion": "NOT_APPLICABLE",
                "reason": reason,
                "evidence_event_ids": [],
            }
        ],
        "evidence_event_ids": [],
        "first_fault_event_id": None,
        "root_causes": [],
        "uncertainty": {"level": "unknown", "reason": reason},
    }


def _no_result_assessment(
    run: dict[str, Any],
    meta: dict[str, str],
    call_linkage: dict[str, Any],
    duration_ms: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "agent_version": run["agent_version"],
        "judge_id": meta["judge_id"],
        "judge_version": meta["judge_version"],
        "verdict": "NO_RESULT",
        "agent_task_success": None,
        "business_result_correct": None,
        "reason": reason,
        "dimensions": _no_result_raw(reason)["dimensions"],
        "evidence_event_ids": [],
        "first_fault_event_id": None,
        "root_causes": [],
        "uncertainty": {"level": "unknown", "reason": reason},
        "duration_ms": duration_ms,
        "call_linkage": call_linkage,
    }


def evaluate_prepared(
    prepared: dict[str, Any],
    judge: JudgeClient,
    origin: str,
    call_lock: Lock | None = None,
) -> dict[str, Any]:
    if prepared.get("input_error"):
        return input_error_outcome(prepared)
    payload = prepared["payload"]
    judge_input = payload["judge_input"]
    linkage = payload["linkage_only"]
    digest = payload["judge_input_sha256"]
    call_id = f"{prepared['control']['control_id']}/{prepared['side']}/{prepared['index']}"
    meta = judge_meta(judge, origin)
    call_linkage = {
        "task_id": str(linkage.get("trace_id") or linkage.get("task_id") or call_id),
        "call_id": call_id,
        "attempt_id": "attempt-1",
        "input_snapshot_id": f"js-{digest[:16]}",
        "judge_input_sha256": digest,
        "model": meta["model"],
        "prompt_version": meta["prompt_version"],
        "execution_origin": origin,
    }
    run = pseudo_run(judge_input, linkage)
    error = ""
    started = time.perf_counter()
    call_meta: dict[str, Any] = {}
    try:
        evaluator = getattr(judge, "evaluate_with_meta", None)
        if callable(evaluator):
            raw, call_meta = evaluator(judge_input, linkage)
        elif call_lock is None:
            raw = judge.evaluate(judge_input, linkage)
            call_meta = dict(getattr(judge, "last_call_meta", {}) or {})
        else:
            with call_lock:
                try:
                    raw = judge.evaluate(judge_input, linkage)
                finally:
                    call_meta = dict(getattr(judge, "last_call_meta", {}) or {})
    except Exception as exc:  # noqa: BLE001
        if not call_meta:
            call_meta = dict(getattr(judge, "last_call_meta", {}) or {})
        error = f"{type(exc).__name__}: {exc}"
        raw = {
            "verdict": "NO_RESULT",
            "agent_task_success": None,
            "business_result_correct": None,
            "reason": f"Judge 调用未取得有效结果：{error}",
            "dimensions": [
                {
                    "name": "judge_execution",
                    "conclusion": "NOT_APPLICABLE",
                    "reason": error,
                    "evidence_event_ids": [],
                }
            ],
            "evidence_event_ids": [],
            "first_fault_event_id": None,
            "root_causes": [],
            "uncertainty": {"level": "unknown", "reason": error},
        }
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    try:
        assessment = normalize_assessment(
            run,
            raw,
            judge_id=meta["judge_id"],
            judge_version=meta["judge_version"],
            call_linkage=call_linkage,
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        assessment = {
            "run_id": run["run_id"],
            "agent_version": run["agent_version"],
            "judge_id": meta["judge_id"],
            "judge_version": meta["judge_version"],
            "verdict": "NO_RESULT",
            "agent_task_success": None,
            "business_result_correct": None,
            "reason": f"Judge 输出结构非法：{error}",
            "dimensions": [
                {
                    "name": "judge_execution",
                    "conclusion": "NOT_APPLICABLE",
                    "reason": error,
                    "evidence_event_ids": [],
                }
            ],
            "evidence_event_ids": [],
            "first_fault_event_id": None,
            "root_causes": [],
            "uncertainty": {"level": "unknown", "reason": error},
            "duration_ms": duration_ms,
            "call_linkage": call_linkage,
        }
        raw = {"verdict": "NO_RESULT", "reason": assessment["reason"]}
        output_error = True
    else:
        output_error = False

    if not output_error:
        evidence_issues = validate_assessment_evidence(assessment, run)
        if evidence_issues:
            output_error = True
            error = "EvidenceContractError: " + "; ".join(evidence_issues)
            raw = _no_result_raw(f"Judge 输出证据引用非法：{error}")
            assessment = _no_result_assessment(run, meta, call_linkage, duration_ms, raw["reason"])
    return {
        "side": prepared["side"],
        "index": prepared["index"],
        "judge_input": judge_input,
        "linkage": linkage,
        "judge_input_sha256": digest,
        "raw": raw,
        "assessment": assessment,
        "error": error,
        "duration_ms": duration_ms,
        "identity": prepared["identity"],
        "call_meta": call_meta,
        "input_error": False,
        "output_error": output_error,
    }
