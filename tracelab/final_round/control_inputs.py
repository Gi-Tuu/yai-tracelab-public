"""专项 Control 的输入准备、断点身份与检查点 I/O。

与 `controls.py`（编排/输出）分离，避免单文件同时承担加载、输入、执行、收据与落盘。
"""

from __future__ import annotations

from typing import Any

from .identity import base_identity, judge_meta
from .judge import JudgeClient
from .judge_input import JudgeInputContractError, normalize_judge_input


def prepare_input(
    control: dict[str, Any],
    side: str,
    index: int,
    item: dict[str, Any],
    judge: JudgeClient,
    origin: str,
    rubric_version: str,
    config_version: str,
) -> dict[str, Any]:
    """规范化输入并构造断点身份；输入不合规时返回 input_error，不调用 Judge。"""
    meta = judge_meta(judge, origin)
    try:
        payload = normalize_judge_input(
            item["judge_input"],
            linkage=dict(item.get("linkage_only") or {}),
            expected_sha256=item.get("judge_input_sha256"),
        )
    except JudgeInputContractError as exc:
        return {
            "control": control,
            "side": side,
            "index": index,
            "payload": None,
            "input_error": str(exc),
            "identity": {
                "control_id": control["control_id"],
                "side": side,
                "index": index,
                "judge_input_sha256": None,
                "prompt_sha256": str(getattr(judge, "prompt_sha256", "") or ""),
                **meta,
                "rubric_version": rubric_version,
                "config_version": config_version,
            },
        }
    identity = base_identity(
        judge,
        origin,
        business_type=payload["judge_input"].get("business_type"),
        judge_input_sha256=payload["judge_input_sha256"],
        extra={
            "control_id": control["control_id"],
            "side": side,
            "index": index,
            "rubric_version": rubric_version,
            "config_version": config_version,
        },
    )
    return {
        "control": control,
        "side": side,
        "index": index,
        "payload": payload,
        "input_error": "",
        "identity": identity,
    }


