"""独立 holdout 冻结 / 标注模板 / 校验 / 报告工具（审计「问题是否成立」支撑）。

设计原则（写进 `docs/independent-eval.md`）：

1. **冻结**：只记录数据文件 SHA-256、Case/Run 清单、代码提交与规则集版本，
   不复制赛事资料包原文；复测时若数据哈希变化，直接拒绝。
2. **第二标注者**：`template` 生成每条运行的空白标注；标注者必须声明
   `annotator` 与 `annotator_independent=true`（不是规则作者），`check` 才放行。
3. **预注册**：`report` 只计算预定义指标（任务成功率 / 开票状态 / 首因 EM /
   标签 macro-F1 / 证据有效率 / UNKNOWN 比例 / labels_unmatched）与 Wilson 95% CI，
   并明确写出"n 很小、不能据此宣称泛化"。

用法::

    python scripts/holdout.py freeze  --cases data/official/student-1.0/04_无标注验证轨迹.jsonl
    python scripts/holdout.py template --cases data/official/student-1.0/04_无标注验证轨迹.jsonl
    # 第二标注者填写 docs/holdout/labels.jsonl 后：
    python scripts/holdout.py check  --cases data/official/student-1.0/04_无标注验证轨迹.jsonl \
        --labels docs/holdout/labels.jsonl
    python scripts/holdout.py report --cases data/official/student-1.0/04_无标注验证轨迹.jsonl \
        --labels docs/holdout/labels.jsonl --out out/holdout

退出码：0 通过；2 输入/哈希错误；3 标注不完整或标注者身份不合规。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.adapters import load_dataset  # noqa: E402
from tracelab.evaluation import (  # noqa: E402
    EVALUATOR_VERSION,
    LabelFormatError,
    evaluate_dataset,
    score_against_labels,
    validate_label_fields,
)
from tracelab.rules import RULE_VERSION  # noqa: E402

DEFAULT_HOLDOUT_CASES = ROOT / "data" / "official" / "student-1.0" / "04_无标注验证轨迹.jsonl"

FREEZE_DEFAULT = ROOT / "docs" / "holdout" / "freeze.json"
TEMPLATE_DEFAULT = ROOT / "docs" / "holdout" / "labels.template.jsonl"
LABELS_DEFAULT = ROOT / "docs" / "holdout" / "labels.jsonl"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _code_commit() -> str:
    """从环境变量读取（CI 注入），没有就写 unknown——不伪造 commit。"""
    import os

    return os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA") or "unknown"


def _git_meta() -> tuple[str, bool]:
    """返回 (HEAD commit, 工作区是否 dirty)；git 不可用时退回环境变量。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )
        commit = head.stdout.strip() if head.returncode == 0 else ""
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
        return (commit or _code_commit()), dirty
    except (OSError, subprocess.SubprocessError):
        return _code_commit(), False


def _code_snapshot_sha() -> str:
    """评测器与规则相关的代码快照摘要（审计 F7-03）。

    冻结不仅要绑定数据，也要绑定"当时跑的代码/规则"；否则只校验数据哈希无法阻止
    "改规则后复用旧 holdout"。范围：tracelab/*.py + pyproject.toml。
    """
    digest = hashlib.sha256()
    files = sorted((ROOT / "tracelab").rglob("*.py")) + [ROOT / "pyproject.toml"]
    for path in files:
        if not path.is_file():
            continue
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _dataset_summary(dataset: Any) -> dict[str, Any]:
    runs = [(case.case_id, run.run_id) for case in dataset.cases for run in case.runs]
    return {
        "cases": len(dataset.cases),
        "runs": len(runs),
        "case_ids": [case.case_id for case in dataset.cases],
        "run_keys": [f"{case_id}/{run_id}" for case_id, run_id in runs],
    }


def _freeze_guard(cases: Path, freeze: Path) -> tuple[str | None, list[str]]:
    """校验冻结清单：数据哈希必验，代码/规则摘要按可用性校验（审计 F5-02 / F7-03）。"""
    warnings: list[str] = []
    if not freeze.is_file():
        return f"缺少冻结清单 {freeze}（先运行 freeze）", warnings
    try:
        frozen = json.loads(freeze.read_text(encoding="utf-8"))
        source = frozen.get("source") or {}
        frozen_sha = str(source.get("sha256") or "")
        code_sha = str(source.get("code_sha256") or "")
    except (OSError, ValueError) as exc:
        return f"无法读取冻结清单 {freeze}：{exc}", warnings
    actual = _sha256(cases)
    if not frozen_sha or frozen_sha != actual:
        return "数据文件哈希与冻结清单不一致——holdout 已被改动，拒绝继续。", warnings
    if not code_sha:
        warnings.append(
            "legacy freeze：未记录代码/规则摘要，仅校验数据哈希；"
            "无法证明预注册之后代码/规则未变（保留历史结果，不复用本批做新结论）。"
        )
        return None, warnings
    if code_sha != _code_snapshot_sha():
        return (
            "冻结时的评测器/规则代码与当前不一致——预注册条件已失效，拒绝继续；"
            "请换一批新数据重新冻结，而不是复用旧 holdout。"
        ), warnings
    return None, warnings


def cmd_freeze(args: argparse.Namespace) -> int:
    path = Path(args.cases)
    if not path.is_file():
        print(f"错误：找不到数据文件 {path}", file=sys.stderr)
        return 2
    out = Path(args.out)
    # 审计 F5-06：已存在的冻结清单默认禁止覆盖；数据哈希变化时绝不允许静默换基线。
    if out.is_file() and not args.force:
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
            existing_sha = str(existing.get("source", {}).get("sha256") or "")
        except (OSError, ValueError):
            existing_sha = ""
        actual = _sha256(path)
        if existing_sha and existing_sha != actual:
            print(f"❌ 已有冻结清单的数据哈希与当前文件不一致：{out}", file=sys.stderr)
            print(f"   冻结时 {existing_sha[:16]}… / 当前 {actual[:16]}…；拒绝静默换基线。", file=sys.stderr)
            print("   如确认要换新一批 holdout，请加 --force 并重新标注。", file=sys.stderr)
            return 2
        print(f"❌ 冻结清单已存在：{out}；如确认重建请加 --force。", file=sys.stderr)
        return 2
    dataset = load_dataset(path)
    git_commit, git_dirty = _git_meta()
    payload = {
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "file": path.name,
            "sha256": _sha256(path),
            "code_sha256": _code_snapshot_sha(),
        },
        "code_commit": git_commit,
        "code_dirty": git_dirty,
        "evaluator_version": EVALUATOR_VERSION,
        "rulepack_version": RULE_VERSION,
        "dataset": _dataset_summary(dataset),
        "note": "冻结只记录哈希与清单，不复制赛事资料包原文；标注完成前不得改写本文件。",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已冻结：{out}（{payload['dataset']['cases']} Case / {payload['dataset']['runs']} 运行）")
    return 0


def cmd_template(args: argparse.Namespace) -> int:
    dataset = load_dataset(Path(args.cases))
    lines: list[str] = []
    for case in dataset.cases:
        for run in case.runs:
            lines.append(json.dumps({
                "case_id": case.case_id,
                "run_id": run.run_id,
                "annotator": "",
                "annotator_independent": False,
                "annotated_at": "",
                "agent_task_success": None,
                "business_invoice_created": None,
                "root_causes": [],
                "version_direction_on_this_case": "",
                "notes": "",
            }, ensure_ascii=False))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成标注模板：{out}（{len(lines)} 条运行；第二标注者填写后另存为 labels.jsonl）")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """只打印原始轨迹（盲标用）：不读评测结果、不显示规则结论。"""
    dataset = load_dataset(Path(args.cases))
    shown = 0
    for case in dataset.cases:
        if args.case and case.case_id != args.case:
            continue
        print(f"=== Case {case.case_id} | 场景：{case.scenario}")
        print(f"用户请求：{case.user_request}")
        print(f"context：{json.dumps(dict(case.context), ensure_ascii=False, sort_keys=True)}")
        print(f"reference_result：{json.dumps(dict(case.reference_result), ensure_ascii=False, sort_keys=True)}")
        for run in case.runs:
            if args.run and run.run_id != args.run:
                continue
            shown += 1
            print()
            print(f"--- Run {run.run_id} | agent_version={run.agent_version} | "
                  f"final_state={run.final_state} | invoice_created={run.invoice_created}")
            for event in run.events:
                print(f"  [seq {event.seq}] {event.event_id} | {event.step_id} | status={event.step_status} | "
                      f"code={event.result_code} | error={event.error_code or '—'} | retries={event.retry_count}")
                if event.observed_input:
                    print(f"    in : {json.dumps(dict(event.observed_input), ensure_ascii=False)}")
                if event.observed_output:
                    print(f"    out: {json.dumps(dict(event.observed_output), ensure_ascii=False)}")
                for tool in event.tool_calls:
                    print(f"    tool {tool.name} args={json.dumps(dict(tool.arguments), ensure_ascii=False)} "
                          f"resp={json.dumps(dict(tool.result), ensure_ascii=False)}")
        print()
    if shown == 0:
        print("没有匹配的 Case/Run（检查 --case/--run 拼写）")
        return 2
    return 0


def cmd_worksheet(args: argparse.Namespace) -> int:
    """生成一页式盲标 worksheet（Markdown）：只含轨迹，不含评测器结论。"""
    dataset = load_dataset(Path(args.cases))
    lines: list[str] = [
        "# Holdout 盲标 worksheet",
        "",
        "> 只依据轨迹与题面标注；不要打开 `out/` 里本数据的评测结果，也不要参考任何规则结论。",
        "> 标完后把结论填进 `docs/holdout/labels.jsonl`（模板已含 case_id/run_id）。",
        "",
    ]
    for case in dataset.cases:
        lines += [
            f"## Case {case.case_id}",
            "",
            f"- 场景：{case.scenario}",
            f"- 用户请求：{case.user_request}",
            f"- context：`{json.dumps(dict(case.context), ensure_ascii=False)}`",
            f"- reference_result（题内目标）：`{json.dumps(dict(case.reference_result), ensure_ascii=False)}`",
            "",
        ]
        for run in case.runs:
            lines += [
                f"### Run `{run.run_id}`（agent `{run.agent_version}`）",
                "",
                "| seq | event_id | 步骤 | 状态 | 输入 | 输出 | 工具调用 |",
                "|---|---|---|---|---|---|---|",
            ]
            for event in run.events:
                tool_text = "; ".join(
                    f"{tool.name}(args={json.dumps(dict(tool.arguments), ensure_ascii=False)}, "
                    f"resp={json.dumps(dict(tool.result), ensure_ascii=False)})"
                    for tool in event.tool_calls
                )
                def cell(value: Any) -> str:
                    return json.dumps(value, ensure_ascii=False).replace("|", "\\|")

                lines.append(
                    f"| {event.seq} | `{event.event_id}` | {event.step_id} | {event.step_status} | "
                    f"{cell(dict(event.observed_input))} | {cell(dict(event.observed_output))} | {tool_text or '—'} |"
                )
            lines += [
                "",
                f"**{run.run_id} 判定**：",
                "- agent_task_success：☐ true　☐ false",
                "- business_invoice_created：☐ true　☐ false",
                "- root_causes（无则写 `[]`）：",
                "- notes（歧义/不确定）：",
                "",
            ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成盲标工作表：{out}")
    return 0


def cmd_csv(args: argparse.Namespace) -> int:
    """导出便于 Excel 填写的 CSV（UTF-8 BOM，16 行），只含轨迹标识不留答案。"""
    import csv

    dataset = load_dataset(Path(args.cases))
    out = Path(args.out)
    # 保护已填写的标注：默绝不覆盖（避免误双击第 1 步把工作清空）
    if out.is_file() and not args.force:
        try:
            with out.open("r", encoding="utf-8-sig", newline="") as existing:
                filled = [
                    row for row in csv.DictReader(existing)
                    if any((row.get(key) or "").strip()
                           for key in ("agent_task_success", "business_invoice_created",
                                       "root_causes", "notes"))
                ]
        except OSError as exc:
            # Excel 打开 CSV 时会锁文件：这里必须给可读提示，不能抛 traceback
            print(f"❌ 无法读取 {out}（很可能正被 Excel 打开）：{exc}", file=sys.stderr)
            print("   请先关闭 Excel 再双击本步骤；已填写的内容不会丢。", file=sys.stderr)
            return 2
        if filled:
            print(f"❌ {out} 已有填写内容（{len(filled)} 行），拒绝覆盖。", file=sys.stderr)
            print("   如确需重置，请删除该文件后重试（或在命令行加 --force）。", file=sys.stderr)
            return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle_ctx = out.open("w", encoding="utf-8-sig", newline="")
    except OSError as exc:
        print(f"❌ 无法写入 {out}（可能正被 Excel 打开）：{exc}", file=sys.stderr)
        print("   请先关闭 Excel 再重试。", file=sys.stderr)
        return 2
    with handle_ctx as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "run_id", "agent_task_success", "business_invoice_created",
                         "root_causes", "notes", "annotated_at"])
        for case in dataset.cases:
            for run in case.runs:
                writer.writerow([case.case_id, run.run_id, "", "", "", "", ""])
    print(f"已生成 CSV：{out}")
    print("用 Excel/WPS 打开，只填后四列：")
    print("  agent_task_success / business_invoice_created：填 是 或 否")
    print("  root_causes：失败才填，格式 LABEL@事件ID|证据ID1,证据ID2；多条用分号隔开；没错留空")
    print("  notes：拿不准/歧义写这里（可留空）")
    print("  annotated_at：可留空；留空则标注文件哈希稳定（重复导入不影响复现）")
    return 0


def _parse_root_causes(text: str, run_id: str) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        label_part, _, rest = chunk.partition("@")
        event_part, _, evidence_part = rest.partition("|")
        evidence = [item.strip() for item in evidence_part.split(",") if item.strip()]
        causes.append({
            "label": label_part.strip(),
            "first_fault_event_id": event_part.strip(),
            "evidence_event_ids": evidence,
        })
    return causes


class CsvLabelError(ValueError):
    """CSV 填表错误（行号 + 原因）。"""


def _parse_csv_bool(value: str, field: str, number: int, run_id: str, problems: list[str]) -> bool:
    text = (value or "").strip().lower()
    if text in ("是", "true", "1", "y", "yes"):
        return True
    if text in ("否", "false", "0", "n", "no"):
        return False
    problems.append(f"第 {number} 行（{run_id}）的 {field} 只能填 是/否，当前为 {value!r}")
    return False


def cmd_import_csv(args: argparse.Namespace) -> int:
    """把 Excel 填好的 CSV 转成 labels.jsonl（校验两列必填、布尔可解析）。"""
    import csv

    source = Path(args.csv)
    if not source.is_file():
        print(f"错误：找不到 {source}", file=sys.stderr)
        return 2
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for number, raw in enumerate(reader, 2):
            case_id = (raw.get("case_id") or "").strip()
            run_id = (raw.get("run_id") or "").strip()
            if not case_id or not run_id:
                problems.append(f"第 {number} 行缺 case_id/run_id")
                continue

            rows.append({
                "case_id": case_id,
                "run_id": run_id,
                "annotator": args.annotator,
                "annotator_independent": not args.self_annotation,
                # 审计 F5-03：不再写 now()——同一 CSV 重复导入必须得到相同 labels.jsonl
                "annotated_at": (raw.get("annotated_at") or "").strip(),
                "agent_task_success": _parse_csv_bool(
                    raw.get("agent_task_success", ""), "agent_task_success", number, run_id, problems
                ),
                "business_invoice_created": _parse_csv_bool(
                    raw.get("business_invoice_created", ""), "business_invoice_created",
                    number, run_id, problems,
                ),
                "root_causes": _parse_root_causes(raw.get("root_causes", ""), run_id),
                "version_direction_on_this_case": "",
                "notes": (raw.get("notes") or "").strip(),
            })
    if problems:
        for problem in problems:
            print(f"❌ {problem}", file=sys.stderr)
        print("请修正 Excel 后重新双击/重跑本步骤。", file=sys.stderr)
        return 3
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                   encoding="utf-8")
    print(f"已生成 {out}（{len(rows)} 条）")
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    """生成一页 HTML 盲标指南（轨迹 + 判断方法），双击后自动用浏览器打开。"""
    import html as html_lib

    dataset = load_dataset(Path(args.cases))
    esc = html_lib.escape

    def pre(value: Any) -> str:
        return f"<pre>{esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"

    parts: list[str] = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<title>Holdout 盲标指南</title>",
        "<style>",
        "body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:1100px;margin:24px auto;",
        "padding:0 16px;line-height:1.5;color:#222}",
        "h1{border-bottom:3px solid #333;padding-bottom:8px}",
        "h2{margin-top:40px;background:#eef3f8;padding:8px 10px;border-left:6px solid #357}",
        "h3{margin-top:26px;color:#145}",
        "table{border-collapse:collapse;width:100%;font-size:12px;margin:8px 0}",
        "th,td{border:1px solid #bbb;padding:4px 6px;vertical-align:top;text-align:left}",
        "th{background:#f2f2f2}",
        "pre{margin:2px 0;white-space:pre-wrap;word-break:break-all;font-size:12px}",
        ".rule{background:#fffbe6;border:1px solid #e0c060;padding:12px 16px;border-radius:6px}",
        ".run{background:#f7fdf7;border:1px solid #9c9;padding:8px 12px;border-radius:6px}",
        ".key{font-weight:bold;color:#a33}",
        "</style></head><body>",
        "<h1>Holdout 盲标指南</h1>",
        "<div class='rule'><b>只看下面这些内容判断，不要看任何评测结果。每条 Run 判断两件事：</b>",
        "<ol>",
        "<li><b>agent_task_success</b>：这条 Run 是否完成了题内目标？<br>",
        "看 Case 的 <code>reference_result</code>：<br>",
        "&nbsp;&nbsp;· <code>required_action</code> 是 <code>ASK_...</code> → 正确做法是停下来问用户、不出票；<br>",
        "&nbsp;&nbsp;· <code>acceptable_snapshot_state</code> → Run 的终态应该等于它；<br>",
        "&nbsp;&nbsp;· <code>invoice_should_exist</code> → true 该出票，false 不该出票；<br>",
        "&nbsp;&nbsp;· <code>expected_amounts</code> 若给了 → 看金额三项是否一致（没给就按金额×税率手算）。<br>",
        "三项都对、且中间没有明显硬错误（金额错/未确认就提交/重复开票/编造字段）→ 填 <b>是</b>，否则 <b>否</b>。</li>",
        "<li><b>business_invoice_created</b>：这条 Run <b>实际</b>有没有生成发票？看 Run 标题的",
        "<code>invoice_created</code> 与 <code>SUBMIT_INVOICE</code> 事件 → 填 <b>是</b> / <b>否</b>。</li>",
        "</ol>",
        "判断拿不准就在 Excel 的 <code>notes</code> 列写一句；不要为了跟评测器一致去猜。",
        "</div>",
    ]
    for case in dataset.cases:
        parts += [
            f"<h2>Case {esc(case.case_id)}　{esc(case.scenario)}</h2>",
            f"<p><b>用户请求：</b>{esc(case.user_request)}</p>",
            "<p><b>context：</b></p>", pre(dict(case.context)),
            "<p><b class='key'>reference_result（题内目标，判断依据）：</b></p>", pre(dict(case.reference_result)),
        ]
        for run in case.runs:
            parts += [
                f"<h3>Run {esc(run.run_id)}　agent={esc(run.agent_version)}　"
                f"final_state={esc(run.final_state)}　invoice_created={run.invoice_created}</h3>",
                "<table><tr><th>seq</th><th>event_id</th><th>步骤</th><th>状态</th>"
                "<th>输入</th><th>输出</th><th>工具调用</th></tr>",
            ]
            for event in run.events:
                tools = "; ".join(
                    f"{tool.name}(args={json.dumps(dict(tool.arguments), ensure_ascii=False)}, "
                    f"resp={json.dumps(dict(tool.result), ensure_ascii=False)})"
                    for tool in event.tool_calls
                )
                parts.append(
                    f"<tr><td>{event.seq}</td><td>{esc(event.event_id)}</td><td>{esc(event.step_id)}</td>"
                    f"<td>{esc(event.step_status)}</td><td><pre>{esc(json.dumps(dict(event.observed_input), ensure_ascii=False))}</pre></td>"
                    f"<td><pre>{esc(json.dumps(dict(event.observed_output), ensure_ascii=False))}</pre></td>"
                    f"<td><pre>{esc(tools or '—')}</pre></td></tr>"
                )
            parts.append("</table>")
            parts.append(
                f"<div class='run'>回到 Excel 的 <b>{esc(run.run_id)}</b> 这一行："
                "agent_task_success = ☐是 ☐否　　business_invoice_created = ☐是 ☐否</div>"
            )
    parts.append("</body></html>")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"已生成盲标指南：{out}（浏览器打开阅读）")
    return 0


def _load_label_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"第 {number} 行不是合法 JSON：{exc}") from exc
    return rows


def _validate_labels(
    rows: list[dict[str, Any]], dataset: Any, *, allow_self_annotation: bool = False
) -> list[str]:
    """返回问题列表；空列表表示可以进入报告步骤。"""
    problems: list[str] = []
    expected = {(case.case_id, run.run_id) for case in dataset.cases for run in case.runs}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row.get("case_id")), str(row.get("run_id")))
        if key not in expected:
            problems.append(f"标注里出现数据中不存在的 run：{key}")
            continue
        if key in seen:
            problems.append(f"同一 run 被标注多次：{key}")
            continue
        seen.add(key)
        if not str(row.get("annotator") or "").strip():
            problems.append(f"{key} 缺少 annotator")
        if row.get("annotator_independent") is not True and not allow_self_annotation:
            problems.append(f"{key} 未声明 annotator_independent=true（第二标注者要求）")
        if row.get("annotator_independent") is True and allow_self_annotation:
            problems.append(f"{key} 同时出现 indep=true 与 --allow-self-annotation，声明自相矛盾")
        try:
            validate_label_fields(row, where=f"{key[0]}/{key[1]}")
        except LabelFormatError as exc:
            problems.append(str(exc))
    missing = expected - seen
    if missing:
        problems.append(f"有 {len(missing)} 条运行未标注：{sorted(missing)[:5]}")
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    cases = Path(args.cases)
    labels = Path(args.labels)
    freeze = Path(args.freeze)
    if not cases.is_file() or not labels.is_file():
        print("错误：数据文件或标注文件不存在", file=sys.stderr)
        return 2
    guard_error, freeze_warnings = _freeze_guard(cases, freeze)
    for warning in freeze_warnings:
        print(f"注意：{warning}", file=sys.stderr)
    if guard_error:
        print(f"错误：{guard_error}", file=sys.stderr)
        return 2
    dataset = load_dataset(cases)
    problems = _validate_labels(_load_label_rows(labels), dataset,
                                allow_self_annotation=args.allow_self_annotation)
    if problems:
        for problem in problems:
            print(f"❌ {problem}", file=sys.stderr)
        return 3
    total_runs = sum(len(case.runs) for case in dataset.cases)
    print(f"✅ 标注完整且标注者独立声明合规（{len(dataset.cases)} Case / {total_runs} 运行）")
    return 0


# ------------------------------------------------------------------ 报告

def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson 95% 置信区间；total=0 返回 None（不伪造 0%）。"""
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def _labels_mapping(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for row in rows:
        case = mapping.setdefault(row["case_id"], {
            "case_id": row["case_id"],
            "run_assessments": [],
            "version_direction_on_this_case": row.get("version_direction_on_this_case") or None,
        })
        case["run_assessments"].append({
            "run_id": row["run_id"],
            "agent_task_success": bool(row["agent_task_success"]),
            "business_invoice_created": bool(row["business_invoice_created"]),
            "root_causes": row.get("root_causes") or [],
        })
    return mapping


def cmd_finish(args: argparse.Namespace) -> int:
    """一键：CSV → labels.jsonl → check → report（供双击脚本调用）。"""
    import_code = cmd_import_csv(argparse.Namespace(
        csv=args.csv, out=args.labels, annotator=args.annotator,
        self_annotation=args.allow_self_annotation,
    ))
    if import_code != 0:
        return import_code
    check_code = cmd_check(argparse.Namespace(
        cases=args.cases, labels=args.labels, freeze=args.freeze,
        allow_self_annotation=args.allow_self_annotation,
    ))
    if check_code != 0:
        return check_code
    return cmd_report(argparse.Namespace(
        cases=args.cases, labels=args.labels, freeze=args.freeze,
        out=args.out, allow_self_annotation=args.allow_self_annotation,
    ))


def cmd_report(args: argparse.Namespace) -> int:
    cases = Path(args.cases)
    labels = Path(args.labels)
    guard_error, freeze_warnings = _freeze_guard(cases, Path(args.freeze))
    for warning in freeze_warnings:
        print(f"注意：{warning}", file=sys.stderr)
    if guard_error:
        print(f"错误：{guard_error}" + "（report 不能绕过 check）", file=sys.stderr)
        return 2
    dataset = load_dataset(cases)
    rows = _load_label_rows(labels)
    problems = _validate_labels(rows, dataset,
                                allow_self_annotation=args.allow_self_annotation)
    if problems:
        for problem in problems:
            print(f"❌ {problem}", file=sys.stderr)
        return 3
    has_root_causes = any(row.get("root_causes") for row in rows)
    result = evaluate_dataset(dataset)
    metrics = score_against_labels(result, _labels_mapping(rows))
    values = metrics.values
    per_run = metrics.per_run

    def pct(name: str) -> str:
        value = values.get(name)
        return "—" if value is None else f"{value:.2%}"

    scored_runs = [run for run in per_run if run.get("scored")]
    total_scored = len(scored_runs)
    task_ok = sum(1 for run in scored_runs if run.get("task_success") == run.get("gold_task_success"))
    invoice_ok = sum(1 for run in scored_runs if run.get("invoice") == run.get("gold_invoice"))
    unknown = int(values.get("runs_unknown") or 0)
    total_runs = total_scored + unknown

    def ci_row(label: str, ok: int) -> str:
        interval = wilson(ok, total_scored)
        if interval is None:
            return f"| {label} | 0/0 | — | — |"
        return (f"| {label} | {ok}/{total_scored} | {ok / total_scored:.2%} | "
                f"[{interval[0]:.1%}, {interval[1]:.1%}] |")

    report_lines = [
        "# Holdout 独立标注评测报告（第二标注者）",
        "",
        f"- 数据：`{cases.name}`　SHA-256 `{_sha256(cases)}`",
        f"- 冻结清单：`{args.freeze}`",
        f"- 评测器：`{EVALUATOR_VERSION}`　规则集：`{RULE_VERSION}`",
        f"- 标注文件：`{labels.name}`（SHA-256 `{_sha256(labels)}`）",
        f"- 规模：{values.get('cases_scored')} Case / {total_scored} 可评估运行 / {unknown} UNKNOWN",
        "",
        ("> ⚠️ **自我标注模式（--allow-self-annotation）**：标注者并非独立第三方，"
         "本结果**不能用于评审加分**，只能作为内部一致性检查；若需独立证据请换第二标注者重做。"
         if args.allow_self_annotation else ""),
        "> **结论边界**：这批 holdout 只用于检验\"规则是否只在训练集上成立\"这一具体问题；",
        "> 样本量小（见 Wilson 区间宽度），**不据此宣称隐藏集泛化能力**，也不与外部基准比较。",
        "",
        "## 预注册指标",
        "",
        "| 指标 | 命中/分母 | 值 | Wilson 95% CI |",
        "|---|---|---|---|",
        ci_row("任务判定一致率（评测器 vs 独立标注）", task_ok),
        ci_row("开票状态一致率（评测器 vs 独立标注）", invoice_ok),
        (f"| 首因事件 EM | {values.get('first_fault_event_scored')} | "
         f"{pct('first_fault_event_exact_match')} | — |" if has_root_causes
         else "| 首因事件 EM | — | 未标注（N/A） | — |"),
        (f"| 根因标签 macro-F1 | — | {pct('root_cause_label_macro_f1')} | — |" if has_root_causes
         else "| 根因标签 macro-F1 | — | 未标注（N/A） | — |"),
        f"| 证据 ID 有效率 | — | {pct('evidence_id_validity')} | — |",
        (f"| UNKNOWN 比例 | {unknown}/{total_runs} | {unknown / total_runs:.2%} |"
         if total_runs else "| UNKNOWN 比例 | 0/0 | — |"),
        f"| labels_unmatched | — | {values.get('labels_unmatched')} | — |",
        "",
        "## 不能得出的结论",
        "",
        "- 不能把这里的任何比例当作隐藏测试集精度（n 小、且 holdout 与训练集同源）；",
        "- 不能据此声称\"优于某个外部工具\"——本仓库没有可比的公开开票轨迹基准；",
        "- 若标注过程中改过规则，必须换一批未看过的 holdout 重做（冻结哈希会拒绝旧数据）。",
        "",
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    markdown = out / "holdout-report.md"
    markdown.write_text("\n".join(report_lines), encoding="utf-8")
    payload = {"values": values, "per_run": per_run, "labels_sha256": _sha256(labels),
               "data_sha256": _sha256(cases)}
    (out / "holdout-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"产出：{markdown}　{out / 'holdout-report.json'}")
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="holdout", description="独立 holdout 冻结/模板/校验/报告")
    sub = parser.add_subparsers(dest="command", required=True)

    p_freeze = sub.add_parser("freeze", help="冻结数据哈希与运行清单（不复制数据原文）")
    p_freeze.add_argument("--cases", required=True)
    p_freeze.add_argument("--out", default=str(FREEZE_DEFAULT))
    p_freeze.add_argument("--force", action="store_true",
                          help="允许覆盖已有冻结清单（换新一批 holdout 时使用）")
    p_freeze.set_defaults(func=cmd_freeze)

    p_tpl = sub.add_parser("template", help="生成第二标注者用的空白标注文件")
    p_tpl.add_argument("--cases", required=True)
    p_tpl.add_argument("--out", default=str(TEMPLATE_DEFAULT))
    p_tpl.set_defaults(func=cmd_template)

    p_check = sub.add_parser("check", help="校验数据未被改动、标注完整、标注者声明独立")
    p_check.add_argument("--cases", default=str(DEFAULT_HOLDOUT_CASES))
    p_check.add_argument("--labels", default=str(LABELS_DEFAULT))
    p_check.add_argument("--freeze", default=str(FREEZE_DEFAULT))
    p_check.add_argument("--allow-self-annotation", action="store_true",
                         help="允许自我标注（annotator_independent=false）；结果不可用于评审加分")
    p_check.set_defaults(func=cmd_check)

    p_show = sub.add_parser("show", help="打印原始轨迹（盲标用，不显示评测器结论）")
    p_show.add_argument("--cases", required=True)
    p_show.add_argument("--case", default="", help="只看某个 Case（可选）")
    p_show.add_argument("--run", default="", help="只看某个 Run（可选）")
    p_show.set_defaults(func=cmd_show)

    p_ws = sub.add_parser("worksheet", help="生成一页式盲标 worksheet（只含轨迹）")
    p_ws.add_argument("--cases", required=True)
    p_ws.add_argument("--out", default=str(ROOT / ".tmp" / "holdout-worksheet.md"))
    p_ws.set_defaults(func=cmd_worksheet)

    p_guide = sub.add_parser("guide", help="生成 HTML 盲标指南（轨迹 + 判断方法）")
    p_guide.add_argument("--cases", default=str(DEFAULT_HOLDOUT_CASES))
    p_guide.add_argument("--out", default=str(ROOT / ".tmp" / "holdout-guide.html"))
    p_guide.set_defaults(func=cmd_guide)

    p_csv = sub.add_parser("csv", help="导出 Excel 可填的 CSV 标注表")
    p_csv.add_argument("--cases", default=str(DEFAULT_HOLDOUT_CASES))
    p_csv.add_argument("--out", default=str(ROOT / "docs" / "holdout" / "labels.csv"))
    p_csv.add_argument("--force", action="store_true", help="允许覆盖已填写的 CSV")
    p_csv.set_defaults(func=cmd_csv)

    p_import = sub.add_parser("import-csv", help="把 Excel 填好的 CSV 转成 labels.jsonl")
    p_import.add_argument("--csv", default=str(ROOT / "docs" / "holdout" / "labels.csv"))
    p_import.add_argument("--out", default=str(ROOT / "docs" / "holdout" / "labels.jsonl"))
    p_import.add_argument("--annotator", default="team-lead")
    p_import.add_argument("--self-annotation", action="store_true",
                          help="自我标注（annotator_independent=false）")
    p_import.set_defaults(func=cmd_import_csv)

    p_finish = sub.add_parser("finish", help="一键：CSV→标注文件→校验→报告（双击脚本用）")
    p_finish.add_argument("--cases", default=str(DEFAULT_HOLDOUT_CASES))
    p_finish.add_argument("--csv", default=str(ROOT / "docs" / "holdout" / "labels.csv"))
    p_finish.add_argument("--labels", default=str(ROOT / "docs" / "holdout" / "labels.jsonl"))
    p_finish.add_argument("--freeze", default=str(FREEZE_DEFAULT))
    p_finish.add_argument("--annotator", default="team-lead")
    p_finish.add_argument("--allow-self-annotation", action="store_true")
    p_finish.add_argument("--out", default=str(ROOT / "out" / "holdout"))
    p_finish.set_defaults(func=cmd_finish)

    p_report = sub.add_parser("report", help="生成预注册指标 + Wilson CI 报告")
    p_report.add_argument("--cases", default=str(DEFAULT_HOLDOUT_CASES))
    p_report.add_argument("--labels", default=str(LABELS_DEFAULT))
    p_report.add_argument("--freeze", default=str(FREEZE_DEFAULT))
    p_report.add_argument("--allow-self-annotation", action="store_true",
                          help="允许自我标注（annotator_independent=false）；结果不可用于评审加分")
    p_report.add_argument("--out", default=str(ROOT / "out" / "holdout"))
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
