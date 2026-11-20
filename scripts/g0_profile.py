#!/usr/bin/env python3
"""G0 资料包结构剖析：只读，不改原始数据。产出 docs/g0/data-profile.md。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "official" / "student-1.0"
OUT = ROOT / "docs" / "g0"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def keys_of(objs: list[dict]) -> Counter:
    c: Counter = Counter()
    for o in objs:
        if isinstance(o, dict):
            c.update(o.keys())
    return c


def fmt_counter(c: Counter) -> str:
    return ", ".join(f"`{k}`×{v}" for k, v in c.most_common())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    train = load_jsonl(DATA / "02_公开训练轨迹.jsonl")
    labels = {r["case_id"]: r for r in load_jsonl(DATA / "03_公开训练参考标注.jsonl")}
    valid = load_jsonl(DATA / "04_无标注验证轨迹.jsonl")
    catalog = json.loads((DATA / "09_模拟客户与商品档案.json").read_text(encoding="utf-8"))

    lines: list[str] = ["# 官方资料包结构剖析（G0 实测）", ""]
    lines.append("数据目录：`data/official/student-1.0/`　资料包版本 student-1.0")
    lines.append(f"训练 Case {len(train)} 条 / 参考标注 {len(labels)} 条 / 验证 Case {len(valid)} 条")
    lines.append("")

    # ---- 字段覆盖 ----
    all_cases = train + valid
    case_keys = keys_of(all_cases)
    runs = [r for c in all_cases for r in c["runs"]]
    run_keys = keys_of(runs)
    events = [e for r in runs for e in r["events"]]
    event_keys = keys_of(events)
    tools = [t for e in events for t in (e.get("tool_calls") or [])]
    tool_keys = keys_of(tools)

    lines += [
        "## 1. 字段覆盖（实测）",
        "",
        f"**Case 级**（共 {len(all_cases)} 条）：{fmt_counter(case_keys)}",
        "",
        f"**Run 级**（共 {len(runs)} 条）：{fmt_counter(run_keys)}",
        "",
        f"**Event 级**（共 {len(events)} 条）：{fmt_counter(event_keys)}",
        "",
        f"**ToolCall 级**（共 {len(tools)} 条）：{fmt_counter(tool_keys)}",
        "",
    ]

    # ---- 枚举分布 ----
    lines += ["## 2. 枚举值分布", ""]
    for field, objs in (
        ("step_id", events),
        ("step_status", events),
        ("module", events),
        ("result_code", events),
        ("error_code", events),
    ):
        c = Counter(str(o.get(field)) for o in objs)
        lines.append(f"- **{field}**：{fmt_counter(c)}")
    lines.append(f"- **tool name**：{fmt_counter(Counter(str(t.get('name') or t.get('tool') or t.get('tool_name')) for t in tools))}")
    lines.append("")

    # ---- 每 Case 概览 ----
    lines += [
        "## 3. 逐 Case 概览",
        "",
        "| case_id | scenario | runs | A 终态 | A 发票 | B 终态 | B 发票 | A 事件数 | B 事件数 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in all_cases:
        rs = c["runs"]
        cells = []
        for r in rs[:2]:
            cells += [str(r.get("final_state")), str(r.get("invoice_created"))]
        while len(cells) < 4:
            cells += ["-", "-"]
        lines.append(
            f"| {c['case_id']} | {c.get('scenario','')} | {len(rs)} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} "
            f"| {len(rs[0]['events'])} | {len(rs[1]['events']) if len(rs) > 1 else '-'} |"
        )
    lines.append("")

    # ---- 参考标注结构 ----
    lines += ["## 4. 参考标注结构（03 文件）", ""]
    if labels:
        sample = next(iter(labels.values()))
        lines.append("单条标注顶层字段：" + fmt_counter(keys_of(list(labels.values()))))
        lines.append("")
        lines.append("单条标注完整结构（脱敏到字段级）：")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(sample, ensure_ascii=False, indent=2)[:3000])
        lines.append("```")
        lines.append("")
        # 标注里的布尔结论分布
        ta = Counter()
        ib = Counter()
        dirs = Counter()
        rootc = Counter()
        for lb in labels.values():
            for ra in lb.get("run_assessments", []):
                ta[str(ra.get("agent_task_success"))] += 1
                ib[str(ra.get("business_invoice_created"))] += 1
                for rc in ra.get("root_causes", []):
                    rootc[str(rc.get("label"))] += 1
            dirs[str(lb.get("version_comparison", {}).get("direction"))] += 1
        lines.append(f"- agent_task_success 分布：{fmt_counter(ta)}")
        lines.append(f"- business_invoice_created 分布：{fmt_counter(ib)}")
        lines.append(f"- version direction 分布：{fmt_counter(dirs)}")
        lines.append(f"- 根因标签频次：{fmt_counter(rootc)}")
        lines.append("")

    # ---- reference_result 结构 ----
    lines += ["## 5. reference_result / context 结构", ""]
    rr_keys = keys_of([c.get("reference_result", {}) for c in all_cases])
    ctx_keys = keys_of([c.get("context", {}) for c in all_cases])
    lines.append(f"- `reference_result` 字段：{fmt_counter(rr_keys)}")
    lines.append(f"- `context` 字段：{fmt_counter(ctx_keys)}")
    lines.append("")
    lines.append("<details><summary>各 Case 的 reference_result</summary>")
    lines.append("")
    for c in all_cases:
        lines.append(f"- `{c['case_id']}`：`{json.dumps(c.get('reference_result'), ensure_ascii=False)}`")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # ---- 档案文件 ----
    lines += ["## 6. 模拟档案（09）", ""]
    lines.append(f"顶层键：{list(catalog.keys())}")
    for k, v in catalog.items():
        if isinstance(v, list):
            lines.append(f"- `{k}`：{len(v)} 条，首条字段 {list(v[0].keys()) if v and isinstance(v[0], dict) else type(v[0]).__name__}")
        elif isinstance(v, dict):
            lines.append(f"- `{k}`：dict，键 {list(v.keys())[:10]}")
        else:
            lines.append(f"- `{k}`：{v!r}")
    lines.append("")

    # ---- 事件序列形态 ----
    lines += ["## 7. 事件序列形态", ""]
    seq_ok = all([e["seq"] for e in r["events"]] == sorted(e["seq"] for e in r["events"]) for r in runs)
    dup_ids = [r["run_id"] for r in runs if len({e["event_id"] for e in r["events"]}) != len(r["events"])]
    gaps = [r["run_id"] for r in runs if [e["seq"] for e in r["events"]] != list(range(len(r["events"])))]
    ev_at = [e for e in events if e.get("retry_count", 0) > 0]
    multi_attempt = [e for e in events if (e.get("tool_calls") or []) and max((t.get("attempt", 1) for t in e["tool_calls"]), default=1) > 1]
    lines.append(f"- `seq` 单调递增：{'是' if seq_ok else '**否**'}")
    lines.append(f"- `seq` 从 0 连续：{'是' if not gaps else f'否，异常 run：{gaps[:5]}'}")
    lines.append(f"- `event_id` 在 run 内唯一：{'是' if not dup_ids else f'否，{dup_ids[:5]}'}")
    lines.append(f"- 含 `retry_count>0` 的事件：{len(ev_at)} 个，分布 {fmt_counter(Counter(str(e.get('retry_count')) for e in ev_at))}")
    lines.append(f"- 单事件含多次 tool attempt 的事件：{len(multi_attempt)} 个")
    lines.append(f"- 单 run 事件数：min={min(len(r['events']) for r in runs)} max={max(len(r['events']) for r in runs)}")
    lines.append("")

    report = OUT / "data-profile.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {report}")

    # 原始样例整档导出，便于逐字段核对
    inv = next(c for c in train if c["case_id"] == "INV-T01")
    (OUT / "sample-case-INV-T01.json").write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    if "INV-T01" in labels:
        (OUT / "sample-label-INV-T01.json").write_text(json.dumps(labels["INV-T01"], ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote samples")


if __name__ == "__main__":
    main()
