"""通用断点记录 I/O（runner 与 controls 共用，避免两套实现漂移）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .judge_input import sha256_json

REQUIRED_TYPES: dict[str, type] = {
    "assessment": dict,
    "raw": dict,
    "verdict": str,
    "payload": dict,
}


def checkpoint_key(identity: dict[str, Any]) -> str:
    return sha256_json(identity)


def load_records(path: Path, *, required_fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """读取已完成断点；字段缺失或类型不符的记录跳过，不参与复用。"""
    done: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("state") != "completed":
            continue
        key, identity, outcome = row.get("key"), row.get("identity"), row.get("outcome")
        if not isinstance(key, str) or not isinstance(identity, dict) or not isinstance(outcome, dict):
            continue
        valid = True
        for field in required_fields:
            value = outcome.get(field)
            expected = REQUIRED_TYPES.get(field)
            if value is None or (expected is not None and not isinstance(value, expected)):
                valid = False
                break
        if valid and isinstance(outcome.get("assessment"), dict):
            assessment = outcome["assessment"]
            if not all(isinstance(assessment.get(field), str) for field in ("run_id", "verdict", "reason")):
                valid = False
            linkage = assessment.get("call_linkage")
            if not isinstance(linkage, dict):
                valid = False
            else:
                for link_field in (
                    "call_id",
                    "task_id",
                    "input_snapshot_id",
                    "judge_input_sha256",
                    "model",
                    "prompt_version",
                    "execution_origin",
                ):
                    if not isinstance(linkage.get(link_field), str) or not linkage[link_field]:
                        valid = False
                        break
        if valid and isinstance(outcome.get("payload"), dict):
            payload = outcome["payload"]
            if not isinstance(payload.get("judge_input"), dict) or not isinstance(
                payload.get("linkage_only"), dict
            ):
                valid = False
            if not isinstance(payload.get("judge_input_sha256"), str):
                valid = False
        if valid and isinstance(outcome.get("raw"), dict) and not isinstance(
            outcome["raw"].get("verdict"), str
        ):
            valid = False
        if valid:
            done[key] = {"identity": identity, "outcome": outcome}
    return done


def append_record(path: Path, key: str, identity: dict[str, Any], outcome: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"key": key, "identity": identity, "state": "completed", "outcome": outcome},
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()
