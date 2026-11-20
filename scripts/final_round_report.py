"""复赛公开集自评报告（团队自选指标；不读取隐藏集/不把公开集当泛化）。

用法：
    python scripts/final_round_report.py --results <results.jsonl> --labels <label.jsonl> \
        --out <dir> [--cases <case.jsonl>] [--title "AI 顾问 public 100"]

输出 metrics.json + report.md（含混淆矩阵、逐 run 分歧、NO_RESULT 原因、按域统计）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from pathlib import Path as _Path

ROOT = _Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from typing import Any  # noqa: E402

from tracelab.final_round.metrics import compute_metrics, pair_runs, strict_bool  # noqa: E402
from tracelab.final_round.receipts import JudgeReceipt  # noqa: E402


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_labels(path: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    labels: dict[str, dict[str, dict[str, Any]]] = {}
    for row in _read_jsonl(path):
        labels[row["case_id"]] = {item["run_id"]: item for item in row["run_assessments"]}
    return labels


def _load_events(path: str | Path | None) -> dict[str, set[str]]:
    if not path:
        return {}
    events: dict[str, set[str]] = {}
    for row in _read_jsonl(path):
        for run in row.get("runs") or []:
            run_id = run.get("run_id")
            if isinstance(run_id, str):
                events[(str(row.get("case_id")), run_id)] = {
                    str(event.get("event_id"))
                    for event in run.get("events") or []
                    if event.get("event_id")
                }
    return events


def _load_domains(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    return {row["case_id"]: str(row.get("domain")) for row in _read_jsonl(path)}


def compute(results: list[dict[str, Any]], labels: dict[str, dict[str, dict[str, Any]]], domains: dict[str, str]) -> dict[str, Any]:
    verdict_total = verdict_hit = 0
    task_total = task_hit = 0
    business_total = business_hit = 0
    first_total = first_hit = 0
    both_null = 0
    first_expected = first_located = first_exact = 0
    confusion: Counter[tuple[str, str]] = Counter()
    verdict_dist: Counter[str] = Counter()
    gold_dist: Counter[str] = Counter()
    no_result_reasons: list[dict[str, str]] = []
    mismatches: list[dict[str, Any]] = []
    by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    for result in results:
        case_id = result["case_id"]
        domain = domains.get(case_id, result.get("domain") or "unknown")
        for item in result.get("run_assessments") or []:
            gold = labels.get(case_id, {}).get(item.get("run_id"))
            if gold is None:
                continue
            verdict = str(item.get("verdict"))
            gold_verdict = str(gold.get("verdict"))
            verdict_dist[verdict] += 1
            gold_dist[gold_verdict] += 1
            confusion[(gold_verdict, verdict)] += 1
            verdict_total += 1
            verdict_hit += verdict == gold_verdict
            by_domain[domain]["total"] += 1
            by_domain[domain]["hit"] += verdict == gold_verdict
            ours_task = strict_bool(item.get("agent_task_success"))
            gold_task = strict_bool(gold.get("agent_task_success"))
            if ours_task is not None and gold_task is not None:
                task_total += 1
                task_hit += ours_task == gold_task
            ours_business = item.get("business_invoice_created")
            gold_business = gold.get("business_invoice_created")
            if ours_business is None and result.get("business_type") == "ai_consultant":
                ours_business = item.get("business_result_correct")
                gold_business = gold.get("business_result_correct")
            ours_business = strict_bool(ours_business)
            gold_business = strict_bool(gold_business)
            if ours_business is not None and gold_business is not None:
                business_total += 1
                business_hit += ours_business == gold_business
            ours_first = item.get("first_fault_event_id")
            gold_first = gold.get("first_fault_event_id")
            if ours_first is not None or gold_first is not None:
                first_total += 1
                first_hit += str(ours_first) == str(gold_first)
            if gold_first is not None:
                first_expected += 1
                if ours_first is not None:
                    first_located += 1
                    if str(ours_first) == str(gold_first):
                        first_exact += 1
            if ours_first is None and gold_first is None:
                both_null += 1
            if verdict == "NO_RESULT":
                no_result_reasons.append(
                    {"case_id": case_id, "run_id": str(item.get("run_id")), "reason": str(item.get("reason") or "")[:300]}
                )
            if verdict != gold_verdict or (
                item.get("agent_task_success") is not None
                and gold.get("agent_task_success") is not None
                and strict_bool(item["agent_task_success"]) is not None
                and strict_bool(gold["agent_task_success"]) is not None
                and strict_bool(item["agent_task_success"]) != strict_bool(gold["agent_task_success"])
            ):
                mismatches.append(
                    {
                        "case_id": case_id,
                        "run_id": item.get("run_id"),
                        "judge_verdict": verdict,
                        "gold_verdict": gold_verdict,
                        "judge_task": item.get("agent_task_success"),
                        "gold_task": gold.get("agent_task_success"),
                        "reason": str(item.get("reason") or "")[:300],
                        "gold_explanation": str(gold.get("explanation") or "")[:200],
                    }
                )
    valid_verdicts = sum(count for verdict, count in verdict_dist.items() if verdict in {"PASS", "FAIL", "REJECT", "NO_RESULT"})
    effective = sum(count for verdict, count in verdict_dist.items() if verdict in {"PASS", "FAIL", "REJECT"})
    no_result = verdict_dist.get("NO_RESULT", 0)
    invalid = sum(count for verdict, count in verdict_dist.items() if verdict not in {"PASS", "FAIL", "REJECT", "NO_RESULT"})
    return {
        "runs": verdict_total,
        "total_runs": verdict_total,
        "valid_verdicts": valid_verdicts,
        "effective_judgments": effective,
        "no_result_count": no_result,
        "invalid_verdicts": invalid,
        "first_fault_expected": first_expected,
        "first_fault_located": first_located,
        "first_fault_exact_match": first_exact,
        "first_fault_both_null": both_null,
        "verdict_agreement": verdict_hit / verdict_total if verdict_total else None,
        "task_agreement": task_hit / task_total if task_total else None,
        "business_agreement": business_hit / business_total if business_total else None,
        "first_fault_agreement": first_hit / first_total if first_total else None,
        "confusion": {f"{gold}->{judge}": count for (gold, judge), count in sorted(confusion.items())},
        "judge_distribution": dict(verdict_dist),
        "gold_distribution": dict(gold_dist),
        "no_result": no_result_reasons,
        "mismatches": mismatches,
        "by_domain": {domain: {"total": c["total"], "hit": c["hit"], "agreement": c["hit"] / c["total"] if c["total"] else None} for domain, c in by_domain.items()},
    }


def render(title: str, metrics: dict[str, Any], extra_note: str = "") -> str:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:.1f}%"

    lines = [
        f"# {title}",
        "",
        "> 本报告是**公开集自评**，不是隐藏集成绩，也不代表泛化能力；结论必须与局限一起读。",
        "> 指标由团队自选并说明口径；公开测试冻结后不用于回调 prompt。",
        "",
    ]
    if extra_note:
        lines += [extra_note, ""]
    pairing = metrics.get("pairing") or {}
    if pairing:
        lines += [
            "## 0. 配对覆盖",
            "",
            "| 项 | A（结果） | B（专家） |",
            "|---|---:|---:|",
            f"| Case | {pairing.get('a_total_cases', 0)} | {pairing.get('b_total_cases', 0)} |",
            f"| Run | {pairing.get('a_total_runs', 0)} | {pairing.get('b_total_runs', 0)} |",
            "",
            f"- 成功配对：**{pairing.get('matched', 0)}**；仅 A：{pairing.get('only_a', 0)}；仅 B：{pairing.get('only_b', 0)}",
            f"- 重复 Run：A {len(pairing.get('duplicate_a', []))}；B {len(pairing.get('duplicate_b', []))}",
            f"- 非法记录：A {pairing.get('invalid_a', 0)}；B {pairing.get('invalid_b', 0)}",
            "",
        ]
    lines += [
        "## 1. 总览",
        "",
        "| 指标 | 数值 | 分母 |",
        "|---|---:|---:|",
        f"| 判定一致率 | {pct(metrics['verdict_agreement'])} | {metrics['runs']} run |",
        f"| 处理正确性一致率 | {pct(metrics['task_agreement'])} | 仅两侧均有布尔值的 run |",
        f"| 业务结果一致率 | {pct(metrics['business_agreement'])} | 仅两侧均有布尔值的 run |",
        f"| 首错事件一致率 | {pct(metrics['first_fault_agreement'])} | 任一侧给出首错的 run |",
        f"| 首错双 null | {metrics['first_fault_both_null']} | — |",
        f"| 有效判定 / 总 Run | {metrics.get('effective_judgments', 0)} / {metrics.get('total_runs', 0)} | — |",
        f"| NO_RESULT / 非法判定 | {metrics.get('no_result_count', 0)} / {metrics.get('invalid_verdicts', 0)} | — |",
        "",
        "## 2. 分布与混淆矩阵",
        "",
        f"- Judge 分布：`{metrics['judge_distribution']}`",
        f"- 专家分布：`{metrics['gold_distribution']}`",
        "",
        "| 专家 \\ Judge | PASS | FAIL | REJECT | NO_RESULT |",
        "|---|---:|---:|---:|---:|",
    ]
    for gold in ("PASS", "FAIL", "REJECT"):
        row = [gold]
        for judge in ("PASS", "FAIL", "REJECT", "NO_RESULT"):
            row.append(str(metrics["confusion"].get(f"{gold}->{judge}", 0)))
        lines.append("| " + " | ".join(row) + " |")
    strict = metrics.get("strict_validity") or {}
    if strict:
        va = strict.get("verdict_agreement") or {}
        ta = strict.get("task_agreement") or {}
        ba = strict.get("business_agreement") or {}
        ff = strict.get("first_fault") or {}
        ev = strict.get("evidence_reference_validity") or {}
        lines += [
            "",
            "## 2b. 有效性与分母（严格配对）",
            "",
            "| 指标 | 分子 | 分母 | 数值 |",
            "|---|---:|---:|---:|",
            f"| 有效四态判定 | {strict.get('effective_judgments', 0)} | {strict.get('runs', 0)} | - |",
            f"| 判定一致 | {va.get('hit', 0)} | {va.get('total', 0)} | {pct(va.get('value'))} |",
            f"| 处理正确性一致 | {ta.get('hit', 0)} | {ta.get('total', 0)} | {pct(ta.get('value'))} |",
            f"| 业务结果一致 | {ba.get('hit', 0)} | {ba.get('total', 0)} | {pct(ba.get('value'))} |",
            f"| 首错精确匹配 | {ff.get('hit', 0)} | {ff.get('total', 0)} | {pct(ff.get('value'))} |",
            f"| 首错覆盖率 | {ff.get('total', 0)} | {strict.get('runs', 0)} | {pct(ff.get('coverage'))} |",
            f"| 证据引用有效 | {ev.get('valid', 0)} | {ev.get('total', 0)} | {pct(ev.get('value'))} |",
            "",
            f"- 非法标注布尔：{strict.get('invalid_label_values', 0)}；Judge 分布：`{strict.get('judge_distribution', {})}`",
            "",
        ]
    lines += ["", "## 3. 按业务域", "", "| 域 | 一致率 | run |", "|---|---:|---:|"]
    for domain, item in sorted(metrics["by_domain"].items()):
        lines.append(f"| {domain} | {pct(item['agreement'])} | {item['total']} |")
    lines += ["", "## 4. 分歧逐条", ""]
    if not metrics["mismatches"]:
        lines.append("无。")
    else:
        lines += ["| Case | run | Judge | 专家 | Judge 理由 |", "|---|---|---|---|---|"]
        for item in metrics["mismatches"]:
            lines.append(
                f"| {item['case_id']} | {item['run_id']} | {item['judge_verdict']} | {item['gold_verdict']} | "
                f"{item['reason'].replace('|', ' ')[:160]} |"
            )
    lines += ["", "## 5. NO_RESULT", ""]
    if not metrics["no_result"]:
        lines.append("无。")
    else:
        for item in metrics["no_result"]:
            lines.append(f"- `{item['case_id']}` / `{item['run_id']}`：{item['reason']}")
    if metrics.get("receipt_metadata"):
        lines += [
            "",
            "## 5b. 收据元数据",
            "",
            f"- 状态分布：`{metrics['receipt_metadata']}`",
            f"- 历史元数据不可靠：{metrics.get('historical_metadata_unreliable')}",
            "- 旧 v0/v1-ext 收据可验证，但 per-call Token/Prompt 不能当真实用量；需要新执行记录请重跑。",
        ]
    lines += [
        "",
        "## 6. 局限与口径",
        "",
        "- 公开集参考标注为专家复核的合成样本，不代表真实线上分布；",
        "- 判定一致率按 exact verdict 计算，REJECT/NO_RESULT 与 FAIL 不互相折算；",
        "- 首错一致率只在任一侧给出首错的 run 上计算，双 null 单列；",
        "- 公开集已用于冻结后的一次性自评；若据此再调 prompt，该批应转开发用途，需新批次验证。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="复赛公开集自评报告")
    parser.add_argument("--results", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cases", default=None, help="可选：Case JSONL，用于按 domain 统计")
    parser.add_argument("--title", default="复赛公开集自评")
    parser.add_argument("--note", default="")
    parser.add_argument("--receipts", default=None, help="可选：收据 JSON，生成历史元数据摘要")
    args = parser.parse_args()

    results = _read_jsonl(args.results)
    label_rows = _read_jsonl(args.labels)
    labels = _load_labels(args.labels)
    metrics = compute(results, labels, _load_domains(args.cases))
    pairs, pairing = pair_runs(results, label_rows)
    metrics["pairing"] = pairing
    strict = compute_metrics(pairs, events_by_run=_load_events(args.cases))
    metrics["strict_validity"] = strict
    for key in (
        "total_runs", "valid_verdicts", "effective_judgments", "no_result",
        "invalid_verdicts", "first_fault_expected", "first_fault_located",
        "first_fault_exact_match", "first_fault_both_null",
    ):
        metrics[key] = strict[key]
    if args.receipts:
        receipt_rows = json.loads(Path(args.receipts).read_text(encoding="utf-8"))
        statuses: dict[str, int] = {}
        for row in receipt_rows:
            status = JudgeReceipt.from_dict(row).metadata_status
            statuses[status] = statuses.get(status, 0) + 1
        metrics["receipt_metadata"] = statuses
        metrics["historical_metadata_unreliable"] = any(
            status != "reliable_v2" for status in statuses
        )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render(args.title, metrics, args.note), encoding="utf-8")
    print(f"报告：{out / 'report.md'}")
    print(f"判定一致率：{metrics['verdict_agreement']:.4f} （{metrics['runs']} run）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
