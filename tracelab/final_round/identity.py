"""Judge 身份的共享构造：runner 与专项集共用，避免 Prompt/模型字段漂移。"""

from __future__ import annotations

from .judge import JudgeClient


def judge_meta(judge: JudgeClient, origin: str) -> dict[str, str]:
    return {
        "judge_id": str(getattr(judge, "judge_id", "unknown")),
        "judge_version": str(getattr(judge, "judge_version", "unknown")),
        "model": str(getattr(judge, "model", "unknown")),
        "prompt_version": str(getattr(judge, "prompt_version", "unknown")),
        "execution_origin": origin,
    }


def prompt_digest(judge: JudgeClient, business_type: str | None) -> str:
    """纯 Prompt 内容摘要；旧 Judge 没有该方法时退化为显式属性。"""
    digest = getattr(judge, "prompt_digest", None)
    if callable(digest):
        return str(digest(business_type) or "")
    return str(getattr(judge, "prompt_sha256", "") or "")


def base_identity(
    judge: JudgeClient,
    origin: str,
    *,
    business_type: str | None,
    judge_input_sha256: str,
    extra: dict[str, object],
) -> dict[str, object]:
    """构造 runner/controls 共用的身份基础字段，再附加各自局部字段。"""
    identity: dict[str, object] = {
        "business_type": str(business_type or ""),
        "judge_input_sha256": judge_input_sha256,
        "prompt_sha256": prompt_digest(judge, business_type),
        **judge_meta(judge, origin),
    }
    identity.update(extra)
    return identity
