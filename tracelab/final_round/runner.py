"""复赛 Judge 流水线（支持并发；当前默认脚本占位，不调用模型）。

流程：Case → 单 run 白名单输入 + 快照 → JudgeClient → 规范化扩展结果 →
收据链 → 结构校验。真实模型接入时只替换 JudgeClient。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from .checkpoint import append_record, checkpoint_key, load_records
from .identity import base_identity, prompt_digest
from .judge import JudgeClient, ScriptedJudge
from .judge_input import build_judge_input, sha256_json
from .load import load_cases, validate_case
from .receipts import ReceiptChain, receipt_from_call, receipt_meta_fields
from .results import build_case_result, compare_versions, normalize_assessment, validate_case_result


@dataclass
class BatchOutput:
    results: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    issues: list[str]
    execution_origin: str = "SCRIPTED_TEST_NOT_EXECUTED"
    executed: bool = False
    schema_valid_cases: int = 0
    schema_invalid_cases: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        verdicts = Counter(
            str((item.get("output") or {}).get("verdict") or item.get("verdict"))
            for item in self.receipts
        )
        tokens: dict[str, float] = {}
        for item in self.receipts:
            for key, value in (item.get("token_usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tokens[key] = tokens.get(key, 0) + value
        return {
            "schema": "tracelab-final-round-batch/1",
            "executed": self.executed,
            "execution_origin": self.execution_origin,
            "cases": len(self.results),
            "runs": len(self.snapshots),
            "receipts": len(self.receipts),
            "schema_valid_cases": self.schema_valid_cases,
            "schema_invalid_cases": self.schema_invalid_cases,
            "model_valid_runs": sum(count for verdict, count in verdicts.items() if verdict in {"PASS", "FAIL", "REJECT"}),
            "effective_judgments": sum(count for verdict, count in verdicts.items() if verdict in {"PASS", "FAIL", "REJECT"}),
            "valid_judgments": sum(count for verdict, count in verdicts.items() if verdict in {"PASS", "FAIL", "REJECT"}),
            "no_result": verdicts.get("NO_RESULT", 0),
            "success_ratio": (
                (len(self.receipts) - verdicts.get("NO_RESULT", 0)) / len(self.receipts)
                if self.receipts
                else None
            ),
            "verdicts": dict(verdicts),
            "token_usage": tokens,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "results_sha256": sha256_json(self.results),
        }


def _no_result(error: str) -> dict[str, Any]:
    return {
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


def _evaluate_one(
    case: dict[str, Any],
    run: dict[str, Any],
    run_index: int,
    judge: JudgeClient,
    origin: str,
    call_lock: Lock | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or build_judge_input(case, run, run_index=run_index)
    attempt_id = f"attempt-{run_index + 1}"
    call_id = f"{case['case_id']}/{run['run_id']}/{attempt_id}"
    linkage = {
        "task_id": payload["linkage_only"]["task_id"],
        "call_id": call_id,
        "attempt_id": attempt_id,
        "input_snapshot_id": payload["input_snapshot_id"],
        "judge_input_sha256": payload["judge_input_sha256"],
        "model": str(getattr(judge, "model", "unknown")),
        "prompt_version": str(getattr(judge, "prompt_version", "unknown")),
        "execution_origin": origin,
    }
    started = time.perf_counter()
    error = ""
    call_meta: dict[str, Any] = {}
    try:
        evaluator = getattr(judge, "evaluate_with_meta", None)
        if callable(evaluator):
            raw, call_meta = evaluator(payload["judge_input"], payload["linkage_only"])
        else:
            if call_lock is None:
                raw = judge.evaluate(payload["judge_input"], payload["linkage_only"])
                call_meta = dict(getattr(judge, "last_call_meta", {}) or {})
            else:
                # 旧 JudgeClient 只有共享 last_call_meta；用锁保证"调用+取元数据"原子。
                with call_lock:
                    try:
                        raw = judge.evaluate(payload["judge_input"], payload["linkage_only"])
                    finally:
                        call_meta = dict(getattr(judge, "last_call_meta", {}) or {})
    except Exception as exc:  # Judge/网络异常必须落成 NO_RESULT，不冒成 PASS
        if not call_meta:
            call_meta = dict(getattr(judge, "last_call_meta", {}) or {})
        raw = _no_result(f"{type(exc).__name__}: {exc}")
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    try:
        assessment = normalize_assessment(
            run,
            raw,
            judge_id=str(getattr(judge, "judge_id", "unknown")),
            judge_version=str(getattr(judge, "judge_version", "unknown")),
            call_linkage=linkage,
            duration_ms=duration_ms,
        )
    except Exception as exc:  # 模型返回结构非法：落 NO_RESULT，不中断整批
        error = f"{type(exc).__name__}: {exc}"
        raw = _no_result(error)
        assessment = normalize_assessment(
            run,
            raw,
            judge_id=str(getattr(judge, "judge_id", "unknown")),
            judge_version=str(getattr(judge, "judge_version", "unknown")),
            call_linkage=linkage,
            duration_ms=duration_ms,
        )
    verdict = str(raw.get("verdict") or "NO_RESULT")
    return {
        "case": case,
        "run": run,
        "run_index": run_index,
        "payload": payload,
        "assessment": assessment,
        "raw": raw,
        "verdict": verdict,
        "error": error,
        "duration_ms": duration_ms,
        "call_meta": call_meta,
    }


def default_config_version(origin: str) -> str:
    """按执行模式统一配置版本；与是否 --limit 无关。"""
    if origin == "LIVE_MODEL":
        return "final-round-live-v0"
    if origin == "LIVE_MODEL_CUSTOM":
        return "final-round-live-custom-v0"
    return "scripted-v0"


def run_cases(
    cases: list[dict[str, Any]],
    judge: JudgeClient,
    *,
    annotation_version: str = "final-round-2026-11-16",
    rubric_version: str = "final-round-v0",
    config_version: str | None = None,
    execution_origin: str | None = None,
    max_workers: int = 1,
    checkpoint_path: str | Path | None = None,
) -> BatchOutput:
    """逐 Case 逐 run 调用 Judge；max_workers>1 时并发调用、结果顺序保持稳定。"""
    origin = execution_origin or str(getattr(judge, "execution_origin", "UNKNOWN"))
    config_version = config_version or default_config_version(origin)
    issues: list[str] = []
    warnings: list[str] = []
    case_ids = [str(case.get("case_id") or "") for case in cases if isinstance(case, dict)]
    counts = Counter(cid for cid in case_ids if cid)
    duplicates = sorted(cid for cid, count in counts.items() if count > 1)
    if duplicates:
        issues.extend(f"case_id 重复：{case_id}" for case_id in duplicates)
        return BatchOutput(
            results=[],
            receipts=[],
            snapshots=[],
            issues=issues,
            execution_origin=origin,
            executed=origin.startswith("LIVE_MODEL"),
            warnings=warnings,
        )
    valid_cases: list[dict[str, Any]] = []
    for case in cases:
        case_issues = validate_case(case)
        if case_issues:
            issues.extend(f"{case.get('case_id', '?')}: {problem}" for problem in case_issues)
            continue
        valid_cases.append(case)

    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    if checkpoint and origin.startswith("LIVE_MODEL"):
        missing_digest = any(not prompt_digest(judge, case["business_type"]) for case in valid_cases)
        if missing_digest:
            warnings.append("LIVE_MODEL Judge 缺少 prompt_digest，已禁用本批 checkpoint 复用")
            checkpoint = None
    done = load_records(checkpoint, required_fields=("assessment", "raw", "verdict", "payload")) if checkpoint else {}
    prepared: list[dict[str, Any]] = []
    failed_cases: set[str] = set()
    for case in valid_cases:
        case_prepared: list[dict[str, Any]] = []
        for index, run in enumerate(case["runs"]):
            try:
                payload = build_judge_input(case, run, run_index=index)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{case['case_id']}/{run['run_id']}: 输入契约失败: {exc}")
                failed_cases.add(case["case_id"])
                break
            identity = base_identity(
                judge,
                origin,
                business_type=case["business_type"],
                judge_input_sha256=payload["judge_input_sha256"],
                extra={
                    "case_id": case["case_id"],
                    "run_id": run["run_id"],
                    "annotation_version": annotation_version,
                    "rubric_version": rubric_version,
                    "config_version": config_version,
                },
            )
            case_prepared.append(
                {
                    "case": case,
                    "run": run,
                    "run_index": index,
                    "payload": payload,
                    "identity": identity,
                    "key": checkpoint_key(identity),
                }
            )
        if case["case_id"] not in failed_cases:
            prepared.extend(case_prepared)
    valid_cases = [case for case in valid_cases if case["case_id"] not in failed_cases]
    outcomes: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for item in prepared:
        cached = done.get(item["key"])
        if cached and cached.get("identity") == item["identity"]:
            saved = dict(cached["outcome"])
            outcomes.append(
                {
                    "case": item["case"],
                    "run": item["run"],
                    "run_index": item["run_index"],
                    "payload": saved.get("payload") or item["payload"],
                    "assessment": saved["assessment"],
                    "raw": saved["raw"],
                    "verdict": saved["verdict"],
                    "error": saved.get("error", ""),
                    "duration_ms": saved.get("duration_ms", 0.0),
                    "call_meta": saved.get("call_meta") or {},
                }
            )
        else:
            jobs.append(item)

    call_lock = None if callable(getattr(judge, "evaluate_with_meta", None)) else Lock()

    def work(item: dict[str, Any]) -> dict[str, Any]:
        outcome = _evaluate_one(
            item["case"],
            item["run"],
            item["run_index"],
            judge,
            origin,
            call_lock,
            item["payload"],
        )
        outcome["identity"] = item["identity"]
        return outcome

    if max_workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for outcome in pool.map(work, jobs):
                outcomes.append(outcome)
                if checkpoint and not outcome.get("error"):
                    key = checkpoint_key(outcome["identity"])
                    append_record(checkpoint, key, outcome["identity"], outcome)
    else:
        for item in jobs:
            outcome = work(item)
            outcomes.append(outcome)
            if checkpoint and not outcome.get("error"):
                key = checkpoint_key(outcome["identity"])
                append_record(checkpoint, key, outcome["identity"], outcome)

    # 固定按 Case/run 顺序重建结果与收据链，避免并发影响产物顺序。
    by_case: dict[str, list[dict[str, Any]]] = {case["case_id"]: [] for case in valid_cases}
    for outcome in outcomes:
        by_case[outcome["case"]["case_id"]].append(outcome)
    for entries in by_case.values():
        entries.sort(key=lambda item: item["run_index"])

    schema_valid_cases = 0
    schema_invalid_cases = 0
    chain = ReceiptChain()
    batch_id = f"batch-{sha256_json([case['case_id'] for case in valid_cases])[:16]}"
    snapshots: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for case in valid_cases:
        assessments: list[dict[str, Any]] = []
        for outcome in by_case[case["case_id"]]:
            payload = outcome["payload"]
            snapshots.append(payload)
            assessments.append(outcome["assessment"])
            raw = outcome["raw"]
            verdict = outcome["verdict"]
            status = {
                "PASS": "ok",
                "FAIL": "ok",
                "REJECT": "rejected",
                "NO_RESULT": "no_result",
            }.get(verdict, "error")
            chain.append(
                receipt_from_call(
                    sequence=len(chain.receipts),
                    call_id=outcome["assessment"]["call_linkage"]["call_id"],
                    case_id=case["case_id"],
                    run_id=outcome["run"]["run_id"],
                    business_type=case["business_type"],
                    judge_id=str(getattr(judge, "judge_id", "unknown")),
                    judge_version=str(getattr(judge, "judge_version", "unknown")),
                    model=str(getattr(judge, "model", "unknown")),
                    prompt_version=str(getattr(judge, "prompt_version", "unknown")),
                    input_snapshot_id=payload["input_snapshot_id"],
                    judge_input_sha256=payload["judge_input_sha256"],
                    output=raw,
                    verdict=verdict,
                    attempt_id=outcome["assessment"]["call_linkage"]["attempt_id"],
                    execution_origin=origin,
                    duration_ms=outcome["duration_ms"],
                    error=outcome["error"] or str(raw.get("reason") if status != "ok" else ""),
                    batch_id=batch_id,
                    **receipt_meta_fields(outcome.get("call_meta")),
                )
            )
        comparison = compare_versions(
            assessments,
            baseline_business_type=case["business_type"],
            candidate_business_type=case["business_type"],
            baseline_rubric_version=rubric_version,
            candidate_rubric_version=rubric_version,
        )
        result = build_case_result(
            case,
            assessments,
            annotation_version=annotation_version,
            rubric_version=rubric_version,
            config_version=config_version,
            version_comparison=comparison,
        )
        result_issues = validate_case_result(result, case)
        if result_issues:
            schema_invalid_cases += 1
        else:
            schema_valid_cases += 1
        issues.extend(f"{case['case_id']}: {problem}" for problem in result_issues)
        results.append(result)

    issues.extend(f"收据链: {problem}" for problem in chain.verify())
    return BatchOutput(
        results=results,
        receipts=chain.to_dicts(),
        snapshots=snapshots,
        issues=issues,
        execution_origin=origin,
        executed=origin.startswith("LIVE_MODEL"),
        schema_valid_cases=schema_valid_cases,
        schema_invalid_cases=schema_invalid_cases,
        warnings=warnings,
    )


def write_batch(out_dir: str | Path, batch: BatchOutput) -> None:
    """写出结果、收据、输入快照与 manifest。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in batch.results) + "\n",
        encoding="utf-8",
    )
    (out / "receipts.json").write_text(
        json.dumps(batch.receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    snap_dir = out / "judge_inputs"
    snap_dir.mkdir(exist_ok=True)
    for snapshot in batch.snapshots:
        name = f"{snapshot['linkage_only']['case_id']}__{snapshot['linkage_only']['run_id']}.json"
        (snap_dir / name).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (out / "manifest.json").write_text(
        json.dumps(batch.to_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def run_file(
    cases_path: str | Path,
    out_dir: str | Path,
    *,
    judge: JudgeClient | None = None,
    config_version: str | None = None,
    max_workers: int = 1,
    checkpoint_path: str | Path | None = None,
) -> BatchOutput:
    cases = load_cases(cases_path)
    actual = judge or ScriptedJudge()
    output = run_cases(
        cases,
        actual,
        config_version=config_version,
        max_workers=max_workers,
        checkpoint_path=checkpoint_path,
    )
    write_batch(out_dir, output)
    return output
