"""生成**本项目自造**的示例数据（官方格式），供公开镜像使用。

重要：这份数据不是赛事资料包，也不从资料包复制任何一条记录。
它只是"按同样的字段结构自己造几条"，用来让公开镜像能跑通 official adapter 的完整链路。

用法：
    python scripts/make_sample_dataset.py --out data/sample

产出（确定性、可复现，无随机数）：
    data/sample/sample-traces.jsonl    4 个 Case / 8 条运行
    data/sample/README.md              明确声明这是自造示例数据
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SELLER = "SELLER-SAMPLE-01"
RATE = 0.06


def money(value: float) -> float:
    return round(value + 1e-9, 2)


def amounts(exclusive: float) -> dict[str, float]:
    return {
        "exclusive_amount": money(exclusive),
        "tax_amount": money(exclusive * RATE),
        "inclusive_amount": money(exclusive * (1 + RATE)),
    }


def resolve_amounts(basis: str, value: float) -> dict[str, float]:
    """按口径把用户给定的金额还原成三项金额。"""
    if basis == "tax_inclusive":
        exclusive_part = value / (1 + RATE)
        result = amounts(exclusive_part)
        result["inclusive_amount"] = money(value)
        return result
    return amounts(value)


def event(
    event_id: str,
    seq: int,
    step_id: str,
    *,
    status: str = "SUCCEEDED",
    code: str = "E100",
    error: str | None = None,
    out: dict[str, Any] | None = None,
    inp: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "task_id": f"TASK-{event_id.rsplit('-', 1)[0]}",
        "trace_id": f"TRACE-{event_id.rsplit('-', 1)[0]}",
        "seq": seq,
        "step_id": step_id,
        "module": step_id,
        "action": step_id,
        "step_status": status,
        "started_at": f"2026-09-01T10:00:{seq:02d}.000+08:00",
        "finished_at": f"2026-09-01T10:00:{seq:02d}.500+08:00",
        "duration_ms": 50,
        "result_code": code,
        "error_code": error,
        "error_message": None,
        "retry_count": retries,
        "observed_input": inp or {},
        "observed_output": out or {},
        "tool_calls": tools or [],
    }


def run(run_id: str, version: str, events: list[dict[str, Any]], *,
        final_state: str, invoice_created: bool, terminal: bool) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent_version": version,
        "task_id": f"TASK-{run_id}",
        "trace_id": f"TRACE-{run_id}",
        "events": events,
        "snapshot_state": final_state,
        "final_state": final_state,
        "workflow_terminal": terminal,
        "invoice_created": invoice_created,
        "resource_usage": {"total_latency_ms": 500, "input_tokens": 300,
                           "output_tokens": 120, "tool_calls": len(events)},
    }


def base_context(*, buyer: str = "CUST-SAMPLE-01", confirm: bool = False,
                 items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "seller_id": SELLER,
        "buyer_candidates": [{"buyer_id": buyer, "display_name": f"示例客户 {buyer}",
                              "profile_complete": True}],
        "item_candidates": items or [{"item_id": "ITEM-SAMPLE-1",
                                      "display_name": "示例服务费", "configured_rate": RATE}],
        "user_confirmation_recorded": confirm,
        "sandbox_rule_set": "SAMPLE-RULES-1.0",
    }


def flow(prefix: str, *, exclusive: float, basis: str = "tax_exclusive",
         buyer: str = "CUST-SAMPLE-01", confirm: bool = True, task_type: str = "BLUE",
         stop_at_confirm: bool = False, submit: bool = True,
         callback: str = "success", final: str = "SUCCEEDED",
         invoice: bool = True) -> tuple[list[dict[str, Any]], str, bool]:
    """构造一条完整/半程轨迹。"""
    amts = resolve_amounts(basis, exclusive)
    seq = 0
    events: list[dict[str, Any]] = []

    def add(step: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal seq
        seq += 1
        events.append(event(f"{prefix}-{seq:02d}", seq, step, **kwargs))
        return events[-1]

    add("RECEIVE_REQUEST", inp={"user_request": "示例请求"})
    add("CREATE_TASK", out={"task_status": "E100"})
    add("RESOLVE_ENTERPRISE", out={"seller_id": SELLER},
        tools=[{"name": "enterprise.resolve", "arguments": {"seller_id": SELLER},
                "result": {"found": True}}])
    add("PARSE_INTENT", out={"intent": "ISSUE", "invoice_task_type": task_type, "confidence": 0.9})
    add("COLLECT_INVOICE_DATA", out={
        "buyer_id": buyer, "item_id": "ITEM-SAMPLE-1", "amount_value": exclusive,
        "amount_basis": basis, "computed_amounts": amts,
        "line_count": 1, "line_items": [{"item_id": "ITEM-SAMPLE-1",
                                         "exclusive_amount": amts["exclusive_amount"]}],
        "original_invoice_id": None, "missing_fields": [],
    }, tools=[{"name": "customer.lookup", "arguments": {"buyer_query": buyer},
               "result": {"query": buyer}}])
    add("VALIDATE_DATA", out={"validation_passed": True, "risk_flags": [],
                              "confirmation_required": True, "create_allowed": confirm})
    if stop_at_confirm:
        add("CONFIRM_INVOICE", status="WAITING_USER",
            out={"invoice_confirmed": False,
                 "preview": {"buyer_id": buyer, "item_id": "ITEM-SAMPLE-1",
                             "invoice_task_type": task_type, "amounts": amts}})
        return events, "WAITING_USER", False
    add("CONFIRM_INVOICE", out={"invoice_confirmed": True,
                               "preview": {"buyer_id": buyer, "item_id": "ITEM-SAMPLE-1",
                                           "invoice_task_type": task_type, "amounts": amts}})
    if not submit:
        return events, "WAITING_USER", False
    add("SUBMIT_INVOICE", out={"submit_status": "accepted", "request_id": f"REQ-{prefix}"},
        inp={"amounts": amts, "user_confirmed": confirm},
        tools=[{"name": "invoice.submit", "attempt": 1,
                "arguments": {"amounts": amts, "idempotency_key": f"KEY-{prefix}"},
                "result": {"submit_status": "accepted"}}])
    if callback == "failed":
        add("WAIT_INVOICE_CALLBACK", status="FAILED", error="SIM_REJECTED",
            out={"callback_status": "failed", "invoice_id": None},
            tools=[{"name": "invoice.callback", "arguments": {"request_id": f"REQ-{prefix}"},
                    "result": {"status": "failed", "error_code": "SIM_REJECTED"}}])
    else:
        add("WAIT_INVOICE_CALLBACK", out={"callback_status": "success",
                                          "invoice_id": f"SIM-INV-{prefix}"},
            tools=[{"name": "invoice.callback", "arguments": {"request_id": f"REQ-{prefix}"},
                    "result": {"status": "success"}}])
    if final == "SUCCEEDED":
        add("COMPLETE_TASK", code="E200", out={"user_message": "完成", "invoice_created": invoice})
    else:
        add("FAIL_TASK", code="E400", out={"user_message": "失败", "invoice_created": invoice})
    return events, final, invoice


def case(case_id: str, scenario: str, request: str, context: dict[str, Any],
         reference: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "scenario": scenario,
        "user_request": request,
        "context": context,
        "reference_result": reference,
        "runs": runs,
        "dataset_version": "sample-1.0",
        "data_origin": "self_authored_sample",
    }


def build() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # S01 正确等待确认
    good, f1, i1 = flow("S01-A", exclusive=1000.0, stop_at_confirm=True)
    bad, f2, i2 = flow("S01-B", exclusive=1000.0, stop_at_confirm=True, basis="tax_inclusive")
    cases.append(case(
        "SAMPLE-01", "明确不含税金额且尚待确认",
        "给示例客户开一张示例服务费专票，不含税 1000 元，先让我确认。",
        base_context(),
        {"invoice_task_type": "BLUE", "buyer_id": "CUST-SAMPLE-01", "item_id": "ITEM-SAMPLE-1",
         "amount_value": 1000.0, "amount_basis": "tax_exclusive",
         "expected_amounts": amounts(1000.0), "acceptable_snapshot_state": "WAITING_USER",
         "invoice_should_exist": False},
        [run("SAMPLE-01-A", "baseline-v1.0", good, final_state=f1, invoice_created=i1,
             terminal=False),
         run("SAMPLE-01-B", "candidate-v1.1", bad, final_state=f2, invoice_created=i2,
             terminal=False)],
    ))

    # S02 两版都正确开票
    a2, fa, ia = flow("S02-A", exclusive=1060.0, basis="tax_inclusive")
    b2, fb, ib = flow("S02-B", exclusive=1060.0, basis="tax_inclusive")
    cases.append(case(
        "SAMPLE-02", "明确含税金额且已确认",
        "给示例客户开示例服务费专票，含税 1060 元，本次已确认。",
        base_context(confirm=True),
        {"invoice_task_type": "BLUE", "buyer_id": "CUST-SAMPLE-01", "item_id": "ITEM-SAMPLE-1",
         "amount_value": 1060.0, "amount_basis": "tax_inclusive",
         "expected_amounts": amounts(1000.0), "acceptable_snapshot_state": "SUCCEEDED",
         "invoice_should_exist": True},
        [run("SAMPLE-02-A", "baseline-v1.0", a2, final_state=fa, invoice_created=ia, terminal=True),
         run("SAMPLE-02-B", "candidate-v1.1", b2, final_state=fb, invoice_created=ib, terminal=True)],
    ))

    # S03 回调失败却被报成功（候选版退化）
    a3, fa3, ia3 = flow("S03-A", exclusive=2600.0, callback="failed", final="FAILED", invoice=False)
    b3, fb3, ib3 = flow("S03-B", exclusive=2600.0, callback="failed", final="SUCCEEDED", invoice=False)
    cases.append(case(
        "SAMPLE-03", "开票回调明确失败",
        "给示例客户开示例服务费专票，不含税 2600 元，本次已确认。",
        base_context(confirm=True),
        {"invoice_task_type": "BLUE", "buyer_id": "CUST-SAMPLE-01", "item_id": "ITEM-SAMPLE-1",
         "amount_value": 2600.0, "amount_basis": "tax_exclusive",
         "expected_amounts": amounts(2600.0), "acceptable_snapshot_state": "FAILED",
         "invoice_should_exist": False, "required_action": "REPORT_CALLBACK_FAILURE"},
        [run("SAMPLE-03-A", "baseline-v1.0", a3, final_state=fa3, invoice_created=ia3, terminal=True),
         run("SAMPLE-03-B", "candidate-v1.1", b3, final_state=fb3, invoice_created=ib3, terminal=True)],
    ))

    # S04 未确认仍提交（候选版退化，工具正确拦截）
    a4, fa4, ia4 = flow("S04-A", exclusive=1900.0, confirm=False, stop_at_confirm=True)
    b4, fb4, ib4 = flow("S04-B", exclusive=1900.0, confirm=False, final="FAILED", invoice=False)
    # 把 B 的提交步骤改成被拒绝
    for e in b4:
        if e["step_id"] == "SUBMIT_INVOICE":
            e["step_status"] = "FAILED"
            e["error_code"] = "CONFIRMATION_REQUIRED"
            e["error_message"] = "示例接口拒绝未确认的提交"
            e["observed_output"] = {"submit_status": "rejected", "request_id": None}
            e["tool_calls"][0]["result"] = {"submit_status": "rejected", "request_id": None}
    fb4 = "FAILED"
    cases.append(case(
        "SAMPLE-04", "未经确认不得提交",
        "给示例客户开示例服务费专票，不含税 1900 元，先展示内容，我确认后再开。",
        base_context(confirm=False),
        {"invoice_task_type": "BLUE", "buyer_id": "CUST-SAMPLE-01", "item_id": "ITEM-SAMPLE-1",
         "amount_value": 1900.0, "amount_basis": "tax_exclusive",
         "expected_amounts": amounts(1900.0), "acceptable_snapshot_state": "WAITING_USER",
         "invoice_should_exist": False},
        [run("SAMPLE-04-A", "baseline-v1.0", a4, final_state=fa4, invoice_created=ia4, terminal=False),
         run("SAMPLE-04-B", "candidate-v1.1", b4, final_state=fb4, invoice_created=ib4, terminal=True)],
    ))

    return cases


SAMPLE_README = """# 自造示例数据（非赛事资料包）

本目录数据由 `scripts/make_sample_dataset.py` **确定性生成**，不包含、也不复制赛事主办方提供的任何记录。

- 用途：让公开镜像能跑通 `official` 适配器的完整链路（评测 → 提交文件 → 报告）
- 规模：4 个 Case / 8 条运行
- 覆盖场景：正确等待确认、正常开票、回调失败被误报成功、未确认仍提交（工具正确拦截）
- 标识符均为本项目自有命名（`CUST-SAMPLE-*` / `ITEM-SAMPLE-*` / `SELLER-SAMPLE-01`）
- 数据版本：`sample-1.0`，`data_origin=self_authored_sample`

> 因此：本目录数据的任何指标**都不能**代表赛事数据上的表现。
"""


def _guard_out_path(out: Path) -> str | None:
    """`--out` 会被直接写入 README/JSONL：先挡住"绝不能覆盖"的目标（审计 F-17）。

    旧实现只 `mkdir(parents=True)` 就写文件，`--out .` 会静默覆盖仓库 README、
    `--out data/official/student-1.0` 会覆盖主办方原件（不可恢复）。
    """
    resolved = Path(os.path.realpath(str(out)))
    root = Path(os.path.realpath(str(ROOT)))
    home = Path(os.path.realpath(str(Path.home())))
    if resolved == root:
        return "输出目录不能是项目根目录（会覆盖 README.md）"
    if root.is_relative_to(resolved):
        return f"输出目录不能是项目根目录的祖先：{resolved}"
    if resolved == home or home.is_relative_to(resolved):
        return f"输出目录不能是用户主目录或其祖先：{resolved}"
    if resolved.parent == resolved:
        return f"输出目录不能是文件系统根：{resolved}"
    official = root / "data" / "official"
    if resolved == official or resolved.is_relative_to(official):
        return "输出目录不能落在官方资料包 data/official 内（会覆盖主办方原件）"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="生成自造示例数据（官方格式）")
    parser.add_argument("--out", default="data/sample")
    parser.add_argument("--force", action="store_true", help="允许覆盖输出目录里已存在的 README.md")
    args = parser.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    guard_error = _guard_out_path(out)
    if guard_error:
        print(f"错误：{guard_error}；未写入任何文件。", file=sys.stderr)
        return 2
    if out.exists() and not out.is_dir():
        # 审计 R4-19：`--out` 指向已存在的文件时干净报错（旧实现抛 FileExistsError 栈回溯）。
        print(f"错误：输出路径已存在且不是目录：{out}；未写入任何文件。", file=sys.stderr)
        return 2
    readme = out / "README.md"
    if readme.exists() and not args.force:
        print(
            f"错误：{readme} 已存在，拒绝静默覆盖（确认要覆盖请加 --force）；未写入任何文件。",
            file=sys.stderr,
        )
        return 2
    out.mkdir(parents=True, exist_ok=True)

    cases = build()
    path = out / "sample-traces.jsonl"
    path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8"
    )
    readme.write_text(SAMPLE_README, encoding="utf-8")
    print(f"wrote {path}（{len(cases)} Case / {sum(len(c['runs']) for c in cases)} 运行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

