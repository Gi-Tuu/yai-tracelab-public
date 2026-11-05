#!/usr/bin/env python3
"""G0 标注 vs 轨迹：检验"朴素基线"能走多远，并提取标签↔步骤映射。"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "official" / "student-1.0"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def main() -> None:
    train = {c["case_id"]: c for c in load_jsonl(DATA / "02_公开训练轨迹.jsonl")}
    labels = {r["case_id"]: r for r in load_jsonl(DATA / "03_公开训练参考标注.jsonl")}
    valid = {c["case_id"]: c for c in load_jsonl(DATA / "04_无标注验证轨迹.jsonl")}

    rows = []
    for cid, lb in labels.items():
        case = train[cid]
        rr = case["reference_result"]
        runs = {r["run_id"]: r for r in case["runs"]}
        for ra in lb["run_assessments"]:
            run = runs[ra["run_id"]]
            ev = {e["event_id"]: e for e in run["events"]}
            faults = ra.get("root_causes", [])
            first_events = [f["first_fault_event_id"] for f in faults if f.get("first_fault_event_id")]
            rows.append(
                {
                    "case": cid,
                    "run": ra["run_id"],
                    "ver": run["agent_version"],
                    "final": run["final_state"],
                    "acceptable": rr["acceptable_snapshot_state"],
                    "inv": run["invoice_created"],
                    "inv_should": rr["invoice_should_exist"],
                    "ok": ra["agent_task_success"],
                    "labels": [f["label"] for f in faults],
                    "first": first_events,
                    "first_steps": [ev[e]["step_id"] for e in first_events if e in ev],
                    "first_status": [ev[e]["step_status"] for e in first_events if e in ev],
                }
            )

    out: list[str] = ["# G0 标注 ↔ 轨迹一致性检验", ""]
    out.append("## 1. 训练集逐 run 对照")
    out.append("")
    out.append("| case | run | final | acceptable | inv | inv_should | 参考ok | 根因标签 | 首错事件(step) |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        marks = " ✅" if r["ok"] else " ❌"
        out.append(
            f"| {r['case']} | {r['run'].replace(r['case']+'-','')} | {r['final']} | {r['acceptable']} | {r['inv']} | {r['inv_should']} "
            f"| {marks} | {', '.join(r['labels']) or '-'} | {'; '.join(f'{e}({s}/{st})' for e, s, st in zip(r['first'], r['first_steps'], r['first_status'], strict=False)) or '-'} |"
        )
    out.append("")

    # ---- 朴素基线 ----
    def acc(pred) -> tuple[int, int, list[str]]:
        hit = 0
        miss = []
        for r in rows:
            if pred(r) == r["ok"]:
                hit += 1
            else:
                miss.append(r["run"])
        return hit, len(rows), miss

    h1 = acc(lambda r: r["final"] == r["acceptable"])
    h2 = acc(lambda r: r["inv"] == r["inv_should"])
    h3 = acc(lambda r: r["final"] == r["acceptable"] and r["inv"] == r["inv_should"])
    h4 = acc(lambda r: True)  # 全判成功
    h5 = acc(lambda r: r["final"] in ("SUCCEEDED", "WAITING_USER"))

    out += [
        "## 2. 朴素基线在训练集上的表现（14 Case / 28 run）",
        "",
        "| 假设 | 命中 | 准确率 | 漏判 run |",
        "|---|---|---|---|",
        f"| H1 `final_state == acceptable_snapshot_state` | {h1[0]}/{h1[1]} | {h1[0]/h1[1]:.1%} | {', '.join(h1[2]) or '无'} |",
        f"| H2 `invoice_created == invoice_should_exist` | {h2[0]}/{h2[1]} | {h2[0]/h2[1]:.1%} | {', '.join(h2[2]) or '无'} |",
        f"| H3 H1∧H2 | {h3[0]}/{h3[1]} | {h3[0]/h3[1]:.1%} | {', '.join(h3[2]) or '无'} |",
        f"| H4 全判 True | {h4[0]}/{h4[1]} | {h4[0]/h4[1]:.1%} | - |",
        f"| H5 `final_state ∈ {{SUCCEEDED, WAITING_USER}}` | {h5[0]}/{h5[1]} | {h5[0]/h5[1]:.1%} | {', '.join(h5[2]) or '无'} |",
        "",
    ]

    # ---- 终态一致性 ----
    bad_state = [(r["run"], r["final"], r["acceptable"]) for r in rows if r["final"] != r["acceptable"]]
    out += [
        "## 3. final_state 与 acceptable_snapshot_state 不一致的 run",
        "",
        "这些正是「终态相等法」必然漏判的样本：",
        "",
    ]
    for run, f, a in bad_state:
        out.append(f"- `{run}`：final=`{f}` vs acceptable=`{a}`")
    out.append("")

    # ---- 标签 → 首错步骤映射 ----
    m: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for lab, st in zip(r["labels"], r["first_steps"] + [None] * len(r["labels"]), strict=False):
            m[lab][str(st)] += 1
    out += ["## 4. 根因标签 → 首个错误事件所在 step_id", "", "| 标签 | 首错步骤 |", "|---|---|"]
    for lab, c in sorted(m.items(), key=lambda kv: -sum(kv[1].values())):
        out.append(f"| `{lab}` | {', '.join(f'{k}×{v}' for k, v in c.most_common())} |")
    out.append("")

    # ---- 首错状态分布 ----
    st_c = Counter(r["first_status"][0] for r in rows if r["first_status"])
    out += ["## 5. 首个错误事件的 step_status 分布", "", f"{dict(st_c)}", ""]

    # ---- 失败 run 的错误码 ----
    err_c = Counter()
    for case in train.values():
        for run in case["runs"]:
            for e in run["events"]:
                if e["error_code"]:
                    err_c[(e["error_code"], e["step_id"])] += 1
    out += ["## 6. 训练集 error_code × step_id", "", "| error_code | step_id | 次数 |", "|---|---|---|"]
    for (code, step), n in err_c.most_common():
        out.append(f"| `{code}` | {step} | {n} |")
    out.append("")

    # ---- 验证集与训练集的场景差异 ----
    out += ["## 7. 验证集没有标注（确认）", ""]
    out.append(f"验证集 {len(valid)} 个 Case，标注文件中出现：{sorted(set(valid) & set(labels)) or '无'}")
    out.append("")
    out.append("验证集 run 终态分布：" + str(Counter(r["final_state"] for c in valid.values() for r in c["runs"])))
    out.append("")

    (ROOT / "docs" / "g0" / "label-baseline.md").write_text("\n".join(out), encoding="utf-8")
    print("wrote docs/g0/label-baseline.md")
    print("\n=== 朴素基线汇总 ===")
    print(f"H1 final==acceptable        : {h1[0]}/{h1[1]} = {h1[0]/h1[1]:.1%}")
    print(f"H2 invoice==should          : {h2[0]}/{h2[1]} = {h2[0]/h2[1]:.1%}")
    print(f"H3 both                     : {h3[0]}/{h3[1]} = {h3[0]/h3[1]:.1%}")
    print(f"H5 final in SEEN/WUSER      : {h5[0]}/{h5[1]} = {h5[0]/h5[1]:.1%}")


if __name__ == "__main__":
    main()
