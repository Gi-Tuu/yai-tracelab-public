"""复赛 soak/并发测试：默认用 ScriptedJudge 做合成负载，不消耗模型额度。

用法：
    # 合成：1000 run
    python scripts/final_round_soak.py --runs 1000 --workers 16 --out out/soak-1000
    # 真实网关（显式指定自有 Key/base/model）
    python scripts/final_round_soak.py --live --limit 100 --workers 8 \
        --base-url <own-gateway>/v1 --model qwen/qwen3.7-max \
        --api-key-file <own-api-key> --out out/soak-live
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round import (  # noqa: E402
    OpenAICompatJudge,
    ProviderError,
    ScriptedJudge,
    load_cases,
    resolve_api_key,
    run_cases,
    write_batch,
)


class CountingScriptedJudge(ScriptedJudge):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def evaluate(self, judge_input, linkage):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().evaluate(judge_input, linkage)


def _event(run_id: str) -> dict[str, Any]:
    return {
        "event_id": f"{run_id}-E01",
        "seq": 1,
        "step_id": "INPUT",
        "step_status": "SUCCEEDED",
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
    }


def _case(case_id: str) -> dict[str, Any]:
    return {
        "dataset_version": "soak-v1",
        "data_origin": "synthetic",
        "case_id": case_id,
        "business_type": "ai_consultant",
        "domain": "salary",
        "data_purpose": "soak",
        "near_duplicate_group": "soak",
        "user_request": "soak",
        "context": {"enterprise_id": "SOAK"},
        "reference_result": {
            "business_goal": "soak",
            "pass_criteria": [],
            "required_evidence": [],
            "rule_source": {"source": "soak"},
            "reference_semantics": "business_goal_only",
        },
        "runs": [
            {
                "run_id": f"{case_id}-A",
                "agent_version": "v1",
                "task_id": f"{case_id}-A",
                "trace_id": f"{case_id}-A",
                "events": [_event(f"{case_id}-A")],
                "final_state": "DONE",
                "snapshot_state": "DONE",
                "workflow_terminal": True,
                "observation_complete": True,
            },
            {
                "run_id": f"{case_id}-B",
                "agent_version": "v2",
                "task_id": f"{case_id}-B",
                "trace_id": f"{case_id}-B",
                "events": [_event(f"{case_id}-B")],
                "final_state": "DONE",
                "snapshot_state": "DONE",
                "workflow_terminal": True,
                "observation_complete": True,
            },
        ],
    }


def run_synthetic(runs: int, workers: int, out: Path) -> dict[str, Any]:
    if runs % 2:
        runs += 1
    cases = [_case(f"SOAK-{index:05d}") for index in range(runs // 2)]
    checkpoint = out / "checkpoint.jsonl"
    if checkpoint.exists():
        checkpoint.unlink()
    judge = CountingScriptedJudge()
    tracemalloc.start()
    started = time.perf_counter()
    batch = run_cases(cases, judge, max_workers=workers, checkpoint_path=checkpoint)
    first_elapsed = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(batch.results) == len(cases)
    assert len(batch.receipts) == runs
    assert not batch.issues
    judge.calls = 0
    started = time.perf_counter()
    resumed = run_cases(cases, judge, max_workers=workers, checkpoint_path=checkpoint)
    resume_elapsed = time.perf_counter() - started
    assert judge.calls == 0, "全部已完成的合成批次不应重新调用"
    assert len(resumed.receipts) == runs
    write_batch(out / "batch", resumed)
    return {
        "mode": "synthetic",
        "runs": runs,
        "workers": workers,
        "elapsed_s": round(first_elapsed, 3),
        "resume_elapsed_s": round(resume_elapsed, 3),
        "peak_memory_mb": round(peak / 1024 / 1024, 2),
        "calls": runs,
        "resume_calls": judge.calls,
        "receipts": len(resumed.receipts),
        "issues": resumed.issues,
    }


def run_live(args: argparse.Namespace, out: Path) -> dict[str, Any]:
    dataset_bytes = Path(args.cases).read_bytes()
    dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
    all_cases = load_cases(args.cases)
    cases = all_cases[: args.limit] if args.limit else all_cases
    try:
        resolve_api_key(key_file=args.api_key_file)
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        raise
    judge = OpenAICompatJudge(
        api_key_file=args.api_key_file,
        base_url=args.base_url,
        model=args.model or "qwen3.8-max",
        retries=args.retries,
        timeout=args.timeout,
    )
    if args.model and args.model != "qwen3.8-max":
        judge.execution_origin = "LIVE_MODEL_CUSTOM"
    started = time.perf_counter()
    batch = run_cases(
        cases,
        judge,
        max_workers=args.workers,
        checkpoint_path=out / "checkpoint.jsonl",
    )
    elapsed = time.perf_counter() - started
    write_batch(out / "batch", batch)
    return {
        "mode": "live",
        "model": args.model,
        "base_url_configured": bool(args.base_url),
        "cases": len(cases),
        "source_case_count": args.source_cases or len(cases),
        "derived_case_count": args.derived_cases,
        "dataset_sha256": dataset_sha,
        "dataset_name": Path(args.cases).name,
        "dataset_note": args.dataset_note,
        "runs": 2 * len(cases),
        "workers": args.workers,
        "elapsed_s": round(elapsed, 3),
        "receipts": len(batch.receipts),
        "warnings": batch.warnings,
        "issues": batch.issues,
        "verdicts": batch.to_manifest().get("verdicts", {}),
        "no_result_ratio": (
            batch.to_manifest().get("verdicts", {}).get("NO_RESULT", 0) / (2 * len(cases))
            if cases
            else None
        ),
        "token_usage": batch.to_manifest().get("token_usage", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="复赛 soak/并发测试")
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--out", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cases", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key-file", default=None)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--source-cases", type=int, default=0, help="原始独立 Case 数（派生 soak 用）")
    parser.add_argument("--derived-cases", type=int, default=0, help="派生 Case 数（仅 soak）")
    parser.add_argument("--dataset-note", default="", help="数据集来源说明")
    args = parser.parse_args()
    if args.live and not args.cases:
        parser.error("--live 需要 --cases 指定 Case JSONL")
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = run_live(args, out) if args.live else run_synthetic(args.runs, args.workers, out)
    (out / "soak-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("issues"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
