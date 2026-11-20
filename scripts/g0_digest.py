#!/usr/bin/env python3
"""把轨迹压缩成人类可读的事件摘要，供规则设计使用。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "official" / "student-1.0"

KEEP_IN = ("user_request", "buyer_id", "item_id", "invoice_task_type", "amount_value", "amount_basis",
           "amounts", "line_count", "line_items", "original_invoice_id", "user_confirmed",
           "missing_fields", "idempotency_key", "callback_status", "request_id")


def short(obj, limit=260) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, dict):
        picked = {k: v for k, v in obj.items() if k in KEEP_IN or not isinstance(v, (dict, list))}
        s = json.dumps(picked, ensure_ascii=False)
        return s if len(s) <= limit else s[:limit] + "…"
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "train"
    files = {
        "train": ("02_公开训练轨迹.jsonl", "trace-digest-train.md"),
        "valid": ("04_无标注验证轨迹.jsonl", "trace-digest-valid.md"),
    }
    src, outname = files[which]
    labels = {}
    if which == "train":
        labels = {r["case_id"]: r for r in load_jsonl(DATA / "03_公开训练参考标注.jsonl")}

    lines: list[str] = [f"# 轨迹摘要（{which}）", ""]
    for case in load_jsonl(DATA / src):
        cid = case["case_id"]
        rr = case["reference_result"]
        lines.append(f"## {cid} · {case.get('scenario','')}")
        lines.append("")
        lines.append(f"- 用户请求：{case['user_request']}")
        lines.append(f"- context：`{json.dumps(case['context'], ensure_ascii=False)}`")
        lines.append(f"- reference_result：`{json.dumps(rr, ensure_ascii=False)}`")
        if cid in labels:
            for ra in labels[cid]["run_assessments"]:
                rc = "; ".join(f"{x['label']}@{x.get('first_fault_event_id')}" for x in ra.get("root_causes", []))
                lines.append(
                    f"- **参考标注 {ra['run_id']}**：ok={ra['agent_task_success']} invoice={ra['business_invoice_created']} 根因=[{rc or '无'}]"
                )
        lines.append("")
        for run in case["runs"]:
            lines.append(
                f"### {run['run_id']} ({run['agent_version']}) → final={run['final_state']} invoice_created={run['invoice_created']} terminal={run['workflow_terminal']}"
            )
            lines.append("")
            for e in run["events"]:
                bits = [f"`{e['seq']}` **{e['step_id']}** {e['step_status']} {e['result_code']}"]
                if e.get("error_code"):
                    bits.append(f"err={e['error_code']}")
                if e.get("retry_count"):
                    bits.append(f"retry={e['retry_count']}")
                line = " | ".join(bits)
                lines.append(f"- {line}")
                if e.get("observed_input"):
                    lines.append(f"    - in: {short(e['observed_input'])}")
                if e.get("observed_output"):
                    lines.append(f"    - out: {short(e['observed_output'])}")
                for t in e.get("tool_calls") or []:
                    att = f"#{t.get('attempt')}" if t.get("attempt") else ""
                    lines.append(f"    - tool {t['name']}{att}: args={short(t.get('arguments'),160)} → {short(t.get('result'),200)}")
            lines.append("")

    out = ROOT / "docs" / "g0" / outname
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
