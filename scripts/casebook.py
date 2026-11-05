"""生成逐 Case 手册：每个 Case 的题内预期、两版实际表现、根因与首因事件。

用途：
* `docs/casebook.md` —— 进仓库，作为可复核的逐例证据（可随时重新生成）
* 答辩时被点名某个 Case，直接查这张表

只读官方数据 + 跑一遍评测，不修改任何输入。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.adapters import load_dataset  # noqa: E402
from tracelab.evaluation import evaluate_dataset, load_reference_labels  # noqa: E402

DATA = ROOT / "data" / "official" / "student-1.0"

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _guard_out_path(out: Path) -> str | None:
    """`--out` 的最小路径守卫（F-17）：返回拒绝原因，None 表示可以写。

    这个脚本只会"写一个文件"（默认 `docs/casebook.md`），但历史版本对 `--out` 不做
    任何检查：`--out .` 会尝试把目录当文件写、`--out <项目根>/..` 会把文件写到仓库外。
    这里沿用打包脚本的思路：项目根/祖先、主目录/祖先、文件系统根一律拒绝。
    """
    resolved = Path(os.path.realpath(str(out)))
    root = Path(os.path.realpath(str(ROOT)))
    home = Path(os.path.realpath(str(Path.home())))
    if resolved == root:
        return f"--out 不能是项目根目录本身：{resolved}"
    if _is_under(root, resolved):
        return f"--out 不能是项目根目录的祖先：{resolved}"
    if resolved == home or _is_under(home, resolved):
        return f"--out 不能是用户主目录或其祖先：{resolved}"
    if resolved.parent == resolved:
        return f"--out 不能是文件系统根：{resolved}"
    if resolved.is_dir():
        return f"--out 指向的是已存在的目录（需要文件路径）：{resolved}"
    return None


def _short(text: str, limit: int = 46) -> str:
    text = text.replace("|", "／").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _yes(value: Any) -> str:
    return "✅" if value else "❌"


def _runs_table(result) -> list[str]:
    lines = [
        "| Case | 场景 | 题内预期终态 | 发票应存在 | 必需动作 |",
        "|---|---|---|---|---|",
    ]
    for cr in result.results:
        ref = cr.case.reference_result
        lines.append(
            f"| `{cr.case.case_id}` | {cr.case.scenario} | `{ref.get('acceptable_snapshot_state')}` "
            f"| {_yes(ref.get('invoice_should_exist'))} | {ref.get('required_action') or '—'} |"
        )
    return lines


def _detail_table(result, labels: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Case | 基线 A | 候选 B | 方向 | A 根因 / 首因 | B 根因 / 首因 | A 分 | B 分 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cr in result.results:
        def cell(a) -> str:
            mark = "✅正确" if a.agent_task_success else "❌错误"
            state = a.state.run.final_state if a.state else "?"
            return f"{mark}（`{state}`）"

        def causes(a) -> str:
            if not a.violations:
                return "—"
            return "；".join(f"{v.label}@`{v.event_id}`" for v in a.violations)

        lines.append(
            f"| `{cr.case.case_id}` | {cell(cr.base)} | {cell(cr.cand)} | `{cr.comparison.direction}` "
            f"| {causes(cr.base)} | {causes(cr.cand)} | {cr.base.quality_score} | {cr.cand.quality_score} |"
        )
    return lines


def _narratives(result, labels: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """给每个 Case 一句人话解释：错在哪、为什么错。"""
    lines = ["| Case | 一句话解释 |", "|---|---|"]
    for cr in result.results:
        bad = [a for a in cr.assessments if not a.agent_task_success]
        if not bad:
            lines.append(f"| `{cr.case.case_id}` | 两版都处理正确，无问题。 |")
            continue
        parts = []
        for a in bad:
            who = "基线版" if a is cr.base else "候选版"
            first = a.first_error
            detail = first.explanation if first else "—"
            parts.append(f"{who}：{detail}")
        lines.append(f"| `{cr.case.case_id}` | {'　'.join(parts)} |")
    return lines


def _gold_table(labels: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Case | run | 官方判定 | 官方根因 | 首因事件 | 我方是否命中 |",
        "|---|---|---|---|---|---|",
    ]
    for case_id, lb in labels.items():
        for ra in lb.get("run_assessments", []):
            gold = "；".join(
                f"{rc['label']}@{rc.get('first_fault_event_id')}" for rc in ra.get("root_causes", [])
            ) or "无"
            lines.append(
                f"| `{case_id}` | `{ra['run_id']}` | "
                f"{'✅正确' if ra['agent_task_success'] else '❌错误'} | {gold} | — | 见上表 |"
            )
    return lines


def build(cases_path: Path, labels_path: Path | None) -> str:
    dataset = load_dataset(cases_path)
    result = evaluate_dataset(dataset)
    labels = load_reference_labels(labels_path) if labels_path and labels_path.exists() else {}

    lines = [
        f"# 逐 Case 手册：{Path(cases_path).name}",
        "",
        f"> 数据：`{Path(cases_path).name}`（{len(dataset)} Case / "
        f"{sum(len(c.runs) for c in dataset)} 运行，SHA-256 `{dataset.sha256[:16]}…`）",
        "> 由 `scripts/casebook.py` 生成，可随时重跑核对；结论与 `out/<name>/results.json` 一致。",
        "",
        "## 1. 题内预期（来自 reference_result）",
        "",
        *_runs_table(result),
        "",
        "## 2. 两版实际表现",
        "",
        *_detail_table(result, labels),
        "",
        "## 3. 每个 Case 错在哪（人话）",
        "",
        *_narratives(result, labels),
        "",
    ]
    if labels:
        lines += ["## 4. 官方标注对照", "", *_gold_table(labels), ""]

    lines += [
        "## 5. 怎么用这张表答辩",
        "",
        "1. 被点名某个 Case：先看「题内预期」，再说「两版各自终态」，最后说「错在哪个事件」。",
        "2. 强调口径：预期终态是**题内业务目标**，不是对某个版本的专家评分。",
        "3. 若被问「为什么 A 对 B 错」：直接指向第 3 节的根因与首因事件编号，回原始轨迹可查。",
        "4. 若被问「单个 Case 能说明版本好坏吗」：不能，见报告第 3 节的成对比较统计。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成逐 Case 手册")
    parser.add_argument("--cases", default=str(DATA / "02_公开训练轨迹.jsonl"))
    parser.add_argument("--labels", default=str(DATA / "03_公开训练参考标注.jsonl"))
    parser.add_argument("--out", default="docs/casebook.md")
    args = parser.parse_args()

    labels_path = Path(args.labels) if args.labels else None
    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    guard = _guard_out_path(out)
    if guard:
        print(f"错误：{guard}", file=sys.stderr)
        print("   未写入任何文件。", file=sys.stderr)
        return 2
    text = build(Path(args.cases), labels_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


