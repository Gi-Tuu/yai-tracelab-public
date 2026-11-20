"""R2-G1 P1-B：官方 Judge 调用记录（模拟服务，不消耗额度）。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tracelab.final_round.llm_judge import OpenAICompatJudge
from tracelab.final_round.receipts import receipt_from_call


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = {
            "model": "mock-response-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "verdict": "PASS",
                                "agent_task_success": True,
                                "business_result_correct": True,
                                "business_invoice_created": None,
                                "reason": "mock pass",
                            }
                        )
                    }
                }
            ],
        }
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _judge(server, key_file: Path) -> OpenAICompatJudge:
    host, port = server.server_address
    return OpenAICompatJudge(
        api_key_file=str(key_file),
        base_url=f"http://{host}:{port}",
        model="mock-request-model",
        retries=0,
        timeout=10,
    )


def test_call_record_contains_model_prompt_usage_and_attempts(tmp_path: Path):
    key = tmp_path / "key.txt"
    key.write_text("test-key", encoding="utf-8")
    server = _serve()
    try:
        judge = _judge(server, key)
        raw = judge.evaluate(
            {"business_type": "ai_consultant", "user_request": "x", "context": {}, "business_objective": {}, "observable_trace": []},
            {"run_id": "R1"},
        )
        assert raw["verdict"] == "PASS"
        meta = judge.last_call_meta
        assert meta["status"] == "valid"
        assert meta["requested_model"] == "mock-request-model"
        assert meta["response_model"] == "mock-response-model"
        assert meta["token_usage"]["total_tokens"] == 15
        assert meta["attempts"] == 1
        assert meta["prompt_sha256"]
        receipt = receipt_from_call(
            sequence=0,
            call_id="c",
            case_id="case",
            run_id="R1",
            business_type="ai_consultant",
            judge_id=judge.judge_id,
            judge_version=judge.judge_version,
            model=judge.model,
            prompt_version=judge.prompt_version,
            input_snapshot_id="s",
            judge_input_sha256="h",
            output=raw,
            verdict=raw["verdict"],
            attempt_id="a",
            execution_origin="LIVE_MODEL_TEST",
            duration_ms=1.0,
            response_model=meta["response_model"],
            prompt_sha256=meta["prompt_sha256"],
            attempts=meta["attempts"],
            token_usage=meta["token_usage"],
            batch_id="b",
        )
        payload = receipt.to_dict()
        assert payload["response_model"] == "mock-response-model"
        assert payload["token_usage"]["total_tokens"] == 15
        assert payload["prompt_sha256"]
        assert payload["batch_id"] == "b"
    finally:
        server.shutdown()
