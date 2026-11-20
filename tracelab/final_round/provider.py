"""云帐房复赛网关（OpenAI 兼容）的最小客户端：纯标准库、零运行时依赖。

网关：https://yxaihub.yunzhangfang.com/v1
模型：qwen3.8-max（以 GET /v1/models 为准）
协议：POST /v1/chat/completions

安全：API Key 只从环境变量读取，不写日志、不进代码、不落仓库。
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://yxaihub.yunzhangfang.com/v1"
DEFAULT_MODEL = "qwen3.8-max"


class ProviderError(RuntimeError):
    """网关调用失败；retryable 决定是否值得重试。"""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    usage: dict[str, Any]
    raw: dict[str, Any]
    attempts: int = 1


def resolve_api_key(explicit: str | None = None, key_file: str | None = None) -> str:
    """按 显式参数 → 环境变量 → Key 文件（显式传入，不写死路径）读取。"""
    key = (explicit or os.environ.get("YXAIHUB_API_KEY") or "").strip()
    if key:
        return key
    path = key_file or os.environ.get("YXAIHUB_API_KEY_FILE")
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ProviderError(f"Key 文件不存在：{candidate}", retryable=False)
        key = candidate.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise ProviderError(
        "缺少 API Key：请设置 YXAIHUB_API_KEY，或用 --api-key-file 指向本地私有 Key 文件",
        retryable=False,
    )


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        retryable = exc.code == 429 or 500 <= exc.code < 600
        raise ProviderError(
            f"网关 HTTP {exc.code}: {body}", status_code=exc.code, retryable=retryable
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"网关连接失败: {exc}", retryable=True) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"网关返回非 JSON: {body[:200]!r}", retryable=True) from exc


def chat_completions(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int = 1200,
    temperature: float = 0.0,
    timeout: float = 90.0,
    retries: int = 2,
    backoff: float = 0.8,
    api_key_file: str | None = None,
) -> ChatResult:
    """调用 chat/completions；429/5xx/连接失败按退避重试，4xx 立即失败。"""
    key = resolve_api_key(api_key, api_key_file)
    base = (base_url or os.environ.get("YXAIHUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    target_model = model or os.environ.get("YXAIHUB_MODEL") or DEFAULT_MODEL
    payload = {
        "model": target_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    last: ProviderError | None = None
    for attempt in range(retries + 1):
        try:
            raw = _post_json(f"{base}/chat/completions", payload, headers, timeout)
            choices = raw.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                raise ProviderError("网关返回没有 choices", retryable=True)
            message = choices[0].get("message") or {}
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ProviderError("网关返回空 content", retryable=True)
            return ChatResult(
                content=content,
                model=str(raw.get("model") or target_model),
                usage=dict(raw.get("usage") or {}),
                raw=raw,
                attempts=attempt + 1,
            )
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt >= retries:
                raise
            # 指数退避 + 随机抖动，降低多 worker 同时重试造成的 429 尖峰。
            time.sleep(backoff * (2**attempt) + random.uniform(0, backoff))
    raise last or ProviderError("网关调用失败")


def list_models(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
    api_key_file: str | None = None,
) -> list[str]:
    """GET /v1/models，用于确认实际模型 ID。"""
    key = resolve_api_key(api_key, api_key_file)
    base = (base_url or os.environ.get("YXAIHUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base}/models", headers={"Authorization": f"Bearer {key}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"获取模型列表失败 HTTP {exc.code}", status_code=exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"获取模型列表失败: {exc}", retryable=True) from exc
    data = raw.get("data") if isinstance(raw, dict) else None
    return [str(item.get("id")) for item in data or [] if isinstance(item, dict) and item.get("id")]
