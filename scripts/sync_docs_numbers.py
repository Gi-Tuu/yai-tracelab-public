#!/usr/bin/env python
"""把「实测数字」同步进文档：`docs/ablation-report.md` + README 的消融表与测试数。

审计 R3-B-05 / R3-B-09 / R3-C-10：文档里的数字是手写常量，已经多次与代码复跑不一致
（规则数 5→6、Macro-F1 58.3%→77.8%、单测数 327→534…），而且没有任何机制能发现漂移。
本脚本以**实际运行结果**为准重写文档段落；`tests/unit/test_docs_consistency.py`
负责在 CI 里检测"文档又落后于代码"。

用法（需要官方训练集，只在私有仓库跑得动）::

    python scripts/sync_docs_numbers.py
    python scripts/sync_docs_numbers.py --readme README.md --report docs/ablation-report.md

脚本不进入公开镜像（构建器白名单未包含它），因此镜像里不会出现官方数据路径。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_CASES = ROOT / "data" / "official" / "student-1.0" / "02_公开训练轨迹.jsonl"
DEFAULT_LABELS = ROOT / "data" / "official" / "student-1.0" / "03_公开训练参考标注.jsonl"
PERCENT_KEYS = (
    ("agent_task_success_accuracy", "任务处理正确率"),
    ("business_invoice_created_accuracy", "发票生成状态准确率"),
    ("version_direction_accuracy", "版本方向准确率"),
    ("first_fault_event_exact_match", "首因事件 Exact Match"),
    ("root_cause_label_macro_f1", "根因标签 Macro-F1"),
    ("evidence_id_validity", "证据 ID 有效性"),
)
HEADER = "| 变体 | 任务正确率 | 可评估运行 | 首因 EM | 版本方向准确率 | 漏判（真值错→判对） | 静默漏报回归 |"


def _pct(value: object) -> str:
    return f"{value:.1%}" if isinstance(value, float) else "—"


def _pytest_count(target: str) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "--collect-only", "-q", "-o", "addopts="],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if not match:
        raise SystemExit(f"无法统计 {target} 的用例数：{proc.stdout[-200:]}{proc.stderr[-200:]}")
    return int(match.group(1))


SHORT_LABELS = {
    "full": "全部 {n} 条规则",
    "terminal_only": "只看终态",
    "generic_health": "通用健康检查",
    "business_only": "领域语义规则",
}


def _ablation_table(outcomes: list, full: object) -> list[str]:
    rows = [HEADER, "|" + "---|" * 7]
    for outcome in outcomes:
        metrics = outcome.metrics
        acc = _pct(metrics.get("agent_task_success_accuracy"))
        runs = metrics.get("runs_scored")
        unknown = metrics.get("runs_unknown") or 0
        runs_text = f"{runs}" if not unknown else f"**{runs}**（另 {unknown} 条 UNKNOWN）"
        first = _pct(metrics.get("first_fault_event_exact_match"))
        direction = _pct(metrics.get("version_direction_accuracy"))
        from tracelab.rules import RULES

        label = SHORT_LABELS.get(outcome.profile.name, outcome.profile.description).format(n=len(RULES))
        missed = len(outcome.false_negatives)
        silent = len(outcome.silent_regressions)
        rows.append(
            f"| `{outcome.profile.name}`（{label}） | {acc} | {runs_text} | "
            f"{first} | {direction} | {missed} | {silent} / 14 |"
        )
    return rows


def _render_report(report: object, dataset: object, cases: Path, labels: Path) -> str:
    outcomes = list(report.outcomes)
    full = outcomes[0]
    generic = next(o for o in outcomes if o.profile.name == "generic_health")
    business = next(o for o in outcomes if o.profile.name == "business_only")
    terminal = next(o for o in outcomes if o.profile.name == "terminal_only")

    def delta(target: object, key: str) -> str:
        a = full.metrics.get(key)
        b = target.metrics.get(key)
        if not isinstance(a, float) or not isinstance(b, float):
            return "—"
        if full.metrics.get("runs_scored") != target.metrics.get("runs_scored"):
            return "—（分母不同，不可比）"
        return f"{(a - b) * 100:+.1f} pp"

    lines = [
        "# 消融实验：领域语义规则到底值多少准确率",
        "",
        "> 本文件由 `scripts/sync_docs_numbers.py` 依据**实际复跑结果**生成，请勿手工改数字。",
        "> 数据：官方公开训练集（`data/official/student-1.0/02_公开训练轨迹.jsonl`，含参考标注）。",
        "> 结论只对这批数据成立，不代表隐藏集泛化能力。",
        "",
        "## 一、变体定义",
        "",
        "| 变体 | 规则集 | 规则数 |",
        "|---|---|---|",
    ]
    for outcome in outcomes:
        count = "全部" if outcome.profile.rule_ids is None and not outcome.profile.terminal_only else (
            "0（不使用规则）" if outcome.profile.terminal_only else len(outcome.profile.rule_ids or ())
        )
        lines.append(f"| `{outcome.profile.name}` | {outcome.profile.description} | {count} |")
    lines += ["", "## 二、指标对比（含分母）", ""]
    table = _ablation_table(outcomes, full)
    lines += table + [""]
    lines += [
        "> 审计 R2-06：不同变体的**可评估运行数**不同（少规则会多出 UNKNOWN），"
        "只给百分比会把\"分母缩小\"误读成\"能力上升\"，因此这里同时列分母。",
        "",
        "## 三、关键结论（相对 `full` 的差距）",
        "",
        "| 对照 | 任务正确率差距 | 首因 Exact Match 差距 |",
        "|---|---|---|",
        f"| `terminal_only` | {delta(terminal, 'agent_task_success_accuracy')} | "
        f"{delta(terminal, 'first_fault_event_exact_match')} |",
        f"| `generic_health` | {delta(generic, 'agent_task_success_accuracy')} | "
        f"{delta(generic, 'first_fault_event_exact_match')} |",
        f"| `business_only` | {delta(business, 'agent_task_success_accuracy')} | "
        f"{delta(business, 'first_fault_event_exact_match')} |",
        "",
        f"- **只看终态**：任务正确率 {_pct(terminal.metrics.get('agent_task_success_accuracy'))}——"
        "它漏掉的样本里终态是「对的」，错在业务语义。",
        f"- **通用健康检查**：{_pct(generic.metrics.get('agent_task_success_accuracy'))}，"
        "说明「Agent 有没有按协议办事」能抓到一部分，但抓不到业务口径。",
        f"- **领域语义规则**：{_pct(business.metrics.get('agent_task_success_accuracy'))}"
        f"（可评估 {business.metrics.get('runs_scored')} 条；另有 "
        f"{business.metrics.get('runs_unknown') or 0} 条判 UNKNOWN 被移出分母），"
        "与通用健康检查**不可直接相减**：分母不同（"
        f"{business.metrics.get('runs_scored')} vs {generic.metrics.get('runs_scored')}），"
        "差值混入「拒判口径变化」，只作方向性参考。",
        "",
        "## 四、错误分解（按真值分类）",
        "",
        "| 变体 | 漏判（真值错、判成对） | 误报（真值对、判成错） |",
        "|---|---|---|",
    ]
    for outcome in outcomes[1:]:
        lines.append(f"| `{outcome.profile.name}` | {len(outcome.false_negatives)} | "
                     f"{len(outcome.false_positives)} |")
    lines += [
        "",
        "## 五、复跑方式",
        "",
        "```bash",
        "python -m tracelab ablate \\",
        "  --cases data/official/student-1.0/02_公开训练轨迹.jsonl \\",
        "  --labels data/official/student-1.0/03_公开训练参考标注.jsonl \\",
        "  --out out/ablation",
        "```",
        "",
        "`out/ablation/ablation.json` 含逐变体的分子/分母/误报/漏报明细；"
        "本文件的表格与它一致（`tests/unit/test_docs_consistency.py` 会在 CI 里校验）。",
        "",
    ]
    return "\n".join(lines)


def _update_readme(readme: Path, report: object) -> None:
    text = readme.read_text(encoding="utf-8")
    outcomes = list(report.outcomes)
    rows = _ablation_table(outcomes, outcomes[0])
    table_re = re.compile(
        r"(\| 变体 \| 任务正确率 \| 可评估运行 \|.*?\n)(\|[^\n]*\n)+", re.S
    )
    match = table_re.search(text)
    if not match:
        raise SystemExit("README 里找不到消融表（请保留表头行）")
    replacement = "\n".join(rows) + "\n"
    text = text[:match.start()] + replacement + text[match.end():]

    unit = _pytest_count("tests/unit")
    total = _pytest_count("tests")
    text = re.sub(r"(python -m pytest tests/unit -q\s+#\s*)\d+( 项)", rf"\g<1>{unit}\g<2>", text, count=1)
    text = re.sub(r"(python -m pytest tests -q\s+#\s*)\d+( 项)", rf"\g<1>{total}\g<2>", text, count=1)
    # 审计 R4-11：README「目录」块里也有手写计数，必须一起同步（否则永远停旧值）。
    integration = total - unit
    text = re.sub(r"(tests/unit/\s+)\d+( 项)", rf"\g<1>{unit}\g<2>", text)
    text = re.sub(r"(tests/integration/\s+)\d+( 项)", rf"\g<1>{integration}\g<2>", text)
    readme.write_text(text, encoding="utf-8")
    print(f"README 已同步：消融表 {len(outcomes)} 行；单测 {unit} 项；全量 {total} 项")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把实测数字同步进文档（消融报告 + README）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--readme", default=str(ROOT / "README.md"))
    parser.add_argument("--report", default=str(ROOT / "docs" / "ablation-report.md"))
    args = parser.parse_args(argv)

    cases, labels_path = Path(args.cases), Path(args.labels)
    if not cases.is_file() or not labels_path.is_file():
        print(
            "错误：同步脚本需要官方训练集与参考标注（私有仓库才有）。"
            f"缺少：{[str(p) for p in (cases, labels_path) if not p.is_file()]}",
            file=sys.stderr,
        )
        return 2

    from tracelab.ablation import run_ablation
    from tracelab.adapters import load_dataset
    from tracelab.evaluation import load_reference_labels

    dataset = load_dataset(cases)
    labels = load_reference_labels(labels_path)
    report = run_ablation(dataset, labels)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(report, dataset, cases, labels_path), encoding="utf-8")
    print(f"已重写 {report_path}")

    _update_readme(Path(args.readme), report)
    print("完成：文档数字已与实测一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
