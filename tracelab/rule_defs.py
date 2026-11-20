"""规则定义层：Violation/RuleSpec/注册表与公共小工具（不依赖具体规则）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import Case, Event
from .state import BusinessState

RULE_VERSION = "rulepack-1.3"


# ------------------------------------------------------------------ 数据结构

@dataclass(frozen=True)
class Violation:
    rule_id: str
    rule_version: str
    label: str                 # 官方根因标签
    event_id: str | None       # 首个错误事件
    seq: int
    evidence_ids: tuple[str, ...]
    explanation: str
    dimension: str             # BUSINESS_RESULT | PROCESS | TOOL_CALL | RISK_CONTROL
    points: float
    confidence: float
    certainty: str = "deterministic"   # deterministic | model


@dataclass
class RuleSpec:
    rule_id: str
    label: str
    dimension: str
    points: float
    title: str
    precondition: str
    assertion: str
    check: Callable[[BusinessState, Case], list[Violation]] | None = field(default=None, repr=False)


RULES: list[RuleSpec] = []


def rule(spec: RuleSpec) -> Callable[[Callable[[BusinessState, Case], list[Violation]]], Callable[[BusinessState, Case], list[Violation]]]:
    """把断言函数绑定到规则元数据上，并登记进规则集。

    审计 F6-02：按 `rule_id` 幂等——模块被 `importlib.reload`（热重载/测试隔离）
    时不会重复追加，只更新断言实现并保持注册顺序。
    """
    def deco(fn: Callable[[BusinessState, Case], list[Violation]]):
        spec.check = fn
        existing = next((item for item in RULES if item.rule_id == spec.rule_id), None)
        if existing is None:
            RULES.append(spec)
        else:
            existing.check = fn
        return fn
    return deco


def _ev(state: BusinessState, event: Event | None, *extra: Event | None) -> tuple[tuple[str, ...], int]:
    ids: list[str] = []
    for e in (event, *extra):
        if e is not None and e.event_id not in ids:
            ids.append(e.event_id)
    return tuple(ids), (event.seq if event else 10**9)


def _first(events: Sequence[Event]) -> Event | None:
    return events[0] if events else None


def _last(events: Sequence[Event]) -> Event | None:
    return events[-1] if events else None


def _ask_required(reference: Mapping[str, Any]) -> str | None:
    action = reference.get("required_action")
    return action if isinstance(action, str) and action.startswith("ASK_") else None
