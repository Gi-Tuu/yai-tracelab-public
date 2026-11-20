"""后果抑制：声明式抑制规则表 + 证据驱动的抑制算法（审计「规则文件体量」拆分）。

与业务规则定义分离：本模块只回答"哪些违规是上游错误的后果"，
不定义任何业务断言。抑制必须同时满足：
1. 父违规更早（或同一事件上的同点观测）；
2. 前置条件能在本次运行里找到真实依赖证据；
3. 找不到证据就不抑制——独立发生的错误必须保留。

`tracelab/rules.py` 仍负责规则定义与引擎编排，本模块由它导入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .contracts import Case, Event
from .state import BusinessState, money_eq

if TYPE_CHECKING:  # 避免 rule_defs <-> suppression 运行时循环导入
    from .rule_defs import Violation


# --------------------------------------------------------------- 归因编排

@dataclass(frozen=True)
class SuppressionRule:
    """一条"下游后果"抑制关系。

    与旧实现的区别：**不再因为"某个标签出现过"就全局抑制**。
    必须同时满足：
    1. 父违规事件早于子违规事件（seq 严格小于）；
    2. `precondition` 能在本次运行里找到**真实的依赖证据**
       （参数真的带过去了 / 题内任务本身就是被误判的那个任务）；
    3. 找不到证据就不抑制——独立发生的错误必须保留。
    """

    parent_rule_id: str
    child_label: str
    reason: str
    precondition: Callable[[Violation, Violation, BusinessState, Case], str | None] = field(repr=False)


@dataclass(frozen=True)
class Suppression:
    """一次被抑制的记录，用于报告与测试断言。"""

    child_label: str
    child_event_id: str | None
    parent_label: str
    parent_event_id: str | None
    parent_rule_id: str
    reason: str
    evidence: str


def _carries_task_type(
    parent: Violation, child: Violation, state: BusinessState, expected_task_type: str | None
) -> str | None:
    """下游参数里是否真的带着被误判的任务类型（传播证据）。

    可复核前置条件（任一不满足即**不抑制**）：
    1. `parent.event_id` 非空：父违规必须落在一个具体事件上；
    2. 承载参数的确认预览 / 提交事件必须存在（`state.confirm_event` / `state.submit_event`）；
    3. 承载事件必须**晚于**父事件：传播方向只能向前；
    4. 承载事件不得晚于子违规：被解释为"后果"的那个动作必须发生在参数已带错之后；
    5. 承载事件里确实写着与误判结果一致的任务类型（关键事实）。
    """
    if parent.event_id is None:
        return None
    if not expected_task_type:
        return None
    payloads: list[tuple[Event, Mapping[str, Any], str]] = []
    confirm = state.confirm_event
    preview = state.preview
    if confirm is not None and isinstance(preview, Mapping):
        payloads.append((confirm, preview, "确认预览"))
    submit = state.submit_event
    args = state.submit_args
    if submit is not None and isinstance(args, Mapping):
        payloads.append((submit, args, "提交参数"))
    expected_label = state.case.reference_result.get("invoice_task_type")
    for event, payload, where in payloads:
        if event.event_id is None:
            continue
        # 承载事件必须夹在"父误判之后、子违规之前（含同一事件）"之间
        if not (parent.seq < event.seq <= child.seq):
            continue
        value = payload.get("invoice_task_type")
        if value is None or str(value) != str(expected_task_type):
            continue
        return (
            f"{where}中的 invoice_task_type={value} 与误判结果一致"
            f"（事件 {event.event_id}，题内期望 {expected_label}）"
        )
    return None


def _same_event(parent: Violation, child: Violation, state: BusinessState | None = None, case: Case | None = None) -> str | None:
    """两条违规指向同一个业务事件：说明后一条是同一事实的连带后果。"""
    if parent.event_id and parent.event_id == child.event_id:
        return f"两条违规指向同一事件 {parent.event_id}，属于同一事实的连带判断"
    return None


def _cancel_flow_misread(parent: Violation, child: Violation, state: BusinessState, case: Case) -> str | None:
    """取消任务被误判：其后的提交/重复/重试都是"做错了另一件事"的后果。

    可复核前置条件（任一不满足即**不抑制**）：
    1. `parent.event_id` 非空，且解析事件存在：题面与实跑都要能定位这次误判；
    2. 题内确实要求 CANCEL，而 Agent 没有真的取消；
    3. **Agent 确实推进了流程**：有提交尝试 / 生成过确认预览 / 走到完成终态 /
       已转人工，至少一项成立。否则它只是卡在解析阶段，
       下游的确认绕过、重复控制、回调误判都是独立错误，必须各自保留。
    """
    if parent.event_id is None:
        return None
    if state.parse_event is None:
        return None
    if case.reference_result.get("required_action") != "CANCEL":
        return None
    if state.canceled:
        return None
    # 3) "推进了流程"的可复核事实：evidence 里点名具体是哪一条，
    #    报告复核时不需要二次推断。
    advanced: list[str] = []
    if state.submit_attempts > 0:
        advanced.append(f"发起提交 {state.submit_attempts} 次")
    if state.preview:
        confirm = state.confirm_event
        advanced.append(f"生成确认预览（事件 {confirm.event_id if confirm else '未知'}）")
    if state.completed:
        advanced.append("以 COMPLETE_TASK 结束")
    if state.transferred:
        advanced.append("转人工")
    if not advanced:
        return None
    return (
        "题内要求取消，Agent 未取消反而继续推进任务（"
        + "；".join(advanced)
        + "），后续动作属于误判任务的执行后果"
    )


def _stale_value_propagated(parent: Violation, child: Violation, state: BusinessState, case: Case) -> str | None:
    """多轮旧值进入计算：金额计算错误是旧值传播的结果。

    可复核前置条件（任一不满足即**不抑制**）：
    1. `parent.event_id` 非空，且信息采集事件存在（旧值只能从这里进入计算）；
    2. `context.conversation_rounds` 里确实存在**被后续轮次覆盖**的旧轮次，
       且该轮次带可用金额；
    3. Agent 实际使用的金额非空，且计算事件不早于采集事件；
    4. 该金额必须正好等于"被覆盖的旧轮次金额"：匹配到**未被覆盖**的轮次金额
       说明 Agent 用的是有效轮次，不构成旧值传播，不得抑制。
    """
    if parent.event_id is None:
        return None
    collect = state.collect_event
    if collect is None or collect.event_id is None:
        return None
    rounds = case.context.get("conversation_rounds")
    if not isinstance(rounds, list) or not rounds:
        return None
    # 被覆盖的是"旧轮次"：supersedes_round 指向它替代的那一轮
    superseded_rounds = {
        r.get("supersedes_round") for r in rounds
        if isinstance(r, Mapping) and r.get("supersedes_round") is not None
    }
    if not superseded_rounds:
        return None
    stale_rounds = [
        r for r in rounds
        if isinstance(r, Mapping) and r.get("round") in superseded_rounds and r.get("exclusive_amount") is not None
    ]
    if not stale_rounds:
        return None
    used = state.amount_value
    if used is None or child.seq < collect.seq:
        return None
    for stale in stale_rounds:
        if not money_eq(used, stale.get("exclusive_amount")):
            continue
        newer = next(
            (r.get("round") for r in rounds
             if isinstance(r, Mapping) and r.get("supersedes_round") == stale.get("round")),
            None,
        )
        return (
            f"金额 {used} 正是第 {stale.get('round')} 轮被第 {newer} 轮覆盖的旧值"
            f"（采集事件 {collect.event_id}），计算错误由旧值沿用导致"
        )
    return None


def _basis_feeds_calculation(parent: Violation, child: Violation, state: BusinessState, case: Case) -> str | None:
    """口径错误导致计算错误：同一采集事件上金额随口径整体偏移。

    可复核前置条件（任一不满足即**不抑制**）：
    1. `parent.event_id` 非空，且信息采集事件存在（口径只能从采集事件进入计算）；
    2. 题内口径与实跑口径都存在且**确实不一致**（父违规 R001 的关键事实）；
    3. 同事件分支：口径与计算落在同一个采集事件上（同一点观测，允许等序依赖）；
    4. 跨事件 fallback：`state.amount_value` 非空（计算真的发生了），
       且计算事件**晚于**口径采集事件，才谈得上"由这条错误口径导致"。
    """
    if parent.event_id is None:
        return None
    collect = state.collect_event
    if collect is None or collect.event_id is None:
        return None
    expected = case.reference_result.get("amount_basis")
    actual = state.amount_basis
    if not (expected and actual and str(expected) != str(actual)):
        return None
    if _same_event(parent, child) is not None:
        return (
            f"口径 {actual} 与题内 {expected} 不一致，且口径与计算发生在同一采集事件 "
            f"{parent.event_id}，计算错误由口径选择导致"
        )
    # fallback：必须证明"计算真的发生"且"计算晚于口径采集"
    if state.amount_value is None:
        return None
    if child.seq <= collect.seq:
        return None
    return (
        f"口径 {actual} 与题内 {expected} 不一致（采集事件 {collect.event_id}），"
        f"计算事件 {child.event_id} 晚于口径采集，计算错误由口径选择导致"
    )


SUPPRESSION_RULES: tuple[SuppressionRule, ...] = (
    SuppressionRule("R001", "AMOUNT_CALCULATION", "口径错误导致计算错误", _basis_feeds_calculation),
    SuppressionRule("R015", "AMOUNT_CALCULATION", "多轮旧值导致计算错误", _stale_value_propagated),
    # 已删除 R003 → AMOUNT_CALCULATION：R003 的父事件是信息采集事件，而"编造购方"与
    # "税额算错"是两件独立事实。两者恰好都落在同一采集事件上，用 _same_event 做证据
    # 等于"同事件共现即因果"，会让"编造购方 + 税额算错"只剩一条根因（75 分）。
    # 已删除 R007 → TOOL_ARGUMENT_ERROR：`_carries_task_type` 只能证明"提交参数里带着
    # 被误判的任务类型"，无法证明"漂移的金额字段正是被误判类型改写的那一项"，
    # 抑制掉参数漂移会把两类独立错误合并成一条。
    SuppressionRule("R007", "CONFIRMATION_BYPASS", "误判任务类型后继续提交",
                    lambda p, c, s, case: _carries_task_type(p, c, s, s.task_type)),
    SuppressionRule("R008", "CONFIRMATION_BYPASS", "取消任务被误判后继续提交", _cancel_flow_misread),
    SuppressionRule("R008", "DUPLICATE_CONTROL", "取消任务被误判后继续推进", _cancel_flow_misread),
    SuppressionRule("R008", "TOOL_ERROR_HANDLING", "取消任务被误判后继续重试", _cancel_flow_misread),
    SuppressionRule("R008", "CALLBACK_MISINTERPRETATION", "取消任务被误判后继续等待回调", _cancel_flow_misread),
    SuppressionRule("R008", "MULTI_ITEM_OMISSION", "取消任务被误判后继续处理明细", _cancel_flow_misread),
    SuppressionRule("R004", "FIELD_FABRICATION", "消歧失败连带字段判断", _same_event),
)


def _find_suppression_parent(
    raw: list[Violation], child: Violation, alive: set[int], state: BusinessState, case: Case
) -> tuple[Violation, SuppressionRule, str] | None:
    """按声明顺序找 child 的第一个"父更早 + 有证据"的抑制父（审计 B4 拆分）。"""
    child_id = id(child)
    for rule in SUPPRESSION_RULES:
        if rule.child_label != child.label:
            continue
        for parent in raw:
            parent_id = id(parent)
            if parent_id == child_id or parent_id not in alive:
                # 5) 被抑制的父不再作为父
                continue
            if parent.label == child.label:
                continue
            if parent.rule_id != rule.parent_rule_id:
                continue
            # 1) 上游必须先于下游：反向不抑制。同一事件上的两条判断视为同一点观测。
            same_event = parent.event_id is not None and parent.event_id == child.event_id
            if not (parent.seq < child.seq or (same_event and parent.seq == child.seq)):
                continue
            # 2) 必须找到真实依赖证据（每条规则自带可复核前置条件）
            evidence = rule.precondition(parent, child, state, case)
            if not evidence:
                continue
            return parent, rule, evidence
    return None


def _revoke_orphan_suppressions(
    links: dict[int, tuple[int, str]], reasons: dict[int, tuple[str, str]], alive: set[int]
) -> bool:
    """父已被抑制 → 撤销这条子抑制，让子重新成为独立根因；返回是否有变化。"""
    orphans = [child_id for child_id, (parent_id, _) in links.items() if parent_id not in alive]
    if not orphans:
        return False
    for child_id in orphans:
        alive.add(child_id)
        links.pop(child_id, None)
        reasons.pop(child_id, None)
    return True


def apply_suppressions(raw: list[Violation], state: BusinessState, case: Case) -> tuple[list[Violation], list[Suppression]]:
    """按证据做条件抑制。返回（保留下来的违规，抑制记录）。

    确定性约定（同一 child 最多只产生 1 条 `Suppression`）：
    1. 对每个 child **按 `SUPPRESSION_RULES` 的声明顺序**逐条尝试；
    2. 每个规则内部按 raw 顺序找第一个同时满足"父更早 + 有证据"的父违规；
    3. 命中第一条即记录并停止，同一 child 只记声明顺序最靠前的那条；
    4. 反向顺序（父晚于子）与无证据一律不抑制；
    5. **被抑制的父不再作为父**；父被抑制时撤销指向它的子抑制（子恢复为独立根因）。
    """
    alive: set[int] = {id(v) for v in raw}
    links: dict[int, tuple[int, str]] = {}   # child id → (parent id, evidence)
    reasons: dict[int, tuple[str, str]] = {}  # child id → (parent_rule_id, reason)

    while True:
        changed = False
        for child in raw:
            child_id = id(child)
            if child_id not in alive:
                continue
            found = _find_suppression_parent(raw, child, alive, state, case)
            if found is None:
                continue
            parent, rule, evidence = found
            links[child_id] = (id(parent), evidence)
            reasons[child_id] = (parent.rule_id, rule.reason)
            alive.discard(child_id)
            changed = True
        if _revoke_orphan_suppressions(links, reasons, alive):
            changed = True
        if not changed:
            break

    by_id = {id(v): v for v in raw}
    survivors = [v for v in raw if id(v) in alive]
    suppressed: list[Suppression] = []
    for child in raw:                      # 记录顺序 = raw 顺序，稳定可复现
        child_id = id(child)
        if child_id not in links:
            continue
        parent_id, evidence = links[child_id]
        parent = by_id[parent_id]
        parent_rule_id, reason = reasons[child_id]
        suppressed.append(Suppression(
            child_label=child.label, child_event_id=child.event_id,
            parent_label=parent.label, parent_event_id=parent.event_id,
            parent_rule_id=parent_rule_id, reason=reason, evidence=evidence,
        ))
    return survivors, suppressed
