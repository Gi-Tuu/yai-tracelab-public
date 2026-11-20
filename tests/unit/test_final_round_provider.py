"""复赛网关客户端与 Judge 输出解析的离线测试（不联网、不需要真实 Key）。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tracelab.final_round import (
    JudgeOutputError,
    OpenAICompatJudge,
    ProviderError,
    build_messages,
    chat_completions,
    list_models,
    parse_judge_output,
    resolve_api_key,
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静默
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append(("GET", self.path))  # type: ignore[attr-defined]
        self._send(200, {"data": [{"id": "qwen3.8-max"}]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(("POST", self.path))  # type: ignore[attr-defined]
        plan = self.server.plan  # type: ignore[attr-defined]
        status, payload = plan.pop(0) if plan else (200, {})
        self._send(status, payload)


@pytest.fixture()
def mock_gateway(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.plan = []  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    monkeypatch.setenv("YXAIHUB_BASE_URL", base)
    monkeypatch.setenv("YXAIHUB_API_KEY", "test-key")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _chat_ok(content: str = '{"verdict":"REJECT","reason":"测试"}') -> dict[str, Any]:
    return {
        "model": "qwen3.8-max",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_chat_completions_success(mock_gateway):
    mock_gateway.plan.append((200, _chat_ok()))
    result = chat_completions([{"role": "user", "content": "hi"}], retries=0)
    assert result.content.startswith("{")
    assert result.usage["total_tokens"] == 15
    assert mock_gateway.requests == [("POST", "/v1/chat/completions")]


def test_chat_completions_401_is_not_retried(mock_gateway):
    mock_gateway.plan.append((401, {"error": "Invalid token"}))
    with pytest.raises(ProviderError) as info:
        chat_completions([{"role": "user", "content": "hi"}], retries=3)
    assert info.value.status_code == 401
    assert info.value.retryable is False
    assert len(mock_gateway.requests) == 1


def test_chat_completions_retries_5xx_then_succeeds(mock_gateway):
    mock_gateway.plan.extend([(500, {"error": "busy"}), (200, _chat_ok("ok"))])
    result = chat_completions([{"role": "user", "content": "hi"}], retries=1, backoff=0.01)
    assert result.content == "ok"
    assert len(mock_gateway.requests) == 2


def test_chat_completions_empty_content_fails(mock_gateway):
    mock_gateway.plan.append((200, {"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(ProviderError, match="空 content"):
        chat_completions([{"role": "user", "content": "hi"}], retries=0)


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("YXAIHUB_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="YXAIHUB_API_KEY"):
        chat_completions([{"role": "user", "content": "hi"}], retries=0)


def test_list_models(mock_gateway):
    assert list_models() == ["qwen3.8-max"]
    assert mock_gateway.requests == [("GET", "/v1/models")]


def test_parse_judge_output_accepts_fenced_json():
    obj = parse_judge_output('```json\n{"verdict":"PASS","reason":"ok"}\n```')
    assert obj["verdict"] == "PASS"


def test_parse_judge_output_rejects_non_json():
    with pytest.raises(JudgeOutputError):
        parse_judge_output("模型说：我认为应该通过")


def test_build_messages_include_business_addendum():
    invoice = build_messages({"business_type": "invoice"})
    consultant = build_messages({"business_type": "ai_consultant"})
    assert "invoice" in invoice[0]["content"] and "business_invoice_created" in invoice[0]["content"]
    assert "ai_consultant" in consultant[0]["content"] and "五项" in consultant[0]["content"]


def test_openai_judge_returns_parsed_json(mock_gateway):
    mock_gateway.plan.append((200, _chat_ok('{"verdict":"REJECT","reason":"证据不足"}')))
    judge = OpenAICompatJudge()
    result = judge.evaluate({"business_type": "invoice"}, {"case_id": "C1"})
    assert result["verdict"] == "REJECT"
    assert judge.model == "qwen3.8-max"
    assert judge.execution_origin == "LIVE_MODEL"


def test_resolve_api_key_from_private_file(tmp_path, monkeypatch):
    monkeypatch.delenv("YXAIHUB_API_KEY", raising=False)
    key_file = tmp_path / "yxaihub.key"
    key_file.write_text("file-key\n", encoding="utf-8")
    assert resolve_api_key(key_file=str(key_file)) == "file-key"
    with pytest.raises(ProviderError, match="不存在"):
        resolve_api_key(key_file=str(tmp_path / "missing.key"))
