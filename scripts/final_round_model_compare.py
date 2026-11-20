"""两个 Judge 结果文件的一致性对比（模型间校准，不是官方成绩）。

用法：
    python scripts/final_round_model_compare.py --a <结果A.jsonl> --b <结果B.jsonl> \
        --out <dir> --label-a "qwen3.8-max" --label-b "qwen3.7-max"
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _scan(path: str | Path) -> dict[str, int]:
    total = 0
    seen: set[tuple[str, str]] = set()
    duplicate = 0
    invalid = 0
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        for item in row.get("run_assessments") or []:
            if not isinstance(item, dict) or not isinstance(item.get("run_id"), str):
                invalid += 1
                continue
            total += 1
            key = (str(row.get("case_id")), str(item.get("run_id")))
            if key in seen:
                duplicate += 1
            seen.add(key)
    return {"total": total, "duplicate": duplicate, "invalid": invalid}


def _load(path: str | Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        for item in row.get("run_assessments") or []:
            out[(row["case_id"], item["run_id"])] = item
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge 结果一致性对比")
    parser.add_argument("--a", required=True, help="基准结果（如官方 qwen3.8-max）")
    parser.add_argument("--b", required=True, help="对比结果（如自有 qwen3.7-max）")
    parser.add_argument("--out", required=True)
    parser.add_argument("--label-a", default="A")
    parser.add_argument("--label-b", default="B")
    args = parser.parse_args()

    a = _load(args.a)
    b = _load(args.b)
    keys = sorted(set(a) & set(b))
    scan_a = _scan(args.a)
    scan_b = _scan(args.b)
    coverage = {
        "a_total": len(a),
        "b_total": len(b),
        "matched": len(keys),
        "only_a": len(set(a) - set(b)),
        "only_b": len(set(b) - set(a)),
        "duplicate_a": scan_a["duplicate"],
        "duplicate_b": scan_b["duplicate"],
        "invalid_a": scan_a["invalid"],
        "invalid_b": scan_b["invalid"],
    }
    verdict_hit = 0
    task_hit = task_total = 0
    both_reject_like = 0
    confusion: Counter[tuple[str, str]] = Counter()
    mismatches: list[dict[str, str]] = []
    for key in keys:
        va = str(a[key].get("verdict"))
        vb = str(b[key].get("verdict"))
        confusion[(va, vb)] += 1
        verdict_hit += va == vb
        if va in {"REJECT", "NO_RESULT"} and vb in {"REJECT", "NO_RESULT"}:
            both_reject_like += 1
        ta, tb = a[key].get("agent_task_success"), b[key].get("agent_task_success")
        if ta is not None and tb is not None:
            task_total += 1
            task_hit += bool(ta) == bool(tb)
        if va != vb:
            mismatches.append(
                {
                    "case_id": key[0],
                    "run_id": key[1],
                    args.label_a: va,
                    args.label_b: vb,
                    "reason_a": str(a[key].get("reason") or "")[:180],
                    "reason_b": str(b[key].get("reason") or "")[:180],
                }
            )
    total = len(keys)
    metrics = {
        "runs": total,
        "verdict_agreement": verdict_hit / total if total else None,
        "task_agreement": task_hit / task_total if task_total else None,
        "both_reject_like": both_reject_like,
        "confusion": {f"{x}->{y}": n for (x, y), n in sorted(confusion.items())},
        "mismatches": mismatches,
        "label_a": args.label_a,
        "label_b": args.label_b,
        "coverage": coverage,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    task_line = "—" if metrics["task_agreement"] is None else f"{metrics['task_agreement'] * 100:.1f}%"
    lines = [
        f"# 模型一致性：{args.label_a} vs {args.label_b}",
        "",
        "> 用途：用已跑过基准模型的数据，检查对比模型是否给出相近判定；不是官方成绩。",
        "",
        f"- run 数：{total}（A {coverage['a_total']} / B {coverage['b_total']} / 配对 {coverage['matched']}）",
        f"- 未配对：仅 A {coverage['only_a']} / 仅 B {coverage['only_b']}",
        f"- 重复 Run：A {coverage['duplicate_a']} / B {coverage['duplicate_b']}；非法记录 A {coverage['invalid_a']} / B {coverage['invalid_b']}",
        f"- 判定一致率：{(metrics['verdict_agreement'] or 0) * 100:.1f}%",
        f"- 处理正确性一致率：{task_line}",
        f"- 两侧都拒判（REJECT/NO_RESULT）：{both_reject_like}",
        "",
        "| 基准 \\ 对比 | PASS | FAIL | REJECT | NO_RESULT |",
        "|---|---:|---:|---:|---:|",
    ]
    for base in ("PASS", "FAIL", "REJECT", "NO_RESULT"):
        row = [base]
        for other in ("PASS", "FAIL", "REJECT", "NO_RESULT"):
            row.append(str(confusion.get((base, other), 0)))
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## 不一致逐条", ""]
    if not mismatches:
        lines.append("无。")
    else:
        lines += ["| Case | run | 基准 | 对比 | 基准理由 | 对比理由 |", "|---|---|---|---|---|---|"]
        for item in mismatches:
            lines.append(
                f"| {item['case_id']} | {item['run_id']} | {item[args.label_a]} | {item[args.label_b]} | "
                f"{item['reason_a'].replace('|',' ')} | {item['reason_b'].replace('|',' ')} |"
            )
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{out / 'report.md'}")
    print(f"判定一致率：{(metrics['verdict_agreement'] or 0) * 100:.1f}% （{total} run）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
