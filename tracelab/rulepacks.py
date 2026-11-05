"""G6-C：规则包接口 —— 同一个评测引擎加载不同业务规则。

设计边界（重要）
----------------
本模块**不修改**既有 16 条开票规则（`rules.py` / `state.py` / `attribution.py` 全部保持原样）：

* **Invoice 规则包**通过"委托"实现：`delegate` 直接调用既有 `assess_run`，把结果统一成引擎的输出结构。
  它证明"引擎能承载现有评测器"，同时保证开票判定行为零变化。
* **Expense 规则包**（报销单审核，100% 自造场景，不接真实财务系统）用同一接口**独立实现** 4 条规则，
  自己的事实抽取函数，自己的规则函数，不依赖开票规则。

统一输出：两个包都返回 `PackAssessment`（verdict / labels / quality_score / 逐条 outcome），
因此下游（报告、导出）可以只面向引擎结构，不需要知道是哪个业务。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .attribution import RunAssessment, assess_run
from .contracts import Case, Event, Run, Verdict
from .rules import RULE_VERSION

EXPENSE_RULEPACK_VERSION = "expense-rulepack/1"


# --------------------------------------------------------------------- 输出结构


@dataclass(frozen=True)
class RuleOutcome:
    """单条规则的判定结果（`violated=None` 表示事实不足，无法判定）。"""

    rule_id: str
    label: str
    dimension: str
    points: float
    violated: bool | None
    evidence_ids: tuple[str, ...] = ()
    event_id: str | None = None
    seq: int = 10**9
    explanation: str = ""


@dataclass(frozen=True)
class PackAssessment:
    """引擎的统一输出：与开票评测保持同一套词汇（三态判定 + 标签 + 质量分）。"""

    pack_id: str
    pack_version: str
    scenario: str
    case_id: str
    run_id: str
    verdict: Verdict
    labels: tuple[str, ...]
    quality_score: float
    outcomes: tuple[RuleOutcome, ...] = field(default_factory=tuple)

    @property
    def is_unknown(self) -> bool:
        return self.verdict is Verdict.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "scenario": self.scenario,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "verdict": self.verdict.value,
            "labels": list(self.labels),
            "quality_score": self.quality_score,
            "outcomes": [
                {
                    "rule_id": o.rule_id,
                    "label": o.label,
                    "violated": o.violated,
                    "points": o.points,
                    "event_id": o.event_id,
                    "evidence_ids": list(o.evidence_ids),
                    "explanation": o.explanation,
                }
                for o in self.outcomes
            ],
        }


# --------------------------------------------------------------------- 规则包定义


@dataclass(frozen=True)
class PackRule:
    """一条规则：声明需要哪些事实，然后用纯函数判定"是否违规"。"""

    rule_id: str
    label: str
    dimension: str
    points: float
    required_facts: tuple[str, ...]
    #: True = 违规；False = 合规；None = 事实不足（不应到达，引擎会先判 required_facts）
    evaluate: Callable[[Mapping[str, Any]], bool | None]
    evidence_step: str | None = None


@dataclass(frozen=True)
class Rulepack:
    """一个业务规则包：身份 + 事实抽取 + 规则集合（可选委托给既有评测器）。"""

    pack_id: str
    version: str
    scenario: str
    fact_builder: Callable[[Case, Run], Mapping[str, Any]]
    rules: tuple[PackRule, ...] = ()
    delegate: Callable[[Case, Run], PackAssessment] | None = None


# --------------------------------------------------------------------- 引擎


def _event_for_step(run: Run, step_id: str | None) -> Event | None:
    if step_id is None:
        return None
    found: Event | None = None
    for event in run.events:
        if event.step_id == step_id:
            found = event
    return found


def evaluate_with_pack(pack: Rulepack, case: Case, run: Run) -> PackAssessment:
    """统一评测入口：同一个引擎，不同规则包。"""
    if pack.delegate is not None:
        return pack.delegate(case, run)

    facts = pack.fact_builder(case, run)
    outcomes: list[RuleOutcome] = []
    for rule in pack.rules:
        missing = [name for name in rule.required_facts if facts.get(name) is None]
        if missing:
            outcomes.append(RuleOutcome(
                rule_id=rule.rule_id, label=rule.label, dimension=rule.dimension,
                points=rule.points, violated=None,
                explanation=f"缺少必要事实：{', '.join(missing)}（按证据不足处理）",
            ))
            continue
        violated = rule.evaluate(facts)
        event = _event_for_step(run, rule.evidence_step)
        if violated is None:
            outcomes.append(RuleOutcome(
                rule_id=rule.rule_id, label=rule.label, dimension=rule.dimension,
                points=rule.points, violated=None,
                event_id=event.event_id if event else None, seq=event.seq if event else 10**9,
                explanation="事实存在但无法完成判定（按证据不足处理）",
            ))
            continue
        if violated and event is None:
            # 审计 R3-06 附带项：违规必须能定位到事件；定位不到就只能按证据不足处理，
            # 否则会出现"判了违规、证据为空"的不可复核结论。
            outcomes.append(RuleOutcome(
                rule_id=rule.rule_id, label=rule.label, dimension=rule.dimension,
                points=rule.points, violated=None,
                explanation=f"{rule.rule_id}：检测到疑似违规，但无法定位到证据事件（按证据不足处理）",
            ))
            continue
        outcomes.append(RuleOutcome(
            rule_id=rule.rule_id, label=rule.label, dimension=rule.dimension,
            points=rule.points, violated=violated,
            evidence_ids=(event.event_id,) if event else (),
            event_id=event.event_id if event else None,
            seq=event.seq if event else 10**9,
            explanation=f"{rule.rule_id}：{'违反' if violated else '满足'} {rule.label}",
        ))

    violated_ids = [o.rule_id for o in outcomes if o.violated is True]
    labels = tuple(sorted({o.label for o in outcomes if o.violated is True}))
    if violated_ids:
        verdict = Verdict.FAIL
    elif any(o.violated is None for o in outcomes):
        verdict = Verdict.UNKNOWN
    else:
        verdict = Verdict.PASS
    deducted = sum(o.points for o in outcomes if o.violated is True)
    quality = round(max(0.0, 100.0 - deducted), 1)
    return PackAssessment(
        pack_id=pack.pack_id, pack_version=pack.version, scenario=pack.scenario,
        case_id=case.case_id, run_id=run.run_id, verdict=verdict, labels=labels,
        quality_score=quality, outcomes=tuple(outcomes),
    )


# --------------------------------------------------------------------- Invoice 规则包（委托）


def _invoice_delegate(case: Case, run: Run) -> PackAssessment:
    """把既有开票评测器的结果**原样**映射成引擎结构（不改任何判定）。"""
    assessment: RunAssessment = assess_run(case, run)
    outcomes = tuple(
        RuleOutcome(
            rule_id=violation.rule_id, label=violation.label,
            dimension=violation.dimension, points=violation.points,
            violated=True, evidence_ids=tuple(violation.evidence_ids),
            event_id=violation.event_id, seq=violation.seq,
            explanation=violation.explanation,
        )
        for violation in assessment.violations
    )
    return PackAssessment(
        pack_id="invoice",
        pack_version=RULE_VERSION,
        scenario="invoice",
        case_id=case.case_id,
        run_id=run.run_id,
        verdict=assessment.verdict,
        labels=tuple(assessment.labels),
        quality_score=assessment.quality_score,
        outcomes=outcomes,
    )


INVOICE_PACK = Rulepack(
    pack_id="invoice",
    version=RULE_VERSION,
    scenario="invoice",
    fact_builder=lambda case, run: {},      # 委托模式下不使用
    rules=(),
    delegate=_invoice_delegate,
)


# --------------------------------------------------------------------- Expense 规则包（自造场景）


def expense_facts(case: Case, run: Run) -> Mapping[str, Any]:
    """从事件里抽取报销单审核需要的事实（纯读，不改轨迹）。"""
    claim_amount: Any = None
    line_items: Any = None
    budget_remaining: Any = None
    settled_before: Any = None
    settle_seen = False
    approvers: list[str] = []
    for event in run.events:
        output = event.observed_output
        if event.step_id == "RECEIVE_CLAIM":
            claim_amount = output.get("claim_amount")
            line_items = output.get("line_items")
            settled_before = output.get("already_settled")
        elif event.step_id == "CHECK_BUDGET":
            budget_remaining = output.get("budget_remaining")
        elif event.step_id in ("APPROVE_MANAGER", "APPROVE_FINANCE"):
            approvers.append(event.step_id.replace("APPROVE_", ""))
        elif event.step_id == "SETTLE_EXPENSE":
            settle_seen = True
    return {
        "claim_amount": claim_amount,
        "line_items": line_items,
        "budget_remaining": budget_remaining,
        "settled_before": settled_before,
        "settle_seen": settle_seen,
        "approver_sequence": approvers or None,
    }


def _sum_lines(facts: Mapping[str, Any]) -> float | None:
    items = facts.get("line_items")
    if not isinstance(items, list) or not items:
        return None
    total = 0.0
    for item in items:
        if not isinstance(item, Mapping):
            return None
        amount = item.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            return None
        total += float(amount)
    return round(total, 2)


def _exp_amount_matches(facts: Mapping[str, Any]) -> bool | None:
    total = _sum_lines(facts)
    claim = facts.get("claim_amount")
    if total is None or not isinstance(claim, (int, float)) or isinstance(claim, bool):
        return None
    return abs(total - float(claim)) > 0.005


def _exp_budget_ok(facts: Mapping[str, Any]) -> bool | None:
    claim = facts.get("claim_amount")
    budget = facts.get("budget_remaining")
    if not isinstance(claim, (int, float)) or not isinstance(budget, (int, float)):
        return None
    return float(claim) > float(budget)


def _exp_approval_order(facts: Mapping[str, Any]) -> bool | None:
    sequence = facts.get("approver_sequence")
    if not isinstance(sequence, list) or not sequence:
        return None
    expected = ["MANAGER", "FINANCE"]
    return [str(item) for item in sequence] != expected


def _exp_no_duplicate_settlement(facts: Mapping[str, Any]) -> bool | None:
    # 审计 R3-06：没有结算动作时"是否重复结算"根本不成立，不能判 False（=满足）。
    # 返回 None 让引擎按 UNKNOWN 处理；required_facts 同时补 settle_seen，双保险。
    if facts.get("settle_seen") is not True:
        return None
    settled_before = facts.get("settled_before")
    if settled_before is None:
        return None
    return bool(settled_before)


EXPENSE_PACK = Rulepack(
    pack_id="expense",
    version=EXPENSE_RULEPACK_VERSION,
    scenario="expense-reimbursement",
    fact_builder=expense_facts,
    rules=(
        PackRule(
            rule_id="EXP001", label="EXPENSE_AMOUNT_MISMATCH", dimension="BUSINESS_RESULT",
            points=30.0, required_facts=("claim_amount", "line_items"),
            evaluate=_exp_amount_matches, evidence_step="RECEIVE_CLAIM",
        ),
        PackRule(
            rule_id="EXP002", label="EXPENSE_BUDGET_EXCEEDED", dimension="RISK_CONTROL",
            points=40.0, required_facts=("claim_amount", "budget_remaining"),
            evaluate=_exp_budget_ok, evidence_step="CHECK_BUDGET",
        ),
        PackRule(
            rule_id="EXP003", label="EXPENSE_APPROVAL_ORDER", dimension="PROCESS",
            points=25.0, required_facts=("approver_sequence",),
            evaluate=_exp_approval_order, evidence_step="APPROVE_FINANCE",
        ),
        PackRule(
            rule_id="EXP004", label="EXPENSE_DUPLICATE_SETTLEMENT", dimension="RISK_CONTROL",
            points=50.0, required_facts=("settle_seen", "settled_before"),
            evaluate=_exp_no_duplicate_settlement, evidence_step="SETTLE_EXPENSE",
        ),
    ),
)


#: 已注册的规则包（README 中区分"已实现"与"仅预留"）
RULEPACKS: dict[str, Rulepack] = {
    INVOICE_PACK.pack_id: INVOICE_PACK,
    EXPENSE_PACK.pack_id: EXPENSE_PACK,
}


def evaluate_case_with(pack_id: str, case: Case, run: Run) -> PackAssessment:
    """按包名评测（CLI/实验的公共入口）。"""
    try:
        pack = RULEPACKS[pack_id]
    except KeyError as exc:  # pragma: no cover - 入口参数校验
        raise KeyError(f"未知规则包：{pack_id}（可用：{', '.join(sorted(RULEPACKS))}）") from exc
    return evaluate_with_pack(pack, case, run)
