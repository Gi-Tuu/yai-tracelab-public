"""TraceLab 命令行入口。

    python -m tracelab evaluate --cases <jsonl> --out <dir> [--labels <jsonl>] [--adapter official]

只做三件事：加载（adapter）→ 评测（rules + attribution）→ 产出（submission/json/report）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapters import get_adapter
from .evaluation import (
    LabelFormatError,
    evaluate_dataset,
    load_reference_labels,
    score_against_labels,
    summarise,
    write_results_json,
    write_submission,
)
from .official_report import write_official_report
from .reports import AUDIENCE_BUILDERS, write_audience_reports, write_markdown
from .secondary import compute_all


def _incomplete_reasons(result, dataset) -> list[str]:
    """C-07：哪些事实让本批不足以作为"完整评测"证据。

    只认三类触发条件（与 Lead 的要求一致）：
    A 读取不完整 / C 评测不完整（含 0-run Case 与降级） / 数据存在完整性错误。
    """
    integrity = dict((getattr(result, "manifest", None) or {}).get("integrity") or {})
    reasons: list[str] = []
    if integrity.get("read_complete") is False:
        reasons.append("A. read_complete=false：文件未读全（坏行 / 编码 / 截断）")
    if integrity.get("evaluation_complete") is False:
        detail = []
        if integrity.get("degraded_runs"):
            detail.append(f"降级 {integrity['degraded_runs']} 条")
        if integrity.get("empty_cases"):
            detail.append(f"无可评估运行的 Case {integrity['empty_cases']} 个")
        if integrity.get("unknown_runs"):
            detail.append(f"UNKNOWN {integrity['unknown_runs']} 条")
        suffix = f"（{'、'.join(detail)}）" if detail else ""
        reasons.append(f"C. evaluation_complete=false：存在未完成评测的运行{suffix}")
    if dataset.errors:
        codes = sorted({i.code for i in dataset.errors})
        reasons.append(f"数据完整性错误 {len(dataset.errors)} 项（{'、'.join(codes)}）")
    return reasons


def _adapter_or_none(name: str):
    """按名字取适配器；未知名字返回 (None, 错误信息)。

    审计 D-12：旧实现直接 `get_adapter()`，未知 adapter 抛 KeyError 栈回溯、
    退出码 1（契约里 2 = 输入错误），调用方无法区分"输入错"与"检查未过"。
    """
    try:
        return get_adapter(name), None
    except KeyError as exc:
        return None, str(exc)


def _require_file(path: Path, what: str) -> str | None:
    """输入文件预检：不存在或不是文件都给出可读错误（审计 R2-11）。"""
    if not path.exists():
        return f"找不到 {what}：{path}"
    if not path.is_file():
        return f"{what} 不是文件：{path}"
    return None


def _prepare_out_dir(path: Path) -> str | None:
    """输出目录预检：路径已存在但不是目录时给出可读错误（而不是 FileExistsError 栈）。"""
    if path.exists() and not path.is_dir():
        return f"输出路径已存在且不是目录：{path}"
    return None


#: evaluate 会产出的文件名（清理与 run-manifest 共用）
_EVALUATE_PRODUCTS: tuple[str, ...] = (
    "submission.jsonl", "results.json", "metrics.json", "report.md",
    "report-official.md", "report-business.md", "report-engineering.md", "report-product.md",
)
#: 上次运行写出、本次用来判断归属的清单
_OUTPUT_MANIFEST = ".tracelab-manifest.json"


def _looks_like_own_output(path: Path) -> bool:
    """内容签名：判断一个同名文件是否由本工具生成（审计 R3-04 / F5-05）。

    审计 F5-05：旧签名只覆盖部分产物；首次运行在写 run-manifest 前中断后，重跑会
    把自家 submission.jsonl / 三份受众报告误报为"外来文件"。这里按文件名给出
    结构签名，既覆盖全部产物，也不会把用户手写的同名文件当成产物。
    """
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:800]
    except OSError:
        return False
    if any(marker in head for marker in ("tracelab_evaluator_version", "YAI TraceLab", "版本评测报告")):
        return True
    name = path.name
    try:
        if name in ("results.json", "metrics.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if name == "results.json":
                return isinstance(data, dict) and {"manifest", "cases"} <= set(data)
            return isinstance(data, dict) and {"values", "per_run"} <= set(data)
        if name == "submission.jsonl":
            first = next((line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), "")
            data = json.loads(first) if first else None
            return isinstance(data, dict) and {"case_id", "run_assessments"} <= set(data)
    except (OSError, ValueError):
        return False
    if name.endswith(".md"):
        return any(title in head for title in (
            "# YAI TraceLab 版本评测报告",
            "# AI 开票 Agent A/B 版本评测报告",
            "# 版本评测结论（业务版）",
            "# 评测技术报告（研发版）",
            "# 版本能力分析（产品版）",
        ))
    return False


def _clean_stale_outputs(out_dir: Path, *, keep_stale: bool) -> tuple[list[str], list[str]]:
    """只清理**本工具自己**的旧产物，返回 (已删除, 保留)。

    审计 R2-10 → R3-04：旧实现按文件名清空，会删用户的 report.md、甚至删本工具从不生成的
    run.json。现在两条判据：① 上次 run-manifest 里登记过；② 内容带本工具签名。
    两条都不满足的一律保留（不碰用户文件）；`--keep-stale` 时全部保留。
    """
    deleted: list[str] = []
    kept: list[str] = []
    manifest_path = out_dir / _OUTPUT_MANIFEST
    listed: set[str] = set()
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            listed = {str(item) for item in (data.get("files") or [])}
        except (OSError, ValueError):
            listed = set()
    for name in _EVALUATE_PRODUCTS:
        path = out_dir / name
        if not path.is_file():
            continue
        if keep_stale or (name not in listed and not _looks_like_own_output(path)):
            kept.append(name)
            continue
        try:
            path.unlink()
            deleted.append(name)
        except OSError:  # pragma: no cover - 只读文件等极端情况
            kept.append(name)
    if manifest_path.is_file() and not keep_stale:
        try:
            manifest_path.unlink()
        except OSError:  # pragma: no cover
            pass
    return deleted, kept


def _write_run_manifest(out_dir: Path, files: list[str]) -> None:
    """记录本次产出的文件清单，供下次复跑判断归属。"""
    from .evaluation import EVALUATOR_VERSION

    (out_dir / _OUTPUT_MANIFEST).write_text(
        json.dumps({"generator": EVALUATOR_VERSION, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _cmd_evaluate(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"错误：找不到 Case 文件 {cases_path}", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_error = _prepare_out_dir(out_dir)
    if out_error:
        print(f"错误：{out_error}；未写入任何文件。", file=sys.stderr)
        return 2
    adapter, adapter_error = _adapter_or_none(args.adapter)
    if adapter is None:
        print(f"错误：{adapter_error}；未写入任何文件。", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset = adapter.load(cases_path)
    if dataset.errors:
        print(f"警告：数据完整性存在 {len(dataset.errors)} 项错误，结果将标记为部分评测。", file=sys.stderr)

    result = evaluate_dataset(dataset)
    no_run_cases = [cr.case.case_id for cr in result.results if not cr.assessments]
    if no_run_cases:
        print(
            f"警告：{len(no_run_cases)} 个 Case 无可评估运行（runs 为空）："
            f"{'、'.join(no_run_cases)}；这些 Case 不参与指标与版本比较。",
            file=sys.stderr,
        )
    unknown_runs = int((result.manifest.get("integrity") or {}).get("unknown_runs") or 0)
    if unknown_runs:
        # 审计 F-13：UNKNOWN 是有效结论，但必须让运行者看见"这批有多少条没判出来"。
        # 不改变退出码（UNKNOWN 不等于评测失败）。
        print(
            f"注意：{unknown_runs} 条运行判定为 UNKNOWN（证据不足或评测器未完成判断）；"
            "它们不进入准确率分母，也不参与版本改善/退化排名。",
            file=sys.stderr,
        )
    # 审计 R2-10 + R3-04：清理放在**全部输入校验之后**（标注坏掉时旧产物不能被删），
    # 且只删本工具自己的文件（run-manifest 登记或内容签名），用户文件一律保留。
    labels: dict[str, dict[str, Any]] | None = None
    if args.labels:
        try:
            labels = load_reference_labels(Path(args.labels))
        except LabelFormatError as exc:
            print(f"错误：参考标注不可用：{exc}；未改动任何文件。", file=sys.stderr)
            return 2

    produced = ["submission.jsonl", "results.json", "report-official.md", "report.md"]
    if args.audience == "all":
        produced += ["report-business.md", "report-engineering.md", "report-product.md"]
    if labels is not None:
        produced.append("metrics.json")

    deleted, kept = _clean_stale_outputs(
        out_dir, keep_stale=bool(getattr(args, "keep_stale", False))
    )
    if deleted:
        print(f"注意：已清理上次评测的 {len(deleted)} 个产物（同一输出目录复跑）。", file=sys.stderr)
    if kept:
        print(
            f"注意：目录里有 {len(kept)} 个同名文件不属于本工具或未要求清理，已保留："
            f"{'、'.join(kept)}（如需清理请手工删除）",
            file=sys.stderr,
        )
    conflicts = [name for name in produced if (out_dir / name).exists()]
    if conflicts:
        # 审计 R3-04：宁可报错也不能覆盖用户文件。用户要么换输出目录，要么手工处理这些文件。
        print(
            f"错误：输出目录里存在不是本次产物的同名文件 {conflicts}；未写入任何文件。"
            "请换一个输出目录，或手工确认后可加 --keep-stale 并自行处理冲突文件。",
            file=sys.stderr,
        )
        return 2

    write_submission(result, out_dir / "submission.jsonl")
    write_results_json(result, out_dir / "results.json")

    metrics = None
    secondary_payload: dict[str, Any] | None = None
    if labels is not None:
        metrics = score_against_labels(result, labels)
        unmatched = int(metrics.values.get("labels_unmatched") or 0)
        if unmatched:
            # C-13：标注与数据 run_id 对不上时，分母会变小——必须显式警告，不能静默。
            matched = int(metrics.values.get("labels_matched") or 0)
            print(
                f"警告：{unmatched} 条参考标注在本次评测中找不到对应运行"
                f"（labels_matched={matched}，labels_unmatched={unmatched}）。"
                "指标分母按实际匹配数计算，请核对标注文件与 Case 文件是否同一批数据。",
                file=sys.stderr,
            )
        unlabeled = int(metrics.values.get("runs_unlabeled") or 0)
        if unlabeled:
            # 审计 F7-02：labels_unmatched=0 只证明"标注都找到了运行"，不证明"运行都有标注"。
            labeled = int(metrics.values.get("runs_labeled") or 0)
            evaluated = int(metrics.values.get("runs_evaluated") or 0)
            print(
                f"警告：本批 {unlabeled} 条评测运行没有参考标注"
                f"（runs_labeled={labeled} / runs_evaluated={evaluated}，"
                f"label_run_coverage={metrics.values.get('label_run_coverage')}）。"
                "指标只覆盖有标注的部分，不代表整批评测结果。",
                file=sys.stderr,
            )
        payload = metrics.as_dict()
        payload["secondary"] = compute_all(result, labels)
        secondary_payload = payload["secondary"]
        (out_dir / "metrics.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    write_official_report(result, out_dir / "report-official.md", metrics, secondary_payload)

    if args.audience == "all":
        write_markdown(result, out_dir / "report.md", metrics)
        written = write_audience_reports(result, out_dir, metrics, secondary_payload)
        for name, path in written.items():
            print(f"受众报告：{name} → {path.name}")
    else:
        builder = AUDIENCE_BUILDERS[args.audience]
        (out_dir / "report.md").write_text(builder(result, metrics, secondary_payload), encoding="utf-8")
        print(f"受众报告：{args.audience} → report.md")

    _write_run_manifest(out_dir, produced)

    summary = summarise(result)
    print(f"Case 数：{summary['cases']}　运行数：{summary['runs']}")
    print(f"处理正确：{summary['task_success']}　处理错误：{summary['task_failure']}　实际开票：{summary['invoice_created']}")
    print(f"版本方向：{summary['directions']}")
    if metrics and metrics.values:
        values = metrics.values
        print("方法校验：")
        for key in ("agent_task_success_accuracy", "business_invoice_created_accuracy",
                    "version_direction_accuracy", "first_fault_event_exact_match",
                    "root_cause_label_macro_f1", "evidence_id_validity"):
            if key in values:
                value = values[key]
                shown = "—" if value is None else f"{value:.2%}"
                print(f"  {key}: {shown}")
    print(f"产出目录：{out_dir.resolve()}")

    # C-07：评测不完整时不能再返回 0（旧行为恒 exit 0，CI 无法据此拦截）。
    reasons = _incomplete_reasons(result, dataset)
    if not reasons:
        return 0
    if args.allow_incomplete:
        print("警告：--allow-incomplete 已放行不完整评测，结论不能当作完整证据：", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 0
    print("错误：本次评测不完整，退出码 3（文件已产出，但结论不可作为完整证据）：", file=sys.stderr)
    for reason in reasons:
        print(f"  - {reason}", file=sys.stderr)
    print("若确认要在不完整数据上继续分析，显式加 --allow-incomplete。", file=sys.stderr)
    return 3


def _cmd_rules(args: argparse.Namespace) -> int:
    from .rules import DECISION_POLICY, RULE_VERSION, RULES

    print(f"规则集版本：{RULE_VERSION}　共 {len(RULES)} 条")
    print(f"判定策略：{DECISION_POLICY}")
    print()
    for spec in RULES:
        print(f"{spec.rule_id}  [{spec.label}]  dimension={spec.dimension}  -{spec.points:g}分")
        print(f"  前置：{spec.precondition}")
        print(f"  断言：{spec.assertion}")
        print()
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    cases = Path(args.cases)
    error = _require_file(cases, "Case 文件")
    if error:
        # 干净报错（退出码 2），不抛 FileNotFoundError 打穿 CLI
        print(f"错误：{error}", file=sys.stderr)
        return 2
    adapter, adapter_error = _adapter_or_none(args.adapter)
    if adapter is None:
        print(f"错误：{adapter_error}", file=sys.stderr)
        return 2
    dataset = adapter.load(cases)
    from .evaluation import build_manifest

    print(json.dumps(build_manifest(dataset), ensure_ascii=False, indent=2))
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    """打开即用：跑样例评测 → 生成单文件体验页（可选自动打开）。"""
    import json
    import webbrowser

    from .demo import write_demo_html

    out = Path(args.out)
    eval_args = argparse.Namespace(
        cases=args.cases, out=str(out), adapter="official", labels=None,
        audience="all", keep_stale=False, allow_incomplete=False,
    )
    code = _cmd_evaluate(eval_args)
    if code == 2:
        return 2
    # 审计 F6-04：3 表示数据不完整（0-run/降级等）。体验页照常生成，但退出码必须透传，
    # 不能把"部分评测"包装成成功。
    incomplete = code == 3
    results_path = out / "results.json"
    if not results_path.is_file():
        print("错误：评测未产出 results.json", file=sys.stderr)
        return 2
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    html_path = Path(args.html) if args.html else out / "demo.html"
    write_demo_html(payload, html_path, source=args.cases)
    print(f"体验页：{html_path}")
    print(f"完整报告：{out / 'report-official.md'}（以及 report.md / 三份受众报告）")
    if incomplete:
        print("⚠️ 评测不完整（数据含 0-run/降级等）：体验页已生成，但结论只对可解析部分有效。",
              file=sys.stderr)
    if not args.no_open:
        try:
            webbrowser.open(html_path.resolve().as_uri())
        except OSError:  # pragma: no cover - 无图形环境
            pass
    return 3 if incomplete else 0


def _cmd_ablate(args: argparse.Namespace) -> int:
    from .ablation import PROFILES, run_ablation, write_ablation
    from .evaluation import load_reference_labels

    cases = Path(args.cases)
    labels_path = Path(args.labels)
    error = _require_file(cases, "Case 文件")
    if error:
        print(f"错误：{error}；未产出任何文件。", file=sys.stderr)
        return 2
    if not labels_path.is_file():
        # 保留原有可读提示（消融没有标注就无法计算指标）
        print("错误：消融实验需要 --labels（没有参考标注就无法计算指标）", file=sys.stderr)
        return 2
    out_error = _prepare_out_dir(Path(args.out))
    if out_error:
        print(f"错误：{out_error}；未产出任何文件。", file=sys.stderr)
        return 2
    adapter, adapter_error = _adapter_or_none(args.adapter)
    if adapter is None:
        print(f"错误：{adapter_error}；未产出任何文件。", file=sys.stderr)
        return 2

    dataset = adapter.load(cases)
    try:
        labels = load_reference_labels(labels_path)
    except LabelFormatError as exc:
        print(f"错误：参考标注不可用：{exc}；未产出任何文件。", file=sys.stderr)
        return 2
    report = run_ablation(dataset, labels)
    md, js = write_ablation(report, dataset, Path(args.out))

    print(f"变体数：{len(PROFILES)}　数据：{len(dataset)} Case / {sum(len(c.runs) for c in dataset)} 运行")
    header = f"{'变体':<18}{'任务正确率':>12}{'首因EM':>10}{'方向准确率':>12}"
    print(header)
    print("-" * len(header))
    def pct(metrics: dict[str, object], key: str) -> str:
        value = metrics.get(key)
        return f"{value:.1%}" if isinstance(value, float) else "—"

    for outcome in report.outcomes:
        m = dict(outcome.metrics)
        print(f"{outcome.profile.name:<18}{pct(m, 'agent_task_success_accuracy'):>12}"
              f"{pct(m, 'first_fault_event_exact_match'):>10}{pct(m, 'version_direction_accuracy'):>12}")
    print()
    for outcome in report.outcomes[1:]:
        print(f"{outcome.profile.name}: 漏判 {len(outcome.false_negatives)} 条，"
              f"误报 {len(outcome.false_positives)} 条，"
              f"静默回归漏报 {len(outcome.silent_regressions)} 个")
    print(f"产出：{md}　{js}")
    return 0


def _cmd_mutate(args: argparse.Namespace) -> int:
    from .mutation import combine, run_injection_study, write_study
    from .mutation import overall as mutation_overall

    paths: list[Path] = []
    for raw in args.cases:
        path = Path(raw)
        error = _require_file(path, "Case 文件")
        if error:
            print(f"错误：{error}；未产出任何文件。", file=sys.stderr)
            return 2
        resolved = path.resolve()
        if resolved in paths:
            print(f"警告：重复注入同一文件 {path}，已去重（避免统计翻倍）。", file=sys.stderr)
            continue
        paths.append(resolved)
    out_error = _prepare_out_dir(Path(args.out))
    if out_error:
        print(f"错误：{out_error}；未产出任何文件。", file=sys.stderr)
        return 2
    adapter, adapter_error = _adapter_or_none(args.adapter)
    if adapter is None:
        print(f"错误：{adapter_error}；未产出任何文件。", file=sys.stderr)
        return 2
    loaded = [adapter.load(path) for path in paths]
    dataset = combine(loaded) if len(loaded) > 1 else loaded[0]
    results = run_injection_study(dataset)
    md, js = write_study(results, dataset, Path(args.out))

    stats = mutation_overall(results)
    rate = "—" if stats["detection_rate"] is None else f"{stats['detection_rate']:.1%}"
    print(f"注入类型 {stats['injections']}　尝试 {stats['tried']}　检出 {stats['detected']}　检出率 {rate}")
    for r in results:
        shown = "—" if r.rate is None else f"{r.rate:.1%}"
        print(f"  {r.injection:<24}{r.tried:>3} 尝试 / {r.detected:>3} 检出 / {shown}")
    print(f"产出：{md}　{js}")
    return 0


def _cmd_regression(args: argparse.Namespace) -> int:
    from .regression import (
        PackExistsError,
        PackFormatError,
        build_expectations,
        load_pack,
        verify_pack,
        write_pack,
        write_verify,
    )

    cases = Path(args.cases)
    error = _require_file(cases, "Case 文件")
    if error:
        print(f"错误：{error}；未产出任何文件。", file=sys.stderr)
        return 2
    out_error = _prepare_out_dir(Path(args.out))
    if out_error:
        print(f"错误：{out_error}；未产出任何文件。", file=sys.stderr)
        return 2
    adapter, adapter_error = _adapter_or_none(args.adapter)
    if adapter is None:
        print(f"错误：{adapter_error}；未产出任何文件。", file=sys.stderr)
        return 2
    dataset = adapter.load(cases)

    if args.action == "build":
        result = evaluate_dataset(dataset)
        expectations = build_expectations(result)
        try:
            path = write_pack(
                expectations, Path(args.out), dataset,
                allow_overwrite=bool(getattr(args, "force", False)),
            )
        except PackExistsError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        counts = {v: sum(1 for e in expectations if e.verdict.value == v) for v in ("PASS", "FAIL", "UNKNOWN")}
        print(f"回归快照已生成：{path}")
        print(f"期望 {len(expectations)} 条运行：PASS {counts['PASS']}　FAIL {counts['FAIL']}"
              f"　UNKNOWN {counts['UNKNOWN']}（UNKNOWN 不计入回归守门）")
        if getattr(args, "force", False):
            print("已按 --force 覆盖活动快照，旧文件已归档到 archive/（历史未删除）。")
        return 0

    pack_path = Path(args.pack)
    if not pack_path.exists():
        print(f"错误：找不到回归包 {pack_path}", file=sys.stderr)
        return 2
    allow_legacy = bool(getattr(args, "allow_legacy", False))
    try:
        pack = load_pack(pack_path, allow_legacy=allow_legacy)
        report = verify_pack(
            pack, dataset,
            allow_data_evolution=bool(getattr(args, "allow_data_evolution", False)),
            allow_legacy=allow_legacy,
            source=str(pack_path),
        )
    except PackFormatError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    meta = dict(pack.get("created_from") or {})
    md, js = write_verify(report, Path(args.out), meta)
    for issue in report.compatibility:
        tag = "ERROR" if issue.level == "error" else "WARN"
        print(f"  [{tag}] {issue.field}：快照={issue.expected} 当前={issue.actual}")
    if report.legacy_pack:
        print(f"警告：旧快照降级读取（{len(report.legacy_downgraded)} 条旧 false 按 UNKNOWN 处理），"
              f"结论不等同于三态复测，请用 regression build 重建。", file=sys.stderr)
    print(f"复测：一致 {len(report.matches)}　改善 {len(report.improved)}　回归 {len(report.regressed)}"
          f"　证据不足 {len(report.insufficient_evidence)}　证据恢复 {len(report.evidence_recovered)}"
          f"　证据丢失 {len(report.evidence_lost)}　判据变化 {len(report.changed)}"
          f"　缺失 {len(report.missing)}")
    # 数据里存在、快照里没有的运行同样不能被宣称"通过"：复测只对覆盖到的运行负责
    uncovered = getattr(report, "uncovered", [])
    covered = getattr(report, "covered_runs", len(report.matches))
    data_runs = getattr(report, "data_runs", covered + len(uncovered))
    print(f"数据覆盖：{covered}/{data_runs} 条运行被快照覆盖"
          + (f"；{len(uncovered)} 条未覆盖（新增运行未经验证）" if uncovered else "（全覆盖）"))
    print(f"产出：{md}　{js}")
    if report.exit_code:
        print("复测未通过：存在回归、缺失、证据不足或环境不兼容，不能宣称通过，需人工确认。",
              file=sys.stderr)
        return report.exit_code
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    """最小入口：评测 → 写依据记录（独立产物）→ 独立复核 → 写统计。

    只做 Python API 的薄封装：不写 submission.jsonl，也不碰其它官方输出文件。
    """
    from .evidence_record import build_records_for_result, write_records
    from .evidence_verify import verify_records_for_cases
    from .rules import RULE_VERSION

    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"错误：找不到 Case 文件 {cases_path}", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_error = _prepare_out_dir(out_dir)
    if out_error:
        print(f"错误：{out_error}；未写入任何文件。", file=sys.stderr)
        return 2
    adapter, adapter_error = _adapter_or_none(args.adapter)
    if adapter is None:
        print(f"错误：{adapter_error}；未写入任何文件。", file=sys.stderr)
        return 2
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    dataset = adapter.load(cases_path)
    if dataset.errors:
        print(f"警告：数据完整性存在 {len(dataset.errors)} 项错误，依据记录只覆盖可解析的部分。", file=sys.stderr)

    result = evaluate_dataset(dataset)
    records = build_records_for_result(result)
    cases = [case_result.case for case_result in result.results]
    if not records:
        print("错误：没有任何可复算的受支持规则证据（R001/R002/R010/R011），未产出记录。", file=sys.stderr)
        return 2

    records_path = write_records(records, evidence_dir / "records.jsonl")
    batch = verify_records_for_cases(records, cases)
    payload = batch.to_dict()
    payload["rulepack_version"] = RULE_VERSION
    (evidence_dir / "verify.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    counts = dict(batch.counts)
    print(f"依据记录：{len(records)} 条 → {records_path.name}")
    print(
        f"独立复核：VERIFIED {counts['VERIFIED']}　MISMATCH {counts['MISMATCH']}"
        f"　INSUFFICIENT {counts['INSUFFICIENT']}"
    )
    print(f"产出目录：{evidence_dir.resolve()}")
    if counts["MISMATCH"]:
        print("错误：存在与原始轨迹不一致的依据记录，不能宣称复核通过。", file=sys.stderr)
        return 1
    return 0


def _cmd_experiments(args: argparse.Namespace) -> int:
    """最小入口：跑变形实验（语义保持 + 业务变化），落盘机读与可读两份结果。

    样本全部自造（`tracelab/samples.py`），不读官方资料包——评委在任意环境都能复现。
    """
    from .experiments import render_suite_markdown, run_experiment_suite

    out_dir = Path(args.out)
    out_error = _prepare_out_dir(out_dir)
    if out_error:
        print(f"错误：{out_error}；未写入任何文件。", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    suite = run_experiment_suite(seed=args.seed)
    (out_dir / "report.json").write_text(
        json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_suite_markdown(suite), encoding="utf-8")

    invariance = suite["invariance"]
    sensitivity = suite["sensitivity"]
    print(
        f"语义保持：{invariance['consistent']}/{invariance['total']} 一致"
        f"（不一致 {invariance['inconsistent']}）"
    )
    print(
        f"业务变化：{sensitivity['detected']}/{sensitivity['total']} 检出"
        f"（首因稳定 {sensitivity['attribution_stable']}/{sensitivity['total']}，"
        f"误报变体 {sensitivity['false_positive_variants']}）"
    )
    print(f"覆盖规则：{', '.join(sensitivity['rule_coverage'])}")
    print(f"产出：{out_dir.resolve()}")
    return 0


def _version_text() -> str:
    """`--version` 的输出：包版本 + 评测器/规则集版本（三者必须能对上）。"""
    from . import __version__
    from .evaluation import EVALUATOR_VERSION
    from .rules import RULE_VERSION

    return f"tracelab {__version__}　评测器 {EVALUATOR_VERSION}　规则集 {RULE_VERSION}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracelab", description="AI 开票 Agent 离线轨迹评测（证据驱动）")
    parser.add_argument("--version", action="version", version=_version_text())
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("evaluate", help="跑批量评测并产出提交文件与报告")
    p_eval.add_argument("--cases", required=True, help="待评测 Case 的 JSONL 文件")
    p_eval.add_argument("--out", required=True, help="产出目录")
    p_eval.add_argument("--labels", default=None, help="参考标注 JSONL（仅用于方法校验，不参与评测）")
    p_eval.add_argument("--adapter", default="official", help="数据适配器名，默认 official")
    p_eval.add_argument("--audience", default="all",
                        choices=["all", "business", "engineering", "product"],
                        help="报告受众：all=综合+三份分版（默认），或只出某一版")
    p_eval.add_argument("--keep-stale", action="store_true",
                        help="复用 --out 时不清理上次产物（默认只清理本工具自己的文件）")
    p_eval.add_argument("--allow-incomplete", action="store_true",
                        help="显式放行不完整评测（读取不全 / 评测不完整 / 数据有错时默认退出码 3）")
    p_eval.set_defaults(func=_cmd_evaluate)

    p_demo = sub.add_parser("demo", help="打开即用：跑样例并生成单文件体验页")
    p_demo.add_argument("--cases", default="data/sample/sample-traces.jsonl",
                        help="样例轨迹（默认仓库自带自造样例，非赛事资料包）")
    p_demo.add_argument("--out", default="out/demo", help="输出目录")
    p_demo.add_argument("--html", default="", help="体验页路径（默认 <out>/demo.html）")
    p_demo.add_argument("--no-open", action="store_true", help="只生成，不自动打开浏览器")
    p_demo.set_defaults(func=_cmd_demo)

    p_abl = sub.add_parser("ablate", help="消融实验：量化领域语义规则对准确率的贡献")
    p_abl.add_argument("--cases", required=True, help="Case 的 JSONL 文件")
    p_abl.add_argument("--labels", required=True, help="参考标注 JSONL（必需）")
    p_abl.add_argument("--out", default="out/ablation", help="产出目录")
    p_abl.add_argument("--adapter", default="official")
    p_abl.set_defaults(func=_cmd_ablate)

    p_mut = sub.add_parser("mutate", help="缺陷注入实验：量化回归发现率")
    p_mut.add_argument("--cases", required=True, action="append",
                      help="可重复指定，跨数据集合并跑注入")
    p_mut.add_argument("--out", default="out/mutation")
    p_mut.add_argument("--adapter", default="official")
    p_mut.set_defaults(func=_cmd_mutate)

    p_reg = sub.add_parser(
        "regression",
        help="回归闭环：生成期望快照 / 复测判断漂移",
        epilog=(
            "退出码：0=零漂移；1=有回归/缺失/证据不足；"
            "2=输入或路径错误；4=复测前提不成立（数据/版本不兼容）"
        ),
    )
    reg_sub = p_reg.add_subparsers(dest="action", required=True)

    reg_build = reg_sub.add_parser("build", help="把当前评测结论固化成回归快照")
    reg_build.add_argument("--cases", required=True)
    reg_build.add_argument("--out", default="regression/invoice-pack")
    reg_build.add_argument("--adapter", default="official")
    reg_build.add_argument("--force", action="store_true",
                           help="允许覆盖已有活动快照（旧文件自动归档到 archive/，历史不删除）")
    reg_build.set_defaults(func=_cmd_regression)

    reg_verify = reg_sub.add_parser("verify", help="用新数据/新规则复测，检查判断漂移")
    reg_verify.add_argument("--pack", required=True, help="regression-pack.json 路径")
    reg_verify.add_argument("--cases", required=True)
    reg_verify.add_argument("--out", default="out/regression-verify")
    reg_verify.add_argument("--adapter", default="official")
    reg_verify.add_argument("--allow-data-evolution", action="store_true",
                            help="允许数据哈希变化（仍以 warn 记录，旧快照保留为历史证据）")
    reg_verify.add_argument("--allow-legacy", action="store_true",
                            help="保守读取旧格式 (/1) 快照：旧 false 记为 UNKNOWN，报告中标注降级读取")
    reg_verify.set_defaults(func=_cmd_regression)

    p_evi = sub.add_parser("evidence", help="为受支持规则产出可独立复算的依据记录（独立产物）")
    p_evi.add_argument("--cases", required=True, help="Case 的 JSONL 文件")
    p_evi.add_argument("--out", required=True, help="产出目录（写入 evidence/records.jsonl 与 evidence/verify.json）")
    p_evi.add_argument("--adapter", default="official")
    p_evi.set_defaults(func=_cmd_evidence)

    p_rules = sub.add_parser("rules", help="列出规则集")
    p_rules.set_defaults(func=_cmd_rules)

    p_exp = sub.add_parser("experiments", help="变形实验：语义保持 + 业务变化（自造样本，固定种子可复现）")
    p_exp.add_argument("--out", default="out/experiments", help="产出目录（写 report.json 与 report.md）")
    p_exp.add_argument("--seed", type=int, default=20261104, help="随机种子（默认 20261104）")
    p_exp.set_defaults(func=_cmd_experiments)

    p_man = sub.add_parser("manifest", help="输出实验清单")
    p_man.add_argument("--cases", required=True)
    p_man.add_argument("--adapter", default="official")
    p_man.set_defaults(func=_cmd_manifest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, OSError, ValueError) as exc:
        # 审计 R2-11/R3-A-08：输入/路径类错误统一退出码 2
        #（契约：0 通过 / 1 检查未过 / 2 输入错 / 3 不完整 / 4 复测前提不成立），
        # 不再打栈回溯，也不与"检查未通过"混成同一个码。
        print(f"错误：{type(exc).__name__}: {exc}；未继续执行。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())






