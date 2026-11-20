"""规则引擎：执行注册表、去重与"无法解释的失败"兜底。"""

from __future__ import annotations

from . import (
    rules_business,  # noqa: F401  # 注册顺序 1：R001–R008
    rules_control,  # noqa: F401  # 注册顺序 2：R009–R015、R902
)
from .contracts import Case
from .rule_defs import RULE_VERSION, RULES, Violation, _ev
from .state import BusinessState
from .suppression import Suppression, apply_suppressions


def evaluate_rules(state: BusinessState, case: Case, enabled: frozenset[str] | None = None) -> list[Violation]:
    """跑规则并做证据驱动的后果抑制、去重、按 seq 排序。

    `enabled` 为 None 表示启用全部规则；传入规则 ID 集合用于消融实验（只跑子集）。
    """
    return evaluate_with_trace(state, case, enabled)[0]


def evaluate_with_trace(
    state: BusinessState, case: Case, enabled: frozenset[str] | None = None
) -> tuple[list[Violation], list[Suppression]]:
    raw: list[Violation] = []
    for spec in RULES:
        if enabled is not None and spec.rule_id not in enabled:
            continue
        if spec.check is None:
            raise RuntimeError(f"规则 {spec.rule_id} 未绑定断言函数")
        raw += spec.check(state, case)

    survivors, suppressed = apply_suppressions(raw, state, case)
    return _dedupe(survivors), suppressed


def _dedupe(violations: list[Violation]) -> list[Violation]:
    """同一根因标签只保留**最早**的可证实事件（首因稳定）。

    多事件同标签时保留最早那条，其余折叠为该标签的后续表现，
    既稳定"首因"定义，也避免同一问题被重复计分。
    """
    best: dict[str, Violation] = {}
    for v in violations:
        cur = best.get(v.label)
        if cur is None or v.seq < cur.seq:
            best[v.label] = v
    return sorted(best.values(), key=lambda v: (v.seq, v.label))


def unexplained_failure(state: BusinessState, case: Case) -> Violation | None:
    """终态偏离但确定性规则未能定位：显式表达不确定性，不编造根因。

    题内**没有**可接受终态时，本运行没有任何可对照的判断依据——这与
    `attribution.assess_run` 的 terminal_only 分支保持一致，必须给 R900 UNKNOWN，
    绝不能因为"没东西可比"就默认 PASS。
    """
    acceptable = case.reference_result.get("acceptable_snapshot_state")
    if not acceptable:
        return Violation(
            rule_id="R900", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
            event_id=None, seq=10**9, evidence_ids=(),
            explanation="题内未给出可接受终态，无法判断运行是否正确。",
            dimension="PROCESS", points=20.0, confidence=0.2,
        )
    if state.final_state == acceptable:
        return None
    event = state.terminals[-1] if state.terminals else (state.run.events[-1] if state.run.events else None)
    ids, seq = _ev(state, event)
    return Violation(
        rule_id="R900", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            f"终态为 {state.final_state}，题内可接受终态为 {acceptable}；"
            f"但现有可观测事件不足以确定性定位根因，按证据不足处理。"
        ),
        dimension="PROCESS", points=20.0, confidence=0.3,
    )


DECISION_POLICY = (
    "hard-constraints-first：业务强约束由确定性规则判定；"
    "模型仅在规则无法覆盖时提供语义辅助，且不得覆盖确定性结论。"
)
