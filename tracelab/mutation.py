"""缺陷注入实验：给"回归发现率"造出可控的真值。

任务书要求 P1 指标里有「Case 回归发现率」。公开数据里没有"注入缺陷"的真值，
所以这里**自己造真值**：拿一条处理正确的运行，注入一个已知类型的缺陷，
看评测器能不能命中对应根因标签。

这不是"提高分数"的实验——它对我们是不利的：注入不中就等于承认规则有洞。
因此结果必须原样报告，包括漏检的注入类型。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .attribution import assess_run
from .contracts import Case, Dataset, Event, Run, ToolCall

# ---------------------------------------------------------------- 注入工具

#: 异构格式（generic）里工具字段的别名：raw 重建时必须认识，否则会丢参数
_TOOL_NAME_KEYS = ("name", "tool")
_TOOL_ARGS_KEYS = ("arguments", "args")
_TOOL_RESULT_KEYS = ("result", "resp")


def _first_key(raw: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    return next((k for k in keys if k in raw), None)


def _as_str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rebuild_event(raw: Mapping[str, Any], fallback: Event) -> Event:
    """按 raw 重建 Event：**IR 的类型化字段必须与 raw 一致**（EVAL-08）。

    旧实现只改 raw（`raw["step_status"]="FAILED"`），Event.step_status 仍是
    `SUCCEEDED`，于是"评测器看到的状态"和"注入记录里的 raw"互相矛盾——
    注入实验到底改了什么都无法复核。现在一律从 raw 重建。
    """
    raw_tools = raw.get("tool_calls")
    if isinstance(raw_tools, list):
        tools: list[ToolCall] = []
        for item in raw_tools:
            if not isinstance(item, Mapping):
                continue
            name_key = _first_key(item, _TOOL_NAME_KEYS)
            args_key = _first_key(item, _TOOL_ARGS_KEYS)
            result_key = _first_key(item, _TOOL_RESULT_KEYS)
            attempt_raw = item.get("attempt")
            tools.append(ToolCall(
                name=str(item.get(name_key)) if name_key else "unknown",
                arguments=_as_mapping(item.get(args_key)) if args_key else {},
                result=_as_mapping(item.get(result_key)) if result_key else {},
                attempt=attempt_raw if isinstance(attempt_raw, int)
                and not isinstance(attempt_raw, bool) else None,
                raw=item,
            ))
        tool_calls = tuple(tools)
    else:
        tool_calls = fallback.tool_calls

    return Event(
        event_id=_as_str(raw.get("event_id"), fallback.event_id),
        seq=_as_int(raw.get("seq"), fallback.seq),
        step_id=_as_str(raw.get("step_id"), fallback.step_id),
        module=_as_str(raw.get("module"), fallback.module),
        action=_as_str(raw.get("action"), fallback.action),
        step_status=_as_str(raw.get("step_status"), fallback.step_status),
        result_code=_as_str(raw.get("result_code"), fallback.result_code),
        error_code=raw["error_code"] if "error_code" in raw else fallback.error_code,
        error_message=raw["error_message"] if "error_message" in raw else fallback.error_message,
        retry_count=_as_int(raw.get("retry_count"), fallback.retry_count),
        observed_input=_as_mapping(raw.get("observed_input")) if "observed_input" in raw
        else dict(fallback.observed_input),
        observed_output=_as_mapping(raw.get("observed_output")) if "observed_output" in raw
        else dict(fallback.observed_output),
        tool_calls=tool_calls,
        started_at=_as_str(raw.get("started_at"), fallback.started_at),
        finished_at=_as_str(raw.get("finished_at"), fallback.finished_at),
        duration_ms=_as_int(raw.get("duration_ms"), fallback.duration_ms),
        raw=raw,
    )


def _replace_run_events(run: Run, events: list[Event]) -> Run:
    """替换 run 的事件，并同步 `Run.raw["events"]`（Run 层 IR 与 raw 不打架）。"""
    raw = dict(run.raw)
    if isinstance(raw.get("events"), list):
        raw["events"] = [dict(e.raw) for e in events]
    return replace(run, events=tuple(events), raw=raw)


def _patch_event(run: Run, event_id: str, mutate: Callable[[dict[str, Any]], None]) -> Run:
    """在内存里改一条事件的 raw，并按 raw 重建 Event（IR 与 raw 一致）。"""
    events: list[Event] = []
    for e in run.events:
        if e.event_id != event_id:
            events.append(e)
            continue
        raw = copy.deepcopy(dict(e.raw))
        mutate(raw)
        events.append(_rebuild_event(raw, e))
    return _replace_run_events(run, events)


def _patch_tool_amounts(run: Run, event_id: str, mutate: Callable[[dict[str, Any]], None]) -> Run:
    """改工具调用参数里的金额：ToolCall 与 Event.raw 同步更新，不允许只改一边。"""
    events: list[Event] = []
    for e in run.events:
        if e.event_id != event_id:
            events.append(e)
            continue
        new_tools: list[ToolCall] = []
        for t in e.tool_calls:
            args = copy.deepcopy(dict(t.arguments))
            amounts = args.get("amounts")
            if isinstance(amounts, dict):
                mutate(amounts)
            tool_raw = dict(t.raw)
            args_key = _first_key(tool_raw, _TOOL_ARGS_KEYS) or "arguments"
            tool_raw[args_key] = args
            new_tools.append(replace(t, arguments=args, raw=tool_raw))
        new_raw = copy.deepcopy(dict(e.raw))
        new_raw["tool_calls"] = [dict(t.raw) for t in new_tools]
        events.append(replace(e, tool_calls=tuple(new_tools), raw=new_raw))
    return _replace_run_events(run, events)


@dataclass(frozen=True)
class Injection:
    name: str
    description: str
    expected_label: str
    applicable: Callable[[Case, Run], bool]
    apply: Callable[[Case, Run], tuple[Case, Run]]
    #: 该注入期望评测器"拒判"（UNKNOWN），而不是命中某个具体标签
    expects_unknown: bool = False
    #: 期望在哪些步骤留下传播边（传播路径准确率的真值来自注入本身）
    propagation_steps: tuple[str, ...] = ()


def _first_event(run: Run, step_id: str) -> Event | None:
    return next((e for e in run.events if e.step_id == step_id), None)


def _inject_amount_basis(case: Case, run: Run) -> tuple[Case, Run]:
    ev = _first_event(run, "COLLECT_INVOICE_DATA")
    assert ev is not None

    def mutate(raw: dict[str, Any]) -> None:
        out = raw.setdefault("observed_output", {})
        out["amount_basis"] = "tax_inclusive" if out.get("amount_basis") == "tax_exclusive" else "tax_exclusive"

    return case, _patch_event(run, ev.event_id, mutate)


def _inject_submit_drift(case: Case, run: Run) -> tuple[Case, Run]:
    ev = _first_event(run, "SUBMIT_INVOICE")
    assert ev is not None

    def mutate(amounts: dict[str, Any]) -> None:
        current = amounts.get("inclusive_amount")
        if isinstance(current, (int, float)):
            amounts["inclusive_amount"] = round(float(current) + 10.0, 2)

    return case, _patch_tool_amounts(run, ev.event_id, mutate)


def _inject_callback_misread(case: Case, run: Run) -> tuple[Case, Run]:
    ev = _first_event(run, "WAIT_INVOICE_CALLBACK")
    assert ev is not None

    def mutate(raw: dict[str, Any]) -> None:
        raw["step_status"] = "FAILED"
        raw["error_code"] = "SIM_REJECTED"
        out = raw.setdefault("observed_output", {})
        out["callback_status"] = "failed"
        out["invoice_id"] = None

    patched = _patch_event(run, ev.event_id, mutate)
    # Run 层同样要 IR/raw 一致：终态与开票状态一起改，raw 同步
    patched_raw = {
        **dict(patched.raw),
        "final_state": "SUCCEEDED",
        "snapshot_state": "SUCCEEDED",
        "invoice_created": False,
    }
    return case, replace(
        patched,
        final_state="SUCCEEDED",
        snapshot_state="SUCCEEDED",
        invoice_created=False,
        raw=patched_raw,
    )


def _inject_confirmation_bypass(case: Case, run: Run) -> tuple[Case, Run]:
    """删掉"本次确认"的全部证据：上下文标记 + 轨迹里的确认事件与确认入参。

    C-11 / EVAL-09：旧实现只改 `case.context.user_confirmation_recorded`，
    轨迹**零变化**。而 R010 恰恰直接读这个开关，于是"检出"是构造性必中——
    评测器命中的是我们自己写进去的字段，不是轨迹里的缺陷。
    现在同步删除 CONFIRM_INVOICE 事件并清空提交入参里的 `user_confirmed`：
    缺陷在轨迹上可见（提交前没有任何确认证据），检出才算真检出。
    """
    new_context = {**dict(case.context), "user_confirmation_recorded": False}
    new_case = replace(case, context=new_context, raw={**dict(case.raw), "context": new_context})

    events: list[Event] = []
    for e in run.events:
        if e.step_id == "CONFIRM_INVOICE":
            continue  # 确认事件整体删除
        if e.step_id == "SUBMIT_INVOICE":
            raw = copy.deepcopy(dict(e.raw))
            observed_input = raw.get("observed_input")
            if isinstance(observed_input, dict):
                observed_input["user_confirmed"] = False
            events.append(_rebuild_event(raw, e))
            continue
        events.append(e)
    return new_case, _replace_run_events(run, events)


def _rounds(case: Case) -> list[Mapping[str, Any]]:
    rounds = case.context.get("conversation_rounds")
    return [r for r in rounds if isinstance(r, Mapping)] if isinstance(rounds, list) else []


def _stale_round(case: Case) -> Mapping[str, Any] | None:
    """找出被后续轮次覆盖掉的旧轮次。"""
    rounds = _rounds(case)
    superseded = {r.get("supersedes_round") for r in rounds if r.get("supersedes_round") is not None}
    for r in rounds:
        if r.get("round") in superseded and r.get("exclusive_amount") is not None:
            return r
    return None


def _inject_context_stale(case: Case, run: Run) -> tuple[Case, Run]:
    stale = _stale_round(case)
    assert stale is not None
    old_value = float(stale["exclusive_amount"])
    rate = 0.06
    items = case.context.get("item_candidates") or []
    if isinstance(items, list) and items and isinstance(items[0], Mapping):
        rate = float(items[0].get("configured_rate") or rate)
    amounts = {
        "exclusive_amount": round(old_value, 2),
        "tax_amount": round(old_value * rate, 2),
        "inclusive_amount": round(old_value * (1 + rate), 2),
    }
    ev = _first_event(run, "COLLECT_INVOICE_DATA")
    assert ev is not None

    def mutate(raw: dict[str, Any]) -> None:
        out = raw.setdefault("observed_output", {})
        out["amount_value"] = old_value
        out["computed_amounts"] = amounts
        out["line_items"] = [{"item_id": out.get("item_id"), "exclusive_amount": old_value}]

    return case, _patch_event(run, ev.event_id, mutate)


def _inject_batch_drop(case: Case, run: Run) -> tuple[Case, Run]:
    ev = _first_event(run, "COLLECT_INVOICE_DATA")
    assert ev is not None

    def mutate(raw: dict[str, Any]) -> None:
        out = raw.setdefault("observed_output", {})
        items = out.get("line_items")
        if isinstance(items, list) and len(items) > 1:
            out["line_items"] = items[:-1]
            out["line_count"] = len(items) - 1

    return case, _patch_event(run, ev.event_id, mutate)


def _line_count(run: Run) -> int:
    ev = _first_event(run, "COLLECT_INVOICE_DATA")
    items = (ev.observed_output.get("line_items") if ev else None) or []
    return len(items) if isinstance(items, list) else 0


def _inject_evidence_removal(case: Case, run: Run) -> tuple[Case, Run]:
    """把采集事件的可观测结果清空：业务事实无从核对 → 评测器应拒判。"""
    ev = _first_event(run, "COLLECT_INVOICE_DATA")
    assert ev is not None

    def mutate(raw: dict[str, Any]) -> None:
        raw["observed_output"] = {}

    return case, _patch_event(run, ev.event_id, mutate)


def _sibling_item(case: Case) -> str | None:
    """从题内商品候选里取一个与目标不同的商品。"""
    expected = case.reference_result.get("item_id")
    candidates = case.context.get("item_candidates") or []
    for item in candidates:
        if isinstance(item, Mapping) and item.get("item_id") and item.get("item_id") != expected:
            return str(item["item_id"])
    return None


def _inject_product_swap(case: Case, run: Run) -> tuple[Case, Run]:
    ev = _first_event(run, "COLLECT_INVOICE_DATA")
    assert ev is not None
    other = _sibling_item(case)
    assert other is not None

    def mutate(raw: dict[str, Any]) -> None:
        out = raw.setdefault("observed_output", {})
        out["item_id"] = other
        items = out.get("line_items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            items[0]["item_id"] = other

    return case, _patch_event(run, ev.event_id, mutate)


INJECTIONS: tuple[Injection, ...] = (
    Injection(
        "amount_basis_flip",
        "把不含税口径翻成含税（金额口径理解反）",
        "AMOUNT_BASIS",
        lambda case, run: case.reference_result.get("amount_basis") is not None
        and _first_event(run, "COLLECT_INVOICE_DATA") is not None,
        _inject_amount_basis,
        propagation_steps=("CONFIRM_INVOICE", "SUBMIT_INVOICE"),
    ),
    Injection(
        "submit_argument_drift",
        "预览正确，但提交参数里的含税金额被改动",
        "TOOL_ARGUMENT_ERROR",
        lambda case, run: any(t.name.startswith("invoice.submit") and isinstance(t.arguments.get("amounts"), dict)
                              for e in run.events for t in e.tool_calls),
        _inject_submit_drift,
    ),
    Injection(
        "callback_misread",
        "回调明确失败，但终态仍报成功",
        "CALLBACK_MISINTERPRETATION",
        lambda case, run: _first_event(run, "WAIT_INVOICE_CALLBACK") is not None,
        _inject_callback_misread,
    ),
    Injection(
        "confirmation_bypass",
        "没有本次确认记录，却仍然调用了提交工具",
        "CONFIRMATION_BYPASS",
        lambda case, run: _first_event(run, "SUBMIT_INVOICE") is not None,
        _inject_confirmation_bypass,
    ),
    Injection(
        "context_stale_amount",
        "多轮修改后仍沿用被覆盖的旧金额",
        "CONTEXT_UPDATE",
        lambda case, run: _stale_round(case) is not None
        and _first_event(run, "COLLECT_INVOICE_DATA") is not None,
        _inject_context_stale,
    ),
    Injection(
        "batch_item_drop",
        "批量任务的预览少了一条明细",
        "MULTI_ITEM_OMISSION",
        lambda case, run: _line_count(run) > 1,
        _inject_batch_drop,
    ),
    Injection(
        "product_swap",
        "商品匹配到候选集合里的另一个商品",
        "PRODUCT_MATCH",
        lambda case, run: _sibling_item(case) is not None
        and _first_event(run, "COLLECT_INVOICE_DATA") is not None,
        _inject_product_swap,
        propagation_steps=("CONFIRM_INVOICE", "SUBMIT_INVOICE"),
    ),
    Injection(
        "evidence_removal",
        "关键业务证据缺失（清空采集结果）",
        "INSUFFICIENT_EVIDENCE",
        # 只对"真的用到业务事实"的运行注入（进入确认/提交），否则删证据不影响判断
        lambda case, run: _first_event(run, "COLLECT_INVOICE_DATA") is not None
        and bool(case.reference_result.get("amount_value"))
        and bool(_first_event(run, "CONFIRM_INVOICE") or _first_event(run, "SUBMIT_INVOICE")),
        _inject_evidence_removal,
        expects_unknown=True,
    ),
)


# ------------------------------------------------------------------ 实验

@dataclass
class InjectionResult:
    injection: str
    description: str
    expected_label: str
    tried: int = 0
    detected: int = 0
    #: 注入后轨迹没有任何变化 = 注入器自身的缺陷，既不算检出也不算漏检
    invalid: int = 0
    #: 该注入期望"拒判"（UNKNOWN）
    expects_unknown: bool = False
    #: 传播路径真值统计：
    #: - expected：注入真值里"应当出现传播"的事件数（召回的分母）
    #: - hit / hit_edges：命中的真值事件数 / 命中真值的边数
    #: - edges：产出的**去重后**传播边数（精确率的分母）
    propagation_expected: int = 0
    propagation_hit: int = 0
    propagation_hit_edges: int = 0
    propagation_edges: int = 0
    missed_runs: list[dict[str, Any]] = None  # type: ignore[assignment]
    invalid_runs: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.missed_runs is None:
            self.missed_runs = []
        if self.invalid_runs is None:
            self.invalid_runs = []

    @property
    def effective(self) -> int:
        return self.tried - self.invalid

    @property
    def rate(self) -> float | None:
        return round(self.detected / self.effective, 4) if self.effective else None

    @property
    def propagation_precision(self) -> float | None:
        """边级精确率：命中真值事件的**边数** / 产出去重边数（审计 R2-08）。"""
        return round(self.propagation_hit_edges / self.propagation_edges, 4) if self.propagation_edges else None

    @property
    def propagation_recall(self) -> float | None:
        return round(self.propagation_hit / self.propagation_expected, 4) if self.propagation_expected else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "injection": self.injection,
            "description": self.description,
            "expected_label": self.expected_label,
            "expects_unknown": self.expects_unknown,
            "tried": self.tried,
            "effective": self.effective,
            "invalid": self.invalid,
            "invalid_runs": self.invalid_runs,
            "detected": self.detected,
            "propagation_expected": self.propagation_expected,
            "propagation_hit": self.propagation_hit,
            "propagation_hit_edges": self.propagation_hit_edges,
            "propagation_edges": self.propagation_edges,
            "propagation_precision": self.propagation_precision,
            "propagation_recall": self.propagation_recall,
            "detection_rate": self.rate,
            "missed_runs": self.missed_runs,
        }


def combine(datasets: list[Dataset]) -> Dataset:
    """把多次 --cases 的输入合并成一个数据集（用于跨训练/验证跑注入实验）。"""
    cases: list[Case] = []
    issues = []
    stats = {"lines_total": 0, "cases_parsed": 0, "skipped": 0, "truncated": 0,
             "runs_total": 0, "events_total": 0}
    names = []
    for ds in datasets:
        cases.extend(ds.cases)
        issues.extend(ds.issues)
        names.append(Path(ds.source).name)
        for key in stats:
            stats[key] += int(ds.stats.get(key, 0) or 0)
    import hashlib

    digest = hashlib.sha256("|".join(sorted(ds.sha256 for ds in datasets)).encode()).hexdigest()
    return Dataset(cases=cases, issues=issues, source=" + ".join(names), sha256=digest, stats=stats)


def _state_signature(case: Case, run: Run) -> str:
    """完整状态指纹：事件 + 工具调用 + 终态 + 题内上下文。

    只对 `observed_output` 取指纹是不够的——提交参数漂移改的是 `tool_calls.arguments`，
    确认绕过改的是 `case.context`，回调误读改的是 `run.final_state`，
    都会被误抓成「无效注入」。
    """
    import json as _json

    payload = {
        "context": dict(case.context),
        "final_state": run.final_state,
        "invoice_created": run.invoice_created,
        "events": [
            {
                "id": e.event_id,
                "status": e.step_status,
                "error": e.error_code,
                "in": dict(e.observed_input),
                "out": dict(e.observed_output),
                "tools": [{"n": t.name, "a": dict(t.arguments), "r": dict(t.result)} for t in e.tool_calls],
            }
            for e in run.events
        ],
    }
    return _json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _correct_runs(dataset: Dataset) -> list[tuple[Case, Run]]:
    out: list[tuple[Case, Run]] = []
    for case in dataset.cases:
        for run in case.runs:
            if assess_run(case, run).agent_task_success:
                out.append((case, run))
    return out


def run_injection_study(dataset: Dataset) -> list[InjectionResult]:

    correct = _correct_runs(dataset)
    results: list[InjectionResult] = []
    for injection in INJECTIONS:
        res = InjectionResult(injection.name, injection.description, injection.expected_label,
                              expects_unknown=injection.expects_unknown)
        for case, run in correct:
            if not injection.applicable(case, run):
                continue
            res.tried += 1
            mutated_case, mutated_run = injection.apply(case, run)
            if _state_signature(mutated_case, mutated_run) == _state_signature(case, run):
                # 注入是个空操作——这是注入器的 bug，既不算检出也不算漏检
                res.invalid += 1
                res.invalid_runs.append(run.run_id)
                continue
            assessment = assess_run(mutated_case, mutated_run)

            if injection.expects_unknown:
                # 判定标准：评测器必须**显式标记证据不足**（可能同时还有别的可证实违规）
                hit = "INSUFFICIENT_EVIDENCE" in assessment.labels
            else:
                hit = injection.expected_label in assessment.labels
            if hit:
                res.detected += 1
            else:
                res.missed_runs.append({
                    "case_id": case.case_id,
                    "run_id": run.run_id,
                    "verdict": assessment.verdict.value,
                    "actual_labels": assessment.labels,
                })

            # 传播路径真值来自注入本身：期望出现传播边的步骤是已知的
            if injection.propagation_steps:
                origin_seq = min(
                    (v.seq for v in assessment.violations if v.rule_id != "R902"), default=None
                )
                expected = [
                    e.event_id for e in mutated_run.events
                    if e.step_id in injection.propagation_steps
                    and (origin_seq is None or e.seq > origin_seq)
                ]
                # 同一对 (from,to) 只算一条边；精确率按"命中真值事件的边数"计
                edges = {
                    (edge.from_event, edge.to_event)
                    for v in assessment.violations
                    for edge in assessment.propagation
                    if edge.from_event == v.event_id
                }
                expected_set = set(expected)
                hit_ids = {to for _, to in edges if to in expected_set}
                res.propagation_expected += len(expected_set)
                res.propagation_hit += len(hit_ids)
                res.propagation_hit_edges += len([1 for _, to in edges if to in expected_set])
                res.propagation_edges += len(edges)
        results.append(res)
    return results


def overall(results: list[InjectionResult]) -> dict[str, Any]:
    """总体检出率：分母只算**有效注入**（EVAL-05）。

    无效注入 = 注入后轨迹没有任何变化，属于注入器自身空操作。
    把它算进分母，等于把"注入器没生效"伪装成"规则漏检"，系统性低估发现率；
    逐项 `rate` 一直用 `effective`，总体也必须同口径。
    """
    tried = sum(r.tried for r in results)
    invalid = sum(r.invalid for r in results)
    effective = sum(r.effective for r in results)
    detected = sum(r.detected for r in results)
    return {
        "injections": len(results),
        "tried": tried,
        "invalid": invalid,
        "effective": effective,
        "detected": detected,
        "detection_rate": round(detected / effective, 4) if effective else None,
    }


def build_markdown(results: list[InjectionResult], dataset: Dataset) -> str:
    stats = overall(results)
    rate_text = "—" if stats["detection_rate"] is None else f"{stats['detection_rate']:.1%}"
    lines = [
        "# 缺陷注入实验：回归发现率",
        "",
        "> 公开数据没有「注入缺陷」的真值，所以本实验自己造真值：拿**处理正确**的运行，",
        "> 注入一个已知类型的缺陷，看评测器能否命中对应根因标签。",
        "> 这个实验对我们不利——漏检就是承认规则有洞，因此结果原样报告。",
        "",
        f"数据：`{Path(dataset.source).name}`，{len(dataset)} Case / {sum(len(c.runs) for c in dataset)} 运行",
        f"参与注入的正确运行：{sum(1 for c in dataset.cases for r in c.runs if assess_run(c, r).agent_task_success)} 条",
        "",
        "## 结果",
        "",
        "| 注入类型 | 说明 | 期望标签 | 尝试 | 无效注入 | 有效 | 检出 | 检出率 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        rate = "—" if r.rate is None else f"{r.rate:.1%}"
        lines.append(
            f"| `{r.injection}` | {r.description} | `{r.expected_label}` | {r.tried} | {r.invalid} "
            f"| {r.effective} | {r.detected} | {rate} |"
        )
    lines += [
        "",
        f"**总体检出率：{stats['detected']}/{stats['effective']}（{rate_text}）**",
        f"口径：有效注入 {stats['effective']} 次（无效 {stats['invalid']} 次已排除），"
        f"尝试 {stats['tried']} 次。",
        "",
    ]
    missed = [r for r in results if r.missed_runs]
    if missed:
        lines += ["## 漏检明细（必须原样报告）", ""]
        for r in missed:
            lines.append(f"### `{r.injection}`（漏检 {len(r.missed_runs)} 条）")
            lines.append("")
            lines.append("| run | 实际命中标签 |")
            lines.append("|---|---|")
            for m in r.missed_runs:
                lines.append(f"| `{m['run_id']}` | {', '.join(m['actual_labels']) or '无'} |")
            lines.append("")
    else:
        lines += ["## 漏检明细", "", "无漏检。", ""]
    lines += [
        "## 方法学提醒：注入器本身也要被验证",
        "",
        "首轮跑这个实验时出现过 2 个「漏检」，追查后是**注入器自身的缺陷**，不是规则洞：",
        "",
        "1. 对「用户未指定口径」的 Case 做口径翻转——本来就没有口径可翻，注入是空操作；",
        "2. 商品替换从 `line_items` 里找另一个商品，而正确做法是从 `context.item_candidates` 取兄弟商品；",
        "   结果在没有替换发生的情况下被算成「漏检」。",
        "",
        "因此增加了**无效注入**这一列：注入后轨迹指纹不变即判为无效，既不算检出也不算漏检。",
        f"当前数据下无效注入 {sum(r.invalid for r in results)} 次、注入器 {len(results)} 个"
        f"（其中 {sum(1 for r in results if r.tried == 0)} 个在本批数据上无适用样本）；"
        "无效注入单列展示，既不算检出也不算漏检。",
        "",
        "还有一类更隐蔽的问题：注入只改 `case.context`（`confirmation_bypass` 曾经如此），"
        "轨迹零变化，而规则恰好直接读那个开关——这种「检出」是构造性必中。",
        "现在该注入会同步删除确认事件、清空提交入参里的 `user_confirmed`："
        "缺陷必须在轨迹上可见，检出才算数。",
        "",
        "教训：突变测试必须先证明「突变确实发生了」，否则会把注入 bug 误读成被测系统的缺陷。",
        "",
        "## 如何读这个数字",
        "",
        "- 注入的是**规则覆盖范围内**的缺陷类型，因此高检出率只说明规则实现正确，不说明泛化能力。",
        "- 真正需要注意的是漏检项：它意味着该注入类型在当前状态下无法被发现。",
        "- 本实验不能替代隐藏测试集：注入缺陷是我们自己设计的，天然偏向已被规则覆盖的类型。",
        "",
    ]
    return "\n".join(lines)


def write_study(results: list[InjectionResult], dataset: Dataset, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "mutation.md"
    js = out_dir / "mutation.json"
    md.write_text(build_markdown(results, dataset), encoding="utf-8")
    js.write_text(
        json.dumps({"overall": overall(results), "results": [r.to_dict() for r in results]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return md, js















