"""生成复赛实验索引：数据版本/摘要、模型/Prompt、调用数、有效数、可比较分母。

用法：
    python scripts/final_round_experiment_index.py --name public100 \
        --results <results.jsonl> [--labels <labels.jsonl>] [--dataset <cases.jsonl>] \
        --out <dir> [--notes "适用范围"]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round.metrics import compute_metrics, pair_runs  # noqa: E402
from tracelab.final_round.receipts import JudgeReceipt  # noqa: E402

VERDICTS = {"PASS", "FAIL", "REJECT", "NO_RESULT"}


def _read(path: str | Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: str | Path | None) -> str | None:
    if not path:
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_events(dataset_path: str | Path | None) -> dict[tuple[str, str], set[str]] | None:
    if not dataset_path:
        return None
    events: dict[tuple[str, str], set[str]] = {}
    for row in _read(dataset_path):
        case_id = str(row.get("case_id"))
        for run in row.get("runs") or []:
            run_id = run.get("run_id")
            if isinstance(run_id, str):
                events[(case_id, run_id)] = {
                    str(event.get("event_id"))
                    for event in run.get("events") or []
                    if event.get("event_id")
                }
    return events


def build_index(
    name: str,
    results: list[dict[str, Any]],
    labels: list[dict[str, Any]] | None,
    dataset_path: str | Path | None,
    notes: str = "",
    receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    models: set[str] = set()
    prompts: set[str] = set()
    origins: set[str] = set()
    verdicts: dict[str, int] = {}
    runs = 0
    for row in results:
        for item in row.get("run_assessments") or []:
            runs += 1
            verdict = str(item.get("verdict") or "")
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            linkage = item.get("call_linkage") or {}
            if linkage.get("model"):
                models.add(str(linkage["model"]))
            if linkage.get("prompt_version"):
                prompts.add(str(linkage["prompt_version"]))
            if linkage.get("execution_origin"):
                origins.add(str(linkage["execution_origin"]))
    index: dict[str, Any] = {
        "schema": "tracelab-final-round-experiment-index/1",
        "name": name,
        "dataset_sha256": _sha256(dataset_path),
        "results_sha256": hashlib.sha256(
            json.dumps(results, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "models": sorted(models),
        "prompt_versions": sorted(prompts),
        "execution_origins": sorted(origins),
        "cases": len(results),
        "runs": runs,
        "attempted_runs": runs,
        "total_runs": runs,
        "completed_runs": sum(n for v, n in verdicts.items() if v in {"PASS", "FAIL", "REJECT"}),
        "valid_verdicts": sum(n for v, n in verdicts.items() if v in {"PASS", "FAIL", "REJECT", "NO_RESULT"}),
        "effective_judgments": sum(n for v, n in verdicts.items() if v in {"PASS", "FAIL", "REJECT"}),
        "no_result": verdicts.get("NO_RESULT", 0),
        "no_result_count": verdicts.get("NO_RESULT", 0),
        "invalid_verdicts": sum(n for v, n in verdicts.items() if v not in {"PASS", "FAIL", "REJECT", "NO_RESULT"}),
        "verdicts": verdicts,
        "comparable": runs,
        "notes": notes,
    }
    if receipts is not None:
        statuses: dict[str, int] = {}
        for row in receipts:
            status = JudgeReceipt.from_dict(row).metadata_status
            statuses[status] = statuses.get(status, 0) + 1
        index["receipt_metadata"] = statuses
        index["historical_metadata_unreliable"] = any(
            status != "reliable_v2" for status in statuses
        )
    if labels is not None:
        pairs, pairing = pair_runs(results, labels)
        strict = compute_metrics(pairs, events_by_run=_load_events(dataset_path))
        index["pairing"] = pairing
        index["metrics"] = strict
        index["comparable"] = pairing["matched"]
        index["only_result"] = pairing["only_a"]
        index["only_label"] = pairing["only_b"]
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="生成复赛实验索引")
    parser.add_argument("--name", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--labels", default=None)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--receipts", default=None, help="可选：收据 JSON，用于标记历史元数据")
    parser.add_argument("--out", required=True)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    results = _read(args.results)
    labels = _read(args.labels) if args.labels else None
    receipts = json.loads(Path(args.receipts).read_text(encoding="utf-8")) if args.receipts else None
    index = build_index(args.name, results, labels, args.dataset, args.notes, receipts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.name)
    target = out / f"experiment-index-{safe_name}.json"
    target.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"索引：{target}")
    print(f"cases={index['cases']} runs={index['runs']} comparable={index['comparable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
