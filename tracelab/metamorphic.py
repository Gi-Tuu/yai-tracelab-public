"""Metamorphic evaluation：评测器自身的稳定性检验。

两个方向
--------
* **语义保持（invariance）**：只改与业务事实无关的东西（事件编号、无关元数据、描述文本、
  金额的等价字符串写法、事件数组顺序、字段键顺序），判断必须**完全不变**。
* **业务语义变化（sensitivity）**：只改一个业务事实（确认、口径、金额、匹配、条目、意图…），
  必须触发**对应规则**，且**不应牵连**别的规则。

本模块只做两件事：变换 + 比较/统计。判定与规则仍然完全交给 `attribution.assess_run`，
不复制评测逻辑，也不把评测器输出当预期答案（预期写在 `experiments.py` 的声明式变体里）。
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .attribution import RunAssessment, assess_run
from .contracts import Case, Event, Run

#: 金额类字段名：只对这些字段做"等价字符串表示"变换，避免动到非金额数字
_AMOUNT_KEYS = frozenset({"amount_value", "exclusive_amount", "tax_amount", "inclusive_amount"})


# --------------------------------------------------------------------- 基础工具


@dataclass(frozen=True)
class ChangeRecord:
    """一次变换做了什么（保留输入变化记录，便于审计与复现）。"""

    transform: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"transform": self.transform, "detail": dict(self.detail)}


@dataclass(frozen=True)
class TransformedCase:
    case: Case
    record: ChangeRecord
    #: 旧事件编号 → 新事件编号（未改编号时为空）；用于把修复前的首因/证据映射回去比较
    id_map: dict[str, str] = field(default_factory=dict, compare=False)

    def map_event(self, event_id: str | None) -> str | None:
        if event_id is None:
            return None
        return self.id_map.get(event_id, event_id)


#: 事件的哪些字段在 raw 里有同名副本（变更时必须同步，否则导出/复核会读到旧事实）。
_RAW_SYNCED_FIELDS = (
    "event_id", "seq", "step_id", "module", "action", "step_status", "result_code",
    "error_code", "error_message", "retry_count", "duration_ms",
    "observed_input", "observed_output", "tool_calls",
)


def _jsonable(value: Any) -> Any:
    """转成可 JSON 序列化的普通结构（审计 R4-18）。

    Decimal/set/bytes 若原样留在 raw 里，导出 `json.dumps` 会抛 TypeError；
    这里统一降级为字符串/列表/表示式，并保持"不丢信息"的语义。
    """
    from dataclasses import asdict, is_dataclass
    from decimal import Decimal

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=str)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    return value


def _rebuild_event(event: Event, **changes: Any) -> Event:
    """重建事件，并**同步 raw**（审计 R3-B-08）。

    `mutation.py` 早已写明"IR 与 raw 不允许只改一边"；而 experiments 的变体与
    metamorphic 的若干变换此前只改 IR，于是 `event.raw` 仍是旧业务事实——
    把变体导出成 JSONL 交给别人复核，得到的会是**未变换**的原轨迹。
    """
    explicit_raw = changes.pop("raw", None)
    base_raw = explicit_raw if explicit_raw is not None else (event.raw or {})
    raw = _jsonable(dict(base_raw))
    for key, value in changes.items():
        if key == "tool_calls":
            # 审计 R4-13：tool_calls 的 raw 必须保持**原始 vendor 形状**，
            # 不能 asdict(ToolCall)（那会把附加键塞进嵌套 raw 并新增 "raw" 键）。
            raw["tool_calls"] = [dict(tool.raw) for tool in value]
        elif key in _RAW_SYNCED_FIELDS or key in raw:
            raw[key] = _jsonable(value)
    return replace(event, raw=raw, **changes)


#: 终态集合（与 adapters 的 workflow_terminal 推导保持同一语义）
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "TRANSFERRED_TO_HUMAN"})


def _rebuild_run(run: Run, **changes: Any) -> Run:
    """重建运行：events 变化时同步 `run.raw["events"]`，其余字段同名同步。

    审计 R4-A-09：`snapshot_state` / `workflow_terminal` 是由 `final_state` 派生的
    （适配器就是这样填的）。调用方只改 `final_state` 时，这里按同一规则重算派生字段，
    否则 IR 与 raw 会同时出现"终态变了、快照/终态标记没变"的不一致。
    """
    if "final_state" in changes:
        final_state = str(changes["final_state"])
        changes.setdefault("snapshot_state", final_state)
        changes.setdefault("workflow_terminal", final_state in _TERMINAL_STATES)
    explicit_raw = changes.pop("raw", None)
    raw = _jsonable(dict(explicit_raw if explicit_raw is not None else (run.raw or {})))
    for key, value in changes.items():
        if key == "events":
            events = tuple(value)
            raw["events"] = [_jsonable(dict(event.raw)) for event in events]
        elif key in _RAW_SYNCED_RUN_FIELDS or key in raw:
            # 审计 R4-18：不只 4 个字段——raw 里有的键（trace_id/task_id/...）只要变了就同步。
            raw[key] = _jsonable(value)
    return replace(run, raw=raw, **changes)


_RAW_SYNCED_CASE_FIELDS = (
    "case_id", "scenario", "user_request", "context", "reference_result",
    "dataset_version", "data_origin",
)

_RAW_SYNCED_RUN_FIELDS = (
    "run_id", "agent_version", "final_state", "invoice_created",
    "snapshot_state", "workflow_terminal", "task_id", "trace_id",
)


def _rebuild_case(case: Case, **changes: Any) -> Case:
    """重建 Case，并同步 `case.raw`（审计 R4-06）。

    `Case.raw` 是官方原始记录、也是仓库里既有的导出范式；只同步 event/run 级
    raw 的话，"按 case.raw 导出变体再读回"拿到的仍是**变换前**轨迹。
    """
    explicit_raw = changes.pop("raw", None)
    raw = _jsonable(dict(explicit_raw if explicit_raw is not None else (case.raw or {})))
    for key, value in changes.items():
        if key == "runs":
            runs = tuple(value)
            raw["runs"] = [_jsonable(dict(run.raw)) for run in runs]
        elif key in _RAW_SYNCED_CASE_FIELDS or key in raw:
            raw[key] = _jsonable(value)
    return replace(case, raw=raw, **changes)


#: 公开别名：其它模块（experiments）也走同一套"IR + raw 同步"实现。
rebuild_event = _rebuild_event
rebuild_run = _rebuild_run
rebuild_case = _rebuild_case


def _map_values(value: Any, fn: Callable[[str, Any], Any]) -> Any:
    """递归重建映射/序列，对叶子调用 ``fn(key, value)``（key 为所在字段名）。"""
    if isinstance(value, Mapping):
        return {key: _map_values(item, fn) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_map_values(item, fn) for item in value)
    if isinstance(value, list):
        return [_map_values(item, fn) for item in value]
    return fn("", value)


def _map_money_fields(value: Any, fn: Callable[[str, Any], Any]) -> Any:
    """只对金额字段的叶子调用 ``fn``，其余原样递归。"""
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and (key in _AMOUNT_KEYS or key.endswith("_amount")):
                out[key] = fn(key, item) if not isinstance(item, (Mapping, list, tuple)) else _map_money_fields(item, fn)
            else:
                out[key] = _map_money_fields(item, fn)
        return out
    if isinstance(value, tuple):
        return tuple(_map_money_fields(item, fn) for item in value)
    if isinstance(value, list):
        return [_map_money_fields(item, fn) for item in value]
    return value


# --------------------------------------------------------------------- 语义保持变换


def rename_event_ids(case: Case) -> TransformedCase:
    """把所有事件编号换成本质无关的新编号（顺序编号），业务事实不变。"""
    id_map: dict[str, str] = {}
    runs: list[Run] = []
    for run in case.runs:
        events: list[Event] = []
        for index, event in enumerate(run.events, start=1):
            new_id = f"EV-{index:04d}"
            id_map[event.event_id] = new_id
            raw = dict(event.raw)
            raw["event_id"] = new_id
            events.append(_rebuild_event(event, event_id=new_id, raw=raw))
        runs.append(_rebuild_run(run, events=tuple(events)))
    new_case = _rebuild_case(case, runs=tuple(runs))
    return TransformedCase(
        case=new_case,
        record=ChangeRecord("rename_event_ids", {"events_renamed": len(id_map)}),
        id_map=id_map,
    )


def add_unrelated_metadata(case: Case) -> TransformedCase:
    """往 raw 里加与业务判断无关的元数据（日志标记、来源标签等）。"""
    runs: list[Run] = []
    for run in case.runs:
        raw = dict(run.raw)
        raw["x_unrelated"] = {"ingest": "metamorphic", "note": "not used by rules"}
        events = []
        for event in run.events:
            event_raw = dict(event.raw)
            event_raw["x_unrelated_trace"] = "noise"
            events.append(_rebuild_event(event, raw=event_raw))
        runs.append(_rebuild_run(run, events=tuple(events), raw=raw))
    case_raw = dict(case.raw)
    case_raw["x_unrelated_case"] = "noise"
    return TransformedCase(
        case=_rebuild_case(case, runs=tuple(runs), raw=case_raw),
        record=ChangeRecord("add_unrelated_metadata", {"added_keys": 3}),
    )


def edit_descriptive_fields(case: Case) -> TransformedCase:
    """改写不参与判定的描述性文本（场景名 / 用户请求措辞）。"""
    return TransformedCase(
        case=_rebuild_case(case, scenario=f"{case.scenario}-renamed", user_request=f"{case.user_request}（措辞改写）"),
        record=ChangeRecord("edit_descriptive_fields", {"fields": ["scenario", "user_request"]}),
    )


def money_equivalent_representation(case: Case) -> TransformedCase:
    """把金额的数值表示改成数学等价的字符串（``1000.0`` → ``"1000.00"``）。

    审计 R3-B-04：旧实现用 ``f"{float(v):.2f}"``（二进制浮点 + 银行家舍入），
    在 1000.005 这类半分边界上会**改动金额**（甚至同一笔金额两个字段舍入方向相反），
    把评测器的真实差异淹没。现在只有"量化到分后与原值完全相等"才转换；
    其余（3 位小数/半分边界）保持原样——语义保持变换绝不能改业务事实。
    """

    def to_text(_key: str, value: Any) -> Any:
        from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        try:
            exact = Decimal(str(value))
            rounded = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return value
        if exact != rounded:
            return value
        return f"{rounded:.2f}"

    runs: list[Run] = []
    changed = 0
    for run in case.runs:
        events: list[Event] = []
        for event in run.events:
            observed_in = _map_money_fields(event.observed_input, to_text)
            observed_out = _map_money_fields(event.observed_output, to_text)
            tools = tuple(
                replace(
                    tool,
                    arguments=_map_money_fields(tool.arguments, to_text),
                    result=_map_money_fields(tool.result, to_text),
                )
                for tool in event.tool_calls
            )
            if observed_in != dict(event.observed_input) or observed_out != dict(event.observed_output):
                changed += 1
            events.append(_rebuild_event(
                event,
                observed_input=observed_in,
                observed_output=observed_out,
                tool_calls=tools,
            ))
        runs.append(_rebuild_run(run, events=tuple(events)))
    return TransformedCase(
        case=_rebuild_case(case, runs=tuple(runs)),
        record=ChangeRecord("money_equivalent_representation", {"events_touched": changed}),
    )


def reorder_event_array(case: Case, *, seed: int = 20261104) -> TransformedCase:
    """只打乱事件在数组里的先后（``seq`` 与依赖关系保持不变）。"""
    rng = random.Random(seed)
    runs: list[Run] = []
    for run in case.runs:
        events = list(run.events)
        rng.shuffle(events)
        runs.append(_rebuild_run(run, events=tuple(events)))
    return TransformedCase(
        case=_rebuild_case(case, runs=tuple(runs)),
        record=ChangeRecord("reorder_event_array", {"seed": seed}),
    )


def reorder_mapping_keys(case: Case) -> TransformedCase:
    """只颠倒映射的键顺序（内容不变）。"""

    def reverse_mapping(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: reverse_mapping(value[key]) for key in reversed(list(value.keys()))}
        if isinstance(value, tuple):
            return tuple(reverse_mapping(item) for item in value)
        if isinstance(value, list):
            return [reverse_mapping(item) for item in value]
        return value

    runs: list[Run] = []
    for run in case.runs:
        events = []
        for event in run.events:
            events.append(_rebuild_event(
                event,
                observed_input=reverse_mapping(event.observed_input),
                observed_output=reverse_mapping(event.observed_output),
            ))
        runs.append(_rebuild_run(run, events=tuple(events)))
    return TransformedCase(
        case=_rebuild_case(case, runs=tuple(runs), context=reverse_mapping(case.context),
                     reference_result=reverse_mapping(case.reference_result)),
        record=ChangeRecord("reorder_mapping_keys", {"note": "只改键顺序"}),
    )


#: 全部语义保持变换（名字 → 函数）
INVARIANCE_TRANSFORMS: dict[str, Callable[[Case], TransformedCase]] = {
    "rename_event_ids": rename_event_ids,
    "add_unrelated_metadata": add_unrelated_metadata,
    "edit_descriptive_fields": edit_descriptive_fields,
    "money_equivalent_representation": money_equivalent_representation,
    "reorder_event_array": reorder_event_array,
    "reorder_mapping_keys": reorder_mapping_keys,
}


# --------------------------------------------------------------------- 结果比较


@dataclass(frozen=True)
class InvarianceOutcome:
    case_id: str
    transform: str
    consistent: bool
    diffs: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "transform": self.transform,
            "consistent": self.consistent,
            "diffs": dict(self.diffs),
            "record": dict(self.record),
        }


def _evidence_set(assessment: RunAssessment, id_map: Mapping[str, str]) -> frozenset[str]:
    return frozenset(id_map.get(event_id, event_id) for v in assessment.violations for event_id in v.evidence_ids)


def compare_invariant(
    before: RunAssessment,
    after: RunAssessment,
    *,
    id_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """比较"语义保持变换"前后的判定；返回差异字典（空 = 完全一致）。"""
    mapping = dict(id_map or {})
    diffs: dict[str, Any] = {}
    if before.verdict is not after.verdict:
        diffs["verdict"] = {"before": before.verdict.value, "after": after.verdict.value}
    if before.business_invoice_created != after.business_invoice_created:
        diffs["business_invoice_created"] = {
            "before": before.business_invoice_created, "after": after.business_invoice_created,
        }
    if set(before.labels) != set(after.labels):
        diffs["labels"] = {"before": sorted(before.labels), "after": sorted(after.labels)}
    if before.quality_score != after.quality_score:
        diffs["quality_score"] = {"before": before.quality_score, "after": after.quality_score}
    if set(_evidence_set(before, mapping)) != set(_evidence_set(after, mapping)):
        diffs["evidence_ids"] = {
            "before": sorted(_evidence_set(before, mapping)),
            "after": sorted(_evidence_set(after, mapping)),
        }
    before_first = before.first_error.event_id if before.first_error else None
    after_first = after.first_error.event_id if after.first_error else None
    mapped_before_first = mapping.get(before_first, before_first) if before_first is not None else None
    if mapped_before_first != after_first:
        diffs["first_fault_event_id"] = {"before": before_first, "after": after_first}
    return diffs


@dataclass
class InvarianceReport:
    outcomes: list[InvarianceOutcome] = field(default_factory=list)
    seed: int = 20261104
    #: 审计 R3-B-06：没有运行、无法检验的 Case（旧实现静默跳过，分母悄悄缩小）。
    skipped: list[dict[str, str]] = field(default_factory=list)
    #: 语料里的 Case 总数（含被跳过的）
    cases_total: int = 0

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def consistent(self) -> int:
        return sum(1 for item in self.outcomes if item.consistent)

    @property
    def inconsistent(self) -> int:
        return self.total - self.consistent

    @property
    def consistency_ratio(self) -> float | None:
        return round(self.consistent / self.total, 4) if self.total else None

    @property
    def cases_tested(self) -> int:
        return self.cases_total - len(self.skipped)

    @property
    def by_transform(self) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}
        for item in self.outcomes:
            bucket = stats.setdefault(item.transform, {"total": 0, "consistent": 0})
            bucket["total"] += 1
            bucket["consistent"] += int(item.consistent)
        return stats

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "cases_total": self.cases_total,
            "cases_tested": self.cases_tested,
            "skipped": list(self.skipped),
            "consistent": self.consistent,
            "inconsistent": self.inconsistent,
            "consistency_ratio": self.consistency_ratio,
            "seed": self.seed,
            "by_transform": self.by_transform,
            "outcomes": [item.as_dict() for item in self.outcomes],
        }


def run_invariance(
    cases: Sequence[Case],
    *,
    transforms: Iterable[str] | None = None,
    seed: int = 20261104,
) -> InvarianceReport:
    """对每个 Case 施加每个语义保持变换，比较判定是否完全一致。"""
    names = list(transforms) if transforms is not None else list(INVARIANCE_TRANSFORMS)
    report = InvarianceReport(seed=seed)
    report.cases_total = len(cases)
    for case in cases:
        run = case.runs[0] if case.runs else None
        if run is None:
            report.skipped.append({"case_id": case.case_id, "reason": "runs 为空，无可检验运行"})
            continue
        baseline = assess_run(case, run)
        for name in names:
            transform = INVARIANCE_TRANSFORMS[name]
            if name == "reorder_event_array":
                transformed = reorder_event_array(case, seed=seed)
            else:
                transformed = transform(case)
            target_run = transformed.case.runs[0]
            after = assess_run(transformed.case, target_run)
            diffs = compare_invariant(baseline, after, id_map=transformed.id_map)
            report.outcomes.append(InvarianceOutcome(
                case_id=case.case_id,
                transform=name,
                consistent=not diffs,
                diffs=diffs,
                record=transformed.record.as_dict(),
            ))
    return report


def cases_digest(cases: Sequence[Case]) -> str:
    """对样本集合做**内容**摘要（便于复现性断言与"语料被改过"检测）。

    审计 R3-B-02：旧实现只摘要 case_id/run_id/agent_version/final_state/事件三元组，
    把 reference、context、观察输入输出、状态码、重试、工具调用等**真正决定判定**的字段
    全部漏掉——业务事实改光、判定 PASS→FAIL→UNKNOWN，摘要一字不变。
    现在按规范化 IR（含 raw 里的业务字段）做 canonical JSON 再 sha256。
    """
    payload = []
    for case in cases:
        payload.append({
            "case_id": case.case_id,
            "scenario": case.scenario,
            "user_request": case.user_request,
            "context": case.context,
            "reference_result": case.reference_result,
            "runs": [
                {
                    "run_id": run.run_id,
                    "agent_version": run.agent_version,
                    "final_state": run.final_state,
                    "invoice_created": run.invoice_created,
                    "events": [
                        {
                            "event_id": event.event_id,
                            "seq": event.seq,
                            "step_id": event.step_id,
                            "module": event.module,
                            "action": event.action,
                            "step_status": event.step_status,
                            "result_code": event.result_code,
                            "error_code": event.error_code,
                            "error_message": event.error_message,
                            "retry_count": event.retry_count,
                            "duration_ms": event.duration_ms,
                            "observed_input": event.observed_input,
                            "observed_output": event.observed_output,
                            "tool_calls": [
                                {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                    "result": call.result,
                                    "attempt": call.attempt,
                                }
                                for call in event.tool_calls
                            ],
                        }
                        for event in run.events
                    ],
                }
                for run in case.runs
            ],
        })
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()