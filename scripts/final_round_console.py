"""复赛本地控制台：同一页面发起开票 / AI 顾问批量评测，查看进度与结果。

零第三方依赖，仅监听 127.0.0.1。示例：
    python scripts/final_round_console.py --data-root E:\\Competition\\final-round-2026
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]

#: 默认路径相对 --data-root；私有目录可用 datasets.json 覆盖，避免把私有路径写进公开代码。
DATASETS = {
    "invoice": {
        "name": "开票 Agent",
        "short": "INV",
        "kind": "cases",
        "path": "cases/invoice_public.jsonl",
        "desc": "金额口径、确认前置、保护性拒绝与提交边界。",
    },
    "ai_public": {
        "name": "AI 顾问 Agent",
        "short": "AIC",
        "kind": "cases",
        "path": "cases/ai_public.jsonl",
        "desc": "七域业务处理、证据依赖、首错归因与空轨迹拒判。",
    },
    "ai_dev": {
        "name": "AI 顾问 dev",
        "short": "DEV",
        "kind": "cases",
        "path": "cases/ai_dev.jsonl",
        "desc": "调优集回归，用于 prompt 与 Judge 边界校准。",
    },
    "controls": {
        "name": "专项验收集",
        "short": "CTRL",
        "kind": "controls",
        "path": "cases/controls.jsonl",
        "desc": "等义、注入、缺证、反事实、有效路径与错配对照。",
    },
}

JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()


def _tail(path: Path, limit: int = 5000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _assessment_steps(assessment: dict[str, Any]) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    for dimension in assessment.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        conclusion = str(dimension.get("conclusion") or "UNKNOWN")
        evidence = dimension.get("evidence_event_ids") or []
        steps.append(
            {
                "type": "root" if conclusion == "FAIL" else "normal",
                "time": conclusion,
                "title": str(dimension.get("name") or "dimension"),
                "desc": str(dimension.get("reason") or "") + (
                    f"；证据：{', '.join(str(x) for x in evidence)}" if evidence else ""
                ),
            }
        )
    first_fault = assessment.get("first_fault_event_id")
    if first_fault:
        steps.insert(
            0,
            {
                "type": "root",
                "time": "first",
                "title": f"首错事件 {first_fault}",
                "desc": str(assessment.get("reason") or ""),
            },
        )
    return steps


def _job_detail(job: dict[str, Any]) -> dict[str, Any]:
    """读取真实产物，供页面逐 Case / 对照视图使用；不生成任何假数据。"""
    out = Path(str(job.get("out") or ""))
    results = _read_jsonl(out / "results.jsonl")
    controls = _read_jsonl(out / "controls.jsonl")
    cases: list[dict[str, Any]] = []
    ab: list[dict[str, Any]] = []
    if results:
        for row in results:
            case_id = str(row.get("case_id") or "")
            for assessment in row.get("run_assessments") or []:
                if not isinstance(assessment, dict):
                    continue
                run_id = str(assessment.get("run_id") or "")
                cases.append(
                    {
                        "id": f"{case_id} / {run_id}",
                        "verdict": str(assessment.get("verdict") or "NO_RESULT"),
                        "firstFault": str(assessment.get("first_fault_event_id") or "无首错"),
                        "steps": _assessment_steps(assessment),
                    }
                )
            comparison = row.get("version_comparison") or {}
            if isinstance(comparison, dict) and comparison.get("direction"):
                ab.append(
                    {
                        "domain": case_id,
                        "base": str(comparison.get("direction")),
                        "cand": "—",
                        "delta": "—",
                        "advice": str(comparison.get("reason") or ""),
                    }
                )
        return {"kind": "cases", "cases": cases, "ab": ab}
    if controls:
        for row in controls:
            control_id = str(row.get("control_id") or "")
            for side in ("baseline", "variant"):
                for item in row.get(side) or []:
                    if not isinstance(item, dict):
                        continue
                    assessment = item.get("assessment") or {}
                    cases.append(
                        {
                            "id": f"{control_id} / {side} / {item.get('index', '?')}",
                            "verdict": str(item.get("verdict") or "NO_RESULT"),
                            "firstFault": str(assessment.get("first_fault_event_id") or "无首错"),
                            "steps": _assessment_steps(assessment) if isinstance(assessment, dict) else [],
                        }
                    )
            ab.append(
                {
                    "domain": control_id,
                    "base": str(row.get("evaluation_status") or "—"),
                    "cand": str(row.get("expected_boundary_met")),
                    "delta": "—",
                    "advice": str(row.get("expected_boundary") or ""),
                }
            )
        return {"kind": "controls", "cases": cases, "ab": ab}
    return {"kind": "empty", "cases": [], "ab": []}


def _summarize(out: Path) -> dict[str, Any]:
    """从产物中提取轻量汇总，供控制台卡片展示。"""
    results = out / "results.jsonl"
    if results.is_file():
        counts: Counter[str] = Counter()
        cases = runs = 0
        for line in results.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cases += 1
            for item in row.get("run_assessments") or []:
                runs += 1
                counts[str(item.get("verdict"))] += 1
        return {"kind": "cases", "cases": cases, "runs": runs, "verdicts": dict(counts)}
    controls = out / "controls.jsonl"
    if controls.is_file():
        counts = Counter()
        controls_count = inputs = 0
        for line in controls.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            controls_count += 1
            for side in ("baseline", "variant"):
                for item in row.get(side) or []:
                    inputs += 1
                    counts[str(item.get("verdict"))] += 1
        return {"kind": "controls", "controls": controls_count, "inputs": inputs, "verdicts": dict(counts)}
    return {}


def _run_job(job_id: str, dataset: dict[str, Any], data_root: Path, live: bool) -> None:
    job = JOBS[job_id]
    out = data_root / "console-runs" / job_id
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "run.log"
    cmd = [sys.executable]
    if dataset["kind"] == "controls":
        cmd += [
            str(ROOT / "scripts/final_round_controls.py"),
            "--controls", str(data_root / dataset["path"]),
            "--out", str(out),
            "--workers", "4",
        ]
    else:
        cmd += [
            str(ROOT / "scripts/final_round_smoke.py"),
            "--cases", str(data_root / dataset["path"]),
            "--out", str(out),
            "--workers", "4",
        ]
    if live:
        key = data_root / "yxaihub.key"
        if not key.is_file():
            job.update(status="failed", log="缺少官方 Key 文件 yxaihub.key", finished=time.strftime("%H:%M:%S"))
            return
        cmd += ["--live", "--api-key-file", str(key)]
    job.update(
        status="running",
        cmd=" ".join(cmd),
        started=time.strftime("%H:%M:%S"),
        out=str(out),
        log_path=str(log_path),
    )
    with log_path.open("w", encoding="utf-8") as handle:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT)
    summary = _summarize(out)
    job.update(
        status="done" if proc.returncode == 0 else "failed",
        code=proc.returncode,
        finished=time.strftime("%H:%M:%S"),
        summary=summary,
    )


PAGE_PATH = Path(__file__).with_name("final_round_console_page.html")


def _render_page() -> str:
    html = PAGE_PATH.read_text(encoding="utf-8")
    # 防止数据集配置中的 </script> 提前闭合脚本；页面数据只来自本机可信配置。
    payload = json.dumps(DATASETS, ensure_ascii=False).replace("</", "<\\/")
    return html.replace("__DATASETS__", payload)


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "TraceLabFinalRound/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = _render_page()
            self._send(200, html.encode("utf-8"), "text/html")
            return
        if parsed.path == "/api/jobs":
            with LOCK:
                items = []
                for job_id, job in list(JOBS.items())[-20:][::-1]:
                    item = dict(job)
                    item["id"] = job_id
                    item["dataset_name"] = DATASETS.get(item.get("dataset", ""), {}).get("name", item.get("dataset", ""))
                    item["log"] = _tail(Path(item["log_path"])) if item.get("log_path") else item.get("log", "")
                    items.append(item)
            self._json(200, {"jobs": items})
            return
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with LOCK:
                job = dict(JOBS.get(job_id) or {})
            if not job:
                self._json(404, {"error": "job not found"})
                return
            self._json(200, _job_detail(job))  # type: ignore[attr-defined]
            return
        if parsed.path == "/api/status":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            job = JOBS.get(job_id)
            if not job:
                self._json(404, {"error": "job not found"})
                return
            payload = {k: job.get(k) for k in ("status", "cmd", "summary", "code")}
            payload["log"] = _tail(Path(job["log_path"])) if job.get("log_path") else job.get("log", "")
            self._json(200, payload)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/run":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        dataset_key = body.get("dataset")
        dataset = DATASETS.get(dataset_key)
        if not dataset:
            self._json(400, {"error": "unknown dataset"})
            return
        job_id = uuid.uuid4().hex[:12]
        live = bool(body.get("live"))
        with LOCK:
            JOBS[job_id] = {
                "status": "starting",
                "dataset": dataset_key,
                "live": live,
                "started": time.strftime("%H:%M:%S"),
                "log": "",
            }
        thread = threading.Thread(
            target=_run_job,
            args=(job_id, dataset, Path(self.server.data_root), live),  # type: ignore[attr-defined]
            daemon=True,
        )
        thread.start()
        self._json(200, {"id": job_id})


def main() -> int:
    parser = argparse.ArgumentParser(description="复赛本地控制台")
    parser.add_argument("--host", default="127.0.0.1", help="只允许 loopback；控制台不做远程认证")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--data-root", default=str(Path.home() / "final-round"))
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        parser.error("控制台只允许 127.0.0.1/localhost/::1；外部监听需要另加认证层，当前版本拒绝")
    data_root = Path(args.data_root)
    datasets_file = data_root / "datasets.json"
    if datasets_file.is_file():
        loaded = json.loads(datasets_file.read_text(encoding="utf-8-sig"))
        for key, item in loaded.items():
            DATASETS.setdefault(key, {}).update(item)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.data_root = str(data_root)  # type: ignore[attr-defined]
    print(f"控制台：http://{args.host}:{args.port}  (data_root={data_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
