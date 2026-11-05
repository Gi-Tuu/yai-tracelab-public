"""业务硬约束规则（R001–R008）：金额口径/计算、字段完整性、消歧、商品、明细、任务类型、取消。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import Case, Event
from .rule_defs import RULE_VERSION, RuleSpec, Violation, _ask_required, _ev, rule
from .state import MONEY_UNEQUAL, BusinessState, money_compare, round_money


@rule(RuleSpec(
    rule_id="R001",
    label="AMOUNT_BASIS",
    dimension="BUSINESS_RESULT",
    points=30.0,
    title="含税/不含税口径必须保持用户明确指定的口径",
    precondition="reference_result.amount_basis 非空且 COLLECT 事件给出了 amount_basis",
    assertion="state.amount_basis == reference.amount_basis",
))
def check_amount_basis(state: BusinessState, case: Case) -> list[Violation]:
    expected = case.reference_result.get("amount_basis")
    actual = state.amount_basis
    if not expected or actual is None:
        return []
    if str(actual) == str(expected):
        return []
    event = state.collect_event
    downstream = state.submits[-1] if state.submits else None
    ids, seq = _ev(state, event, downstream)
    return [Violation(
        rule_id="R001", rule_version=RULE_VERSION, label="AMOUNT_BASIS",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            f"用户约定口径为 {expected}，Agent 实际按 {actual} 解释金额 {state.amount_value}，"
            f"导致税额与含税金额随之偏移。"
        ),
        dimension="BUSINESS_RESULT", points=30.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R002",
    label="AMOUNT_CALCULATION",
    dimension="BUSINESS_RESULT",
    points=30.0,
    title="口径正确时金额与税额必须按题内规则计算",
    precondition="R001 未触发，且 reference_result.expected_amounts 与 state.computed_amounts 均有值",
    assertion="computed_amounts 三项均与 expected_amounts 在 ±0.005 内一致",
))
def check_amount_calculation(state: BusinessState, case: Case) -> list[Violation]:
    expected = case.reference_result.get("expected_amounts")
    if not isinstance(expected, Mapping):
        return []
    actual = state.computed_amounts
    if not actual:
        actual = state.submit_amounts
    if not actual:
        return []
    # 只用"确定不等"的字段作为违规依据；无法判断（缺失/非法）不算违规，也不静默通过
    diffs = {
        k: (round_money(v), round_money(actual.get(k)))
        for k, v in expected.items()
        if v is not None and money_compare(actual.get(k), v) == MONEY_UNEQUAL
    }
    if not diffs:
        return []
    event = state.collect_event
    ids, seq = _ev(state, event, state.submits[-1] if state.submits else None)
    detail = "；".join(f"{k} 应为 {exp}，实际 {got}" for k, (exp, got) in diffs.items())
    return [Violation(
        rule_id="R002", rule_version=RULE_VERSION, label="AMOUNT_CALCULATION",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=f"金额口径正确，但计算与题内配置税率不符：{detail}。",
        dimension="BUSINESS_RESULT", points=30.0, confidence=1.0,
    )]


def _ask_evidence(state: BusinessState) -> bool:
    """Agent 是否真的把缺失信息交回给用户（而不是自己往下推进）。"""
    if any(e.step_id == "WAIT_USER_SUPPLEMENT" for e in state.ordered_events):
        return True
    return state.final_state == "WAITING_USER"


def _continued_without_asking(
    state: BusinessState, missing: Sequence[str], action: str | None
) -> list[Violation]:
    """题面要求追问，Agent 既没追问也没编造，却直接进了确认/提交。

    这是 R003 的第三个分支：**"没有编造"不等于"处理正确"**。
    缺字段场景下只要 Agent 继续出票（预览或提交），就必须拿出"交回用户"的证据
    （WAIT_USER_SUPPLEMENT 或停在 WAITING_USER），否则同样是对必要信息的凭空推进。
    """
    if _ask_evidence(state):
        return []
    if not (state.confirms or state.submits):
        return []
    fields = ", ".join(missing) if missing else "必要业务字段"
    event = state.confirms[0] if state.confirms else state.submits[0]
    ids, seq = _ev(state, event, state.collect_event)
    return [Violation(
        rule_id="R003", rule_version=RULE_VERSION, label="FIELD_FABRICATION",
        event_id=event.event_id, seq=seq, evidence_ids=ids,
        explanation=(
            f"题面要求先追问（{action or '缺字段'}：{fields}），"
            "Agent 既没有向用户追问，也没有编造具体取值，却直接进入确认/提交阶段；"
            "缺失信息在未被澄清的情况下被继续推进出票。"
        ),
        dimension="PROCESS", points=25.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R003",
    label="FIELD_FABRICATION",
    dimension="PROCESS",
    points=25.0,
    title="必要信息缺失时必须追问，不得自行编造",
    precondition="reference_result.required_action ∈ {ASK_*}，或 context.required_missing_fields 非空",
    assertion="Agent 未在缺字段情况下继续生成预览/提交，且缺失字段未被填上编造值；继续出票必须能拿出'已交回用户'的证据",
))
def check_missing_field(state: BusinessState, case: Case) -> list[Violation]:
    action = _ask_required(case.reference_result)
    missing = case.context.get("required_missing_fields")
    missing = tuple(missing) if isinstance(missing, list) else ()
    if not action and not missing:
        return []
    if action == "ASK_BUYER_DISAMBIGUATION":
        return []  # 由 R004 专门处理
    if action == "ASK_ORIGINAL_INVOICE_AND_AMOUNT":
        return []  # 由 R014 专门处理
    if action == "ASK_AMOUNT_BASIS":
        basis = state.amount_basis
        if basis is None:
            # 没有取定口径也不等于正确：要求追问时继续出票必须能拿出"已交回用户"的证据
            return _continued_without_asking(state, missing or ("amount_basis",), action)
        event = state.collect_event
        ids, seq = _ev(state, event)
        return [Violation(
            rule_id="R003", rule_version=RULE_VERSION, label="FIELD_FABRICATION",
            event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
            explanation=f"用户未说明金额口径，Agent 自行取定 {basis}，属于对必要信息的编造。",
            dimension="PROCESS", points=25.0, confidence=1.0,
        )]
    # ASK_BUYER 或无 required_action 的缺字段场景
    reference_buyer = case.reference_result.get("buyer_id")
    fabricated = state.buyer_id
    if reference_buyer is None and fabricated is not None:
        event = state.collect_event
        ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
        return [Violation(
            rule_id="R003", rule_version=RULE_VERSION, label="FIELD_FABRICATION",
            event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
            explanation=f"购方信息缺失（{list(missing) or ['buyer_id']}），Agent 自行补出购方 {fabricated}。",
            dimension="PROCESS", points=25.0, confidence=1.0,
        )]
    if reference_buyer is not None and fabricated == reference_buyer:
        return []
    if reference_buyer is None and fabricated is None:
        # 题面要求追问，"既没编造也没追问"仍属违规：缺失信息被无声推进
        return _continued_without_asking(state, missing or ("buyer_id",), action)
    return []


@rule(RuleSpec(
    rule_id="R004",
    label="ENTITY_RESOLUTION",
    dimension="BUSINESS_RESULT",
    points=30.0,
    title="存在多个候选购方时必须消歧，不得擅自选择",
    precondition="context.buyer_candidates 多于 1 个，或 reference_result.required_action == ASK_BUYER_DISAMBIGUATION",
    assertion="Agent 输出 buyer_id 为 null 并停在等待用户选择",
))
def check_buyer_disambiguation(state: BusinessState, case: Case) -> list[Violation]:
    candidates = case.context.get("buyer_candidates")
    multiple = isinstance(candidates, list) and len(candidates) > 1
    # 题面显式要求消歧也是触发条件：不能只看 context 里有没有多候选列表
    action_requires = case.reference_result.get("required_action") == "ASK_BUYER_DISAMBIGUATION"
    if not (multiple or case.context.get("buyer_disambiguation_required") or action_requires):
        return []
    chosen = state.buyer_id
    if chosen is None:
        return []
    event = state.collect_event
    ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
    if isinstance(candidates, list) and candidates:
        names_text = str([c.get("buyer_id") for c in candidates])
    else:
        names_text = "题内要求消歧（未给出可选项列表）"
    return [Violation(
        rule_id="R004", rule_version=RULE_VERSION, label="ENTITY_RESOLUTION",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=f"候选购方为 {names_text}，Agent 未消歧直接选定 {chosen}。",
        dimension="BUSINESS_RESULT", points=30.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R005",
    label="PRODUCT_MATCH",
    dimension="BUSINESS_RESULT",
    points=30.0,
    title="商品必须匹配用户描述，相似商品不得混用",
    precondition="reference_result.item_id 与 state.item_id 均有值",
    assertion="state.item_id == reference_result.item_id（批量任务还需逐行校验）",
))
def check_product_match(state: BusinessState, case: Case) -> list[Violation]:
    expected = case.reference_result.get("item_id")
    actual = state.item_id
    if not expected or not actual or str(expected) == str(actual):
        return []
    event = state.collect_event
    ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
    return [Violation(
        rule_id="R005", rule_version=RULE_VERSION, label="PRODUCT_MATCH",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=f"题内目标商品为 {expected}，Agent 匹配到 {actual}。",
        dimension="BUSINESS_RESULT", points=30.0, confidence=1.0,
    )]


def _line_amount_signature(value: Any) -> tuple[str, ...]:
    """明细金额签名（含三态）：委托 `state.money_signature`，与 money_identical 同源。

    非法/缺失/不支持各自带状态，只有原始值也相同时才可能相等，绝不会静默通过。
    """
    from .state import money_signature

    kind, detail = money_signature(value)
    if kind == "amount":
        return ("amount", detail)
    return ("unparsed", detail, repr(value))


def _line_signature(items: Sequence[Mapping[str, Any]]) -> list[tuple[str, tuple[str, ...]]]:
    """明细多重集合签名：排序后逐条比较，条数差异/字段差异都能体现。"""
    return sorted(
        ((str(it.get("item_id")), _line_amount_signature(it.get("exclusive_amount"))) for it in items),
        key=lambda pair: (pair[0], pair[1]),
    )


def _line_item_layers(state: BusinessState) -> list[tuple[str, Event, Sequence[Any]]]:
    """采集 → 确认预览 → 提交参数三层里**实际可观测**的明细。

    只有该层真的存在（阶段事件存在且带了 line_items 字段）才记录，
    避免把"没走到提交阶段"误判成"提交缺明细"。
    """
    layers: list[tuple[str, Event, Sequence[Any]]] = []
    collect = state.collect_event
    if collect is not None:
        items = collect.observed_output.get("line_items")
        if isinstance(items, list):
            layers.append(("信息采集", collect, items))
    confirm = state.confirm_event
    if confirm is not None:
        items = state.preview.get("line_items")
        if isinstance(items, list):
            layers.append(("确认预览", confirm, items))
    submit = state.submit_event
    if submit is not None:
        items = state.submit_args.get("line_items")
        if not isinstance(items, list):
            items = (submit.observed_input or {}).get("line_items")
        if isinstance(items, list):
            layers.append(("提交参数", submit, items))
    return layers


@rule(RuleSpec(
    rule_id="R006",
    label="MULTI_ITEM_OMISSION",
    dimension="BUSINESS_RESULT",
    points=25.0,
    title="批量任务必须保留全部条目，遗漏/合并/重复都要识别",
    precondition="reference_result.expected_line_items 非空",
    assertion="采集 / 确认预览 / 提交参数三层各自的 line_items 都与 expected_line_items 逐条一致（按多重集合比较，缺项与多项都算违规）",
))
def check_batch_lines(state: BusinessState, case: Case) -> list[Violation]:
    expected = case.reference_result.get("expected_line_items")
    if not isinstance(expected, list) or not expected:
        return []
    expected_items = [x for x in expected if isinstance(x, Mapping)]
    if not expected_items:
        return []
    target = _line_signature(expected_items)

    violations: list[Violation] = []
    layers = _line_item_layers(state)
    if not layers:
        # 三层都观测不到明细：题内要求了批量条目，却没有任何可核对证据——按缺项报
        event = state.collect_event
        ids, seq = _ev(state, event, state.confirms[-1] if state.confirms else None)
        violations.append(Violation(
            rule_id="R006", rule_version=RULE_VERSION, label="MULTI_ITEM_OMISSION",
            event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
            explanation=(
                f"题内批量任务应有 {len(expected_items)} 条明细，"
                "但采集 / 预览 / 提交三层都没有给出可核对的 line_items。"
            ),
            dimension="BUSINESS_RESULT", points=25.0, confidence=1.0,
        ))
        return violations

    def _has_undecidable(signature: list[tuple[str, tuple[str, ...]]]) -> bool:
        # 审计 R3-03：任一侧含无法解析的金额时，明细差异不可判定，
        # 不能据此判 Agent 用错金额；由 R902 统一按证据不足处理。
        return any(sig[0] != "amount" for _, sig in signature)

    for where, event, items in layers:
        actual_items = [it for it in items if isinstance(it, Mapping)]
        actual = _line_signature(actual_items)
        if actual == target:
            continue
        if _has_undecidable(actual) or _has_undecidable(target):
            continue
        ids, seq = _ev(state, event, state.collect_event)
        violations.append(Violation(
            rule_id="R006", rule_version=RULE_VERSION, label="MULTI_ITEM_OMISSION",
            event_id=event.event_id, seq=seq, evidence_ids=ids,
            explanation=(
                f"{where}层明细与题内应得 {len(expected_items)} 条不一致："
                f"应为 {target}，实际 {len(items)} 条 {actual}。"
            ),
            dimension="BUSINESS_RESULT", points=25.0, confidence=1.0,
        ))
    return violations


@rule(RuleSpec(
    rule_id="R007",
    label="INTENT_CLASSIFICATION",
    dimension="PROCESS",
    points=40.0,
    title="任务类型（蓝票/红票/批量）必须识别正确",
    precondition="reference_result.invoice_task_type 与 state.task_type 均有值",
    assertion="state.task_type == reference_result.invoice_task_type",
))
def check_task_type(state: BusinessState, case: Case) -> list[Violation]:
    expected = case.reference_result.get("invoice_task_type")
    actual = state.task_type
    if not expected or not actual or str(expected) == str(actual):
        return []
    event = state.parse_event
    ids, seq = _ev(state, event, state.collect_event)
    return [Violation(
        rule_id="R007", rule_version=RULE_VERSION, label="INTENT_CLASSIFICATION",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=f"题内任务类型为 {expected}，Agent 识别为 {actual}。",
        dimension="PROCESS", points=40.0, confidence=1.0,
    )]


@rule(RuleSpec(
    rule_id="R008",
    label="INTENT_CLASSIFICATION",
    dimension="PROCESS",
    points=40.0,
    title="取消类请求必须识别为取消并终止任务",
    precondition="reference_result.required_action == CANCEL 或 acceptable_snapshot_state == CANCELED",
    assertion="出现 CANCEL_TASK 事件，且其后没有任何提交动作",
))
def check_cancel_intent(state: BusinessState, case: Case) -> list[Violation]:
    action = case.reference_result.get("required_action")
    acceptable = case.reference_result.get("acceptable_snapshot_state")
    if action != "CANCEL" and acceptable != "CANCELED":
        return []
    if state.canceled and not state.submits:
        return []
    event = state.parse_event
    ids, seq = _ev(state, event, state.submit_event)
    return [Violation(
        rule_id="R008", rule_version=RULE_VERSION, label="INTENT_CLASSIFICATION",
        event_id=event.event_id if event else None, seq=seq, evidence_ids=ids,
        explanation=(
            f"用户要求取消任务，Agent 将 intent 识别为 {state.intent} 并继续推进到 "
            f"{'提交开票' if state.submits else '非取消流程'}。"
        ),
        dimension="PROCESS", points=40.0, confidence=1.0,
    )]
