"""Judge 调用收据与哈希链（纯标准库）。

复赛要求"版本与数据治理"可追溯：每次 Judge 调用都要能回答
"谁（judge/version）、用什么输入（快照哈希）、得到什么输出（输出哈希）、
是否成功、属于哪次 attempt"。这里用 prev_hash + SHA-256 串成防篡改链。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .judge_input import sha256_json

RECEIPT_STATUSES = frozenset({"ok", "rejected", "no_result", "error"})
RECEIPT_SCHEMA_VERSION = "2"
EXTENDED_RECEIPT_FIELDS = (
    "response_model",
    "prompt_sha256",
    "messages_sha256",
    "attempts",
    "token_usage",
    "batch_id",
    "call_status",
)


@dataclass(frozen=True)
class JudgeReceipt:
    sequence: int
    call_id: str
    case_id: str
    run_id: str
    business_type: str
    judge_id: str
    judge_version: str
    model: str
    prompt_version: str
    input_snapshot_id: str
    judge_input_sha256: str
    output_sha256: str
    verdict: str
    attempt_id: str
    execution_origin: str
    duration_ms: float
    error: str = ""
    response_model: str = ""
    prompt_sha256: str = ""
    messages_sha256: str = ""
    attempts: int = 0
    token_usage: dict[str, Any] | None = None
    batch_id: str = ""
    call_status: str = ""
    schema_version: str = ""
    prev_hash: str = ""
    receipt_hash: str = ""

    @property
    def metadata_status(self) -> str:
        """v2 元数据可信；v1-ext 只有部分扩展字段；v0 无扩展元数据。"""
        if self.schema_version == RECEIPT_SCHEMA_VERSION:
            return "reliable_v2"
        if self._has_extended_fields():
            return "partial_v1_extended"
        return "legacy_v0_unreliable"

    @property
    def metadata_reliable(self) -> bool:
        return self.schema_version == RECEIPT_SCHEMA_VERSION

    def _has_extended_fields(self) -> bool:
        return self.schema_version == RECEIPT_SCHEMA_VERSION or any(
            getattr(self, field) not in ("", 0, None) for field in EXTENDED_RECEIPT_FIELDS
        )

    def _payload(self, prev_hash: str) -> dict[str, Any]:
        payload = {
            "sequence": self.sequence,
            "call_id": self.call_id,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "business_type": self.business_type,
            "judge_id": self.judge_id,
            "judge_version": self.judge_version,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_snapshot_id": self.input_snapshot_id,
            "judge_input_sha256": self.judge_input_sha256,
            "output_sha256": self.output_sha256,
            "verdict": self.verdict,
            "attempt_id": self.attempt_id,
            "execution_origin": self.execution_origin,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "prev_hash": prev_hash,
        }
        if self._has_extended_fields():
            payload.update(
                {
                    "response_model": self.response_model,
                    "prompt_sha256": self.prompt_sha256,
                    "attempts": self.attempts,
                    "token_usage": self.token_usage,
                    "batch_id": self.batch_id,
                    "call_status": self.call_status,
                }
            )
            if self.schema_version == RECEIPT_SCHEMA_VERSION or self.messages_sha256:
                payload["messages_sha256"] = self.messages_sha256
            # v2 显式带版本；v1-ext（已有扩展字段但无版本）保持旧哈希口径。
            if self.schema_version == RECEIPT_SCHEMA_VERSION:
                payload["schema_version"] = self.schema_version
        return payload

    def seal(self, prev_hash: str) -> JudgeReceipt:
        payload = self._payload(prev_hash)
        return replace(
            self,
            prev_hash=prev_hash,
            receipt_hash=hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(self.prev_hash), "receipt_hash": self.receipt_hash}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JudgeReceipt:
        return cls(
            sequence=int(data.get("sequence", 0)),
            call_id=str(data.get("call_id", "")),
            case_id=str(data.get("case_id", "")),
            run_id=str(data.get("run_id", "")),
            business_type=str(data.get("business_type", "")),
            judge_id=str(data.get("judge_id", "")),
            judge_version=str(data.get("judge_version", "")),
            model=str(data.get("model", "")),
            prompt_version=str(data.get("prompt_version", "")),
            input_snapshot_id=str(data.get("input_snapshot_id", "")),
            judge_input_sha256=str(data.get("judge_input_sha256", "")),
            output_sha256=str(data.get("output_sha256", "")),
            verdict=str(data.get("verdict", "")),
            attempt_id=str(data.get("attempt_id", "")),
            execution_origin=str(data.get("execution_origin", "")),
            duration_ms=float(data.get("duration_ms", 0.0) or 0.0),
            error=str(data.get("error", "")),
            response_model=str(data.get("response_model", "")),
            prompt_sha256=str(data.get("prompt_sha256", "")),
            messages_sha256=str(data.get("messages_sha256", "")),
            attempts=int(data.get("attempts", 0) or 0),
            token_usage=dict(data.get("token_usage") or {}) or None,
            batch_id=str(data.get("batch_id", "")),
            call_status=str(data.get("call_status", "")),
            schema_version=str(data.get("schema_version", "")),
            prev_hash=str(data.get("prev_hash", "")),
            receipt_hash=str(data.get("receipt_hash", "")),
        )


class ReceiptChain:
    """按追加顺序生成哈希链；任何一条被改都会导致后续校验失败。"""

    def __init__(self, receipts: Iterable[JudgeReceipt] | None = None) -> None:
        self._receipts: list[JudgeReceipt] = list(receipts or [])

    def append(self, receipt: JudgeReceipt) -> JudgeReceipt:
        previous = self._receipts[-1].receipt_hash if self._receipts else ""
        sealed = receipt.seal(previous)
        self._receipts.append(sealed)
        return sealed

    @property
    def receipts(self) -> list[JudgeReceipt]:
        return list(self._receipts)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._receipts]

    def verify(self) -> list[str]:
        """返回问题列表；空列表=链完整。"""
        problems: list[str] = []
        previous = ""
        for index, receipt in enumerate(self._receipts):
            if receipt.sequence != index:
                problems.append(f"第 {index} 条 sequence 应为 {index}，实际 {receipt.sequence}")
            if receipt.prev_hash != previous:
                problems.append(f"第 {index} 条 prev_hash 不匹配")
            expected = receipt.seal(previous)
            if expected.receipt_hash != receipt.receipt_hash:
                problems.append(f"第 {index} 条 receipt_hash 不匹配（可能被篡改）")
            previous = receipt.receipt_hash
        return problems


def receipt_meta_fields(call_meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """把一次调用的元数据映射为收据可选字段，避免 runner/controls 各自复制字段表。"""
    meta = dict(call_meta or {})
    token_usage = meta.get("token_usage")
    return {
        "response_model": str(meta.get("response_model", "")),
        "prompt_sha256": str(meta.get("prompt_sha256", "")),
        "messages_sha256": str(meta.get("messages_sha256", "")),
        "attempts": int(meta.get("attempts", 0) or 0),
        "token_usage": token_usage if isinstance(token_usage, dict) else None,
        "call_status": str(meta.get("status", "")),
    }


def receipt_from_call(
    *,
    sequence: int,
    call_id: str,
    case_id: str,
    run_id: str,
    business_type: str,
    judge_id: str,
    judge_version: str,
    model: str,
    prompt_version: str,
    input_snapshot_id: str,
    judge_input_sha256: str,
    output: Any,
    verdict: str,
    attempt_id: str,
    execution_origin: str,
    duration_ms: float,
    error: str = "",
    response_model: str = "",
    prompt_sha256: str = "",
    messages_sha256: str = "",
    attempts: int = 0,
    token_usage: dict[str, Any] | None = None,
    batch_id: str = "",
    call_status: str = "",
    schema_version: str = RECEIPT_SCHEMA_VERSION,
) -> JudgeReceipt:
    return JudgeReceipt(
        sequence=sequence,
        call_id=call_id,
        case_id=case_id,
        run_id=run_id,
        business_type=business_type,
        judge_id=judge_id,
        judge_version=judge_version,
        model=model,
        prompt_version=prompt_version,
        input_snapshot_id=input_snapshot_id,
        judge_input_sha256=judge_input_sha256,
        output_sha256=sha256_json(output),
        verdict=verdict,
        attempt_id=attempt_id,
        execution_origin=execution_origin,
        duration_ms=duration_ms,
        error=error,
        response_model=response_model,
        prompt_sha256=prompt_sha256,
        messages_sha256=messages_sha256,
        attempts=attempts,
        token_usage=token_usage,
        batch_id=batch_id,
        call_status=call_status,
        schema_version=schema_version,
    )
