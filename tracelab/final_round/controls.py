"""专项验收集（抗干扰/等义/事实反事实/有效路径/错配）的 Judge 运行器。

本模块只负责公共加载、批次编排与输出；输入身份/断点在 `control_inputs.py`，
单输入执行在 `control_execution.py`，避免单文件承担所有职责。
"""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from .checkpoint import append_record, checkpoint_key, load_records
from .control_execution import evaluate_prepared
from .control_inputs import prepare_input
from .identity import prompt_digest
from .judge import JudgeClient, ScriptedJudge
from .judge_input import sha256_json
from .receipts import ReceiptChain, receipt_from_call, receipt_meta_fields


class ControlFormatError(ValueError):
    pass


def load_controls(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ControlFormatError(f"{path}:{lineno} JSON 解析失败: {exc}") from exc
        if not isinstance(row, dict):
            raise ControlFormatError(f"{path}:{lineno} 不是 object")
        for key in ("control_id", "control_type", "expected_boundary", "baseline_inputs"):
            if key not in row:
                raise ControlFormatError(f"{path}:{lineno} 缺少 {key}")
        # BUSINESS_MISMATCH 对照组可能只有 baseline（故意错配 Judge），允许 variant 为空/缺省。
        row["variant_inputs"] = row.get("variant_inputs") or []
        for side in ("baseline_inputs", "variant_inputs"):
            if not isinstance(row[side], list):
                raise ControlFormatError(f"{path}:{lineno} {side} 必须是数组")
            for index, item in enumerate(row[side]):
                if not isinstance(item, dict) or not isinstance(item.get("judge_input"), dict):
                    raise ControlFormatError(f"{path}:{lineno} {side}[{index}] 缺少 judge_input")
        if not row["baseline_inputs"] and not row["variant_inputs"]:
            raise ControlFormatError(f"{path}:{lineno} baseline/variant 不能同时为空")
        rows.append(row)
    seen: set[str] = set()
    for row in rows:
        control_id = str(row.get("control_id"))
        if control_id in seen:
            raise ControlFormatError(f"{path} control_id 重复：{control_id}")
        seen.add(control_id)
    return rows


@dataclass
class ControlBatch:
    controls: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    issues: list[str]
    execution_origin: str = "SCRIPTED_TEST_NOT_EXECUTED"
    executed: bool = False
    retryable_errors: int = 0
    warnings: list[str] = field(default_factory=list)
    input_errors: int = 0
    output_errors: int = 0

    def to_manifest(self) -> dict[str, Any]:
        verdicts: dict[str, int] = {}
        tokens: dict[str, float] = {}
        for item in self.receipts:
            verdict = str(item.get("verdict") or "")
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            for key, value in (item.get("token_usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tokens[key] = tokens.get(key, 0) + value
        return {
            "schema": "tracelab-final-round-controls/1",
            "executed": self.executed,
            "execution_origin": self.execution_origin,
            "controls": len(self.controls),
            "inputs": len(self.snapshots),
            "receipts": len(self.receipts),
            "total_runs": len(self.receipts),
            "valid_verdicts": sum(
                count for verdict, count in verdicts.items() if verdict in {"PASS", "FAIL", "REJECT", "NO_RESULT"}
            ),
            "effective_judgments": sum(
                count for verdict, count in verdicts.items() if verdict in {"PASS", "FAIL", "REJECT"}
            ),
            "valid_judgments": sum(
                count for verdict, count in verdicts.items() if verdict in {"PASS", "FAIL", "REJECT"}
            ),
            "no_result": verdicts.get("NO_RESULT", 0),
            "invalid_verdicts": sum(
                count for verdict, count in verdicts.items() if verdict not in {"PASS", "FAIL", "REJECT", "NO_RESULT"}
            ),
            "input_errors": self.input_errors,
            "output_errors": self.output_errors,
            "success_ratio": (
                (len(self.receipts) - verdicts.get("NO_RESULT", 0)) / len(self.receipts)
                if self.receipts
                else None
            ),
            "verdicts": verdicts,
            "token_usage": tokens,
            "expected_boundary_met": sum(1 for row in self.controls if row.get("expected_boundary_met")),
            "evaluation_status": {
                status: sum(1 for row in self.controls if row.get("evaluation_status") == status)
                for status in ("COMPLETE", "PARTIAL", "INSUFFICIENT_EVIDENCE")
            },
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "retryable_errors": self.retryable_errors,
            "results_sha256": sha256_json(self.controls),
        }


VALID_CONTROL_VERDICTS = frozenset({"PASS", "FAIL", "REJECT"})


def _evaluate_boundary(
    control_type: str,
    baseline: list[str],
    variant: list[str],
) -> dict[str, Any]:
    """按 Control Type 的既有实验定义计算预期关系，不把 NO_RESULT 当作达成。"""
    all_verdicts = baseline + variant
    valid_inputs = sum(verdict in VALID_CONTROL_VERDICTS for verdict in all_verdicts)
    no_result_inputs = len(all_verdicts) - valid_inputs
    verdict_consistent = bool(all_verdicts) and len(set(all_verdicts)) == 1
    if valid_inputs == 0:
        status = "INSUFFICIENT_EVIDENCE"
    elif no_result_inputs:
        status = "PARTIAL"
    else:
        status = "COMPLETE"

    met = False
    if status == "COMPLETE":
        if control_type in {"EQUIVALENT", "PROMPT_INJECTION", "DIFFERENT_VALID_PATHS"}:
            met = len(set(all_verdicts)) == 1
        elif control_type == "MISSING_EVIDENCE":
            met = bool(variant) and all(verdict == "REJECT" for verdict in variant)
        elif control_type == "FACT_COUNTERFACTUAL":
            met = bool(baseline) and bool(variant) and set(baseline) != set(variant)
        else:
            # 只有自然语言 expected_boundary、没有可计算验收条件的实验不推测通过。
            status = "INSUFFICIENT_EVIDENCE"
    return {
        "valid_inputs": valid_inputs,
        "no_result_inputs": no_result_inputs,
        "verdict_consistent": verdict_consistent,
        "expected_boundary_met": met and status == "COMPLETE",
        "evaluation_status": status,
    }


def run_controls(
    controls: list[dict[str, Any]],
    judge: JudgeClient,
    *,
    max_workers: int = 1,
    execution_origin: str | None = None,
    checkpoint_path: str | Path | None = None,
    rubric_version: str = "final-round-v0",
    config_version: str = "scripted-v0",
) -> ControlBatch:
    origin = execution_origin or str(getattr(judge, "execution_origin", "UNKNOWN"))
    control_ids = [str(control.get("control_id") or "") for control in controls]
    counts = Counter(cid for cid in control_ids if cid)
    duplicates = sorted(cid for cid, count in counts.items() if count > 1)
    if duplicates:
        raise ControlFormatError(f"control_id 重复：{', '.join(duplicates)}")
    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    prepared_all = [
        prepare_input(control, side, index, item, judge, origin, rubric_version, config_version)
        for control in controls
        for side in ("baseline_inputs", "variant_inputs")
        for index, item in enumerate(control[side])
    ]
    warnings: list[str] = []
    if checkpoint and origin.startswith("LIVE_MODEL"):
        missing_digest = any(
            not prompt_digest(judge, (prepared.get("payload") or {}).get("judge_input", {}).get("business_type"))
            for prepared in prepared_all
            if prepared.get("payload")
        )
        if missing_digest:
            warnings.append("LIVE_MODEL Judge 缺少 prompt_digest，已禁用本批 checkpoint 复用")
            checkpoint = None
    done = load_records(checkpoint, required_fields=("assessment", "raw")) if checkpoint else {}
    outcomes: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for prepared in prepared_all:
        key = checkpoint_key(prepared["identity"])
        cached = done.get(key)
        if cached and cached.get("identity") == prepared["identity"] and not prepared.get("input_error"):
            outcomes.append(
                {
                    "control": prepared["control"],
                    "side": prepared["side"],
                    "index": prepared["index"],
                    "result": cached["outcome"],
                }
            )
        else:
            jobs.append(prepared)

    call_lock = None if callable(getattr(judge, "evaluate_with_meta", None)) else Lock()

    def work(prepared: dict[str, Any]) -> dict[str, Any]:
        return {
            "control": prepared["control"],
            "side": prepared["side"],
            "index": prepared["index"],
            "result": evaluate_prepared(prepared, judge, origin, call_lock),
        }

    if max_workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(work, prepared): prepared for prepared in jobs}
            for future in as_completed(futures):
                outcome = future.result()
                outcomes.append(outcome)
                result = outcome["result"]
                if checkpoint and not result.get("error"):
                    key = checkpoint_key(result["identity"])
                    append_record(checkpoint, key, result["identity"], result)
    else:
        for prepared in jobs:
            outcome = work(prepared)
            outcomes.append(outcome)
            result = outcome["result"]
            if checkpoint and not result.get("error"):
                key = checkpoint_key(result["identity"])
                append_record(checkpoint, key, result["identity"], result)

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for outcome in outcomes:
        control_id = outcome["control"]["control_id"]
        grouped.setdefault(control_id, {"baseline_inputs": [], "variant_inputs": []})
        grouped[control_id][outcome["side"]].append(outcome)

    chain = ReceiptChain()
    batch_id = f"controls-{sha256_json([row.get('control_id') for row in controls])[:16]}"
    snapshots: list[dict[str, Any]] = []
    output_controls: list[dict[str, Any]] = []
    issues: list[str] = []
    for control in controls:
        sides = grouped.get(control["control_id"], {"baseline_inputs": [], "variant_inputs": []})
        row: dict[str, Any] = {
            "control_id": control["control_id"],
            "control_type": control["control_type"],
            "expected_boundary": control["expected_boundary"],
            "source_case_id": control.get("source_case_id"),
            "paired_case_id": control.get("paired_case_id"),
            "baseline": [],
            "variant": [],
        }
        for extra in ("requested_judge", "appropriate_judge"):
            if extra in control:
                row[extra] = control[extra]
        verdicts: list[str] = []
        side_verdicts: dict[str, list[str]] = {"baseline": [], "variant": []}
        for side_key, side_name in (("baseline_inputs", "baseline"), ("variant_inputs", "variant")):
            for outcome in sorted(sides[side_key], key=lambda item: item["index"]):
                result = outcome["result"]
                if result.get("judge_input"):
                    snapshots.append(
                        {
                            "judge_input": result["judge_input"],
                            "linkage_only": result["linkage"],
                            "judge_input_sha256": result["judge_input_sha256"],
                            "input_snapshot_id": f"js-{result['judge_input_sha256'][:16]}",
                        }
                    )
                raw = result["raw"]
                verdict = str(raw.get("verdict") or "NO_RESULT")
                verdicts.append(verdict)
                side_verdicts[side_name].append(verdict)
                row[side_name].append(
                    {
                        "index": outcome["index"],
                        "verdict": verdict,
                        "agent_task_success": result["assessment"].get("agent_task_success"),
                        "reason": result["assessment"].get("reason"),
                        "assessment": result["assessment"],
                        "input_snapshot_id": (
                            f"js-{result['judge_input_sha256'][:16]}" if result.get("judge_input_sha256") else ""
                        ),
                    }
                )
                chain.append(
                    receipt_from_call(
                        sequence=len(chain.receipts),
                        call_id=result["assessment"]["call_linkage"]["call_id"],
                        case_id=str(control.get("source_case_id") or control["control_id"]),
                        run_id=str(result["linkage"].get("run_id") or "CTRL-RUN"),
                        business_type=str(result["judge_input"].get("business_type") or "unknown"),
                        judge_id=str(getattr(judge, "judge_id", "unknown")),
                        judge_version=str(getattr(judge, "judge_version", "unknown")),
                        model=str(getattr(judge, "model", "unknown")),
                        prompt_version=str(getattr(judge, "prompt_version", "unknown")),
                        input_snapshot_id=(
                            f"js-{result['judge_input_sha256'][:16]}" if result.get("judge_input_sha256") else "js-invalid"
                        ),
                        judge_input_sha256=result.get("judge_input_sha256") or "",
                        output=raw,
                        verdict=verdict,
                        attempt_id="attempt-1",
                        execution_origin=origin,
                        duration_ms=result.get("duration_ms") or 0.0,
                        error=result.get("error", ""),
                        batch_id=batch_id,
                        **receipt_meta_fields(result.get("call_meta")),
                    )
                )
        row["consistent"] = len(set(verdicts)) == 1
        row.update(_evaluate_boundary(str(control["control_type"]), side_verdicts["baseline"], side_verdicts["variant"]))
        output_controls.append(row)

    issues.extend(f"收据链: {problem}" for problem in chain.verify())
    return ControlBatch(
        controls=output_controls,
        receipts=chain.to_dicts(),
        snapshots=snapshots,
        issues=issues,
        execution_origin=origin,
        executed=origin.startswith("LIVE_MODEL"),
        retryable_errors=sum(1 for outcome in outcomes if outcome["result"].get("error")),
        input_errors=sum(1 for outcome in outcomes if outcome["result"].get("input_error")),
        output_errors=sum(1 for outcome in outcomes if outcome["result"].get("output_error")),
        warnings=warnings,
    )


def write_control_batch(out_dir: str | Path, batch: ControlBatch) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "controls.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in batch.controls) + "\n",
        encoding="utf-8",
    )
    (out / "receipts.json").write_text(
        json.dumps(batch.receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    snap_dir = out / "judge_inputs"
    snap_dir.mkdir(exist_ok=True)
    for index, snapshot in enumerate(batch.snapshots):
        (snap_dir / f"input-{index:04d}.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (out / "manifest.json").write_text(
        json.dumps(batch.to_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def run_control_file(
    controls_path: str | Path,
    out_dir: str | Path,
    *,
    judge: JudgeClient | None = None,
    max_workers: int = 1,
    checkpoint_path: str | Path | None = None,
    rubric_version: str = "final-round-v0",
    config_version: str = "scripted-v0",
) -> ControlBatch:
    controls = load_controls(controls_path)
    batch = run_controls(
        controls,
        judge or ScriptedJudge(),
        max_workers=max_workers,
        checkpoint_path=checkpoint_path,
        rubric_version=rubric_version,
        config_version=config_version,
    )
    write_control_batch(out_dir, batch)
    return batch
