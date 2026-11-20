"""对比两个模型在同一批专项 control 上的判定（模型一致性校准）。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _scan(path: str | Path) -> dict[str, int]:
    total = 0
    seen: set[tuple[str, str, int]] = set()
    duplicate = 0
    invalid = 0
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        for side in ("baseline", "variant"):
            for item in row.get(side) or []:
                if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                    invalid += 1
                    continue
                total += 1
                key = (str(row.get("control_id")), side, item["index"])
                if key in seen:
                    duplicate += 1
                seen.add(key)
    return {"total": total, "duplicate": duplicate, "invalid": invalid}


def _load(path: str | Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for side in ("baseline", "variant"):
            for item in row.get(side) or []:
                out[(row["control_id"], side, int(item["index"]))] = {
                    "verdict": item.get("verdict"),
                    "reason": item.get("reason") or "",
                    "expected": row.get("expected_boundary") or "",
                    "control_type": row.get("control_type"),
                }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="专项 control 模型一致性对比")
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
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
    confusion: Counter[tuple[str, str]] = Counter()
    hit = 0
    mismatches: list[dict[str, Any]] = []
    for key in keys:
        va, vb = str(a[key]["verdict"]), str(b[key]["verdict"])
        confusion[(va, vb)] += 1
        hit += va == vb
        if va != vb:
            mismatches.append(
                {
                    "key": key,
                    "type": a[key]["control_type"],
                    "expected": a[key]["expected"][:160],
                    "a": va,
                    "b": vb,
                    "reason_a": a[key]["reason"][:150],
                    "reason_b": b[key]["reason"][:150],
                }
            )
    total = len(keys)
    metrics = {
        "inputs": total,
        "verdict_agreement": hit / total if total else None,
        "confusion": {f"{x}->{y}": n for (x, y), n in sorted(confusion.items())},
        "mismatches": mismatches,
        "coverage": coverage,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# 专项 control 模型一致性：{args.label_a} vs {args.label_b}",
        "",
        f"- 共同输入：{total}（A {coverage['a_total']} / B {coverage['b_total']}）",
        f"- 未配对：仅 A {coverage['only_a']} / 仅 B {coverage['only_b']}",
        f"- 重复输入：A {coverage['duplicate_a']} / B {coverage['duplicate_b']}；非法记录 A {coverage['invalid_a']} / B {coverage['invalid_b']}",
        f"- 判定一致率：{(metrics['verdict_agreement'] or 0) * 100:.1f}%",
        "",
        "| 基准 \\ 对比 | PASS | FAIL | REJECT | NO_RESULT |",
        "|---|---:|---:|---:|---:|",
    ]
    for base in ("PASS", "FAIL", "REJECT", "NO_RESULT"):
        lines.append(
            "| "
            + base
            + " | "
            + " | ".join(str(confusion.get((base, other), 0)) for other in ("PASS", "FAIL", "REJECT", "NO_RESULT"))
            + " |"
        )
    lines += ["", "## 不一致逐条", ""]
    if not mismatches:
        lines.append("无。")
    else:
        lines += ["| control | side/index | 类型 | 基准 | 对比 | expected |", "|---|---|---|---|---|---|"]
        for item in mismatches:
            cid, side, idx = item["key"]
            lines.append(
                f"| {cid} | {side}/{idx} | {item['type']} | {item['a']} | {item['b']} | {item['expected'].replace('|',' ')} |"
            )
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{out / 'report.md'}")
    print(f"一致率：{(metrics['verdict_agreement'] or 0) * 100:.1f}%（{total} 输入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
