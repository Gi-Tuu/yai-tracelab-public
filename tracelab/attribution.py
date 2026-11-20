"""证据驱动归因层。

职责：把规则产出组织成"可复核的证据链"，并显式区分四类东西：
1. 最早可证实错误（first provable error）
2. 后续传播后果（propagation）
3. 正常工具拦截（normal guard）—— 不是故障
4. 无法确认的异常（uncertainty）

因果关系的建立优先依据**参数流动与业务字段变化**（同一笔金额从预览流到提交参数），
而不是时间戳先后。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .contracts import STEP_IDS, Case, Verdict
from .rules import (
    RULE_VERSION,
    Violation,
    evaluate_with_trace,
    unexplained_failure,
)
from .state import BusinessState, reconstruct
from .suppression import Suppression

GUARD_ERROR_CODES = frozenset({
    "CONFIRMATION_REQUIRED",  # 未确认即提交 → 工具正确拦截
    "DUPLICATE_BLOCKED",
    "RISK_BLOCKED",
})

#: 只有这些"金额本身错了"的标签才允许建立"错误金额流向后续步骤"的传播边。
#: 其它违规（购方编造/商品匹配/重复风控/意图误判…）与金额无关，
#: 给它们套"错误金额"文案等于伪造因果（审计 F-12）。
MONEY_RELATED_LABELS = frozenset({
    "AMOUNT_BASIS",
    "AMOUNT_CALCULATION",
    "CONTEXT_UPDATE",
})


@dataclass(frozen=True)
class PropagationEdge:
    from_event: str
    to_event: str
    why: str


@dataclass(frozen=True)
class Guard:
    event_id: str
    code: str
    reason: str


@dataclass
class RunAssessment:
    case_id: str
    run_id: str
    agent_version: str
    verdict: Verdict
    business_invoice_created: bool
    quality_score: float
    violations: list[Violation] = field(default_factory=list)
    propagation: list[PropagationEdge] = field(default_factory=list)
    guards: list[Guard] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    state: BusinessState | None = None
    #: 因果抑制审计记录：哪条下游违规被哪条上游违规折叠掉、依据什么证据。
    #: 只用于报告复核，不改变违规计分；父必须是最终保留下来的违规（不存在孤儿父）。
    suppressions: list[Suppression] = field(default_factory=list)

    # ---- 派生 ----
    @property
    def agent_task_success(self) -> bool:
        """官方 Schema 字段映射：只有 PASS 为 true；UNKNOWN 也是 false。

        这是**展示层**语义，内部判断一律用 `verdict`，不要用这个属性做推理。
        """
        return self.verdict.to_success_flag()

    @property
    def is_unknown(self) -> bool:
        return self.verdict is Verdict.UNKNOWN

    @property
    def first_error(self) -> Violation | None:
        return self.violations[0] if self.violations else None

    @property
    def labels(self) -> list[str]:
        return [v.label for v in self.violations]

    @property
    def evidence_ids(self) -> list[str]:
        out: list[str] = []
        for v in self.violations:
            for eid in v.evidence_ids:
                if eid not in out:
                    out.append(eid)
        return out

    def penalty_items(self) -> list[dict[str, Any]]:
        items = []
        for v in self.violations:
            items.append({
                "dimension": v.dimension,
                "points_deducted": v.points,
                "evidence_event_ids": list(v.evidence_ids),
                "explanation": f"[{v.rule_id}] {v.explanation}",
            })
        return items


def _build_propagation(state: BusinessState, violation: Violation) -> list[PropagationEdge]:
    """只在**参数真的流动过去**时建立传播边，避免"时间在后即因果"。

    审计 F-12：旧实现不筛违规类型，任何违规（购方编造/商品匹配/重复风控…）都会
    被写成"错误金额流向下游"，等于给证据链编造因果。现在只有金额类违规才允许
    "金额参数传播"边；非金额违规只保留"工具保护性拒绝"这类可核实的事实边。
    """
    edges: list[PropagationEdge] = []
    if not violation.event_id:
        return edges
    origin = next((e for e in state.run.events if e.event_id == violation.event_id), None)
    if origin is None:
        return edges

    if violation.label in MONEY_RELATED_LABELS:
        carriers = state.carries_amounts_detailed(state.preview_amounts or state.computed_amounts)
        for ev, matched in carriers:
            if ev.seq <= origin.seq:
                continue
            fields = "、".join(f"{k}={v}" for k, v in sorted(matched.items()))
            why = f"携带同一组（错误的）金额参数：{fields}"
            if ev.step_id == "SUBMIT_INVOICE":
                why = f"错误金额作为提交参数进入正式开票请求（{fields}）"
            elif ev.step_id == "CONFIRM_INVOICE":
                why = f"错误金额进入用户确认预览（{fields}）"
            elif ev.step_id == "FAIL_TASK":
                why = f"错误被下游以失败终态吸收（{fields}）"
            edges.append(PropagationEdge(violation.event_id, ev.event_id, why))

    # 确认门禁类：提交被拦截是后果而非新根因
    if violation.label in ("CONFIRMATION_BYPASS", "DUPLICATE_CONTROL"):
        for ev in state.events_after(origin):
            if ev.error_code in GUARD_ERROR_CODES:
                edges.append(PropagationEdge(violation.event_id, ev.event_id, "违规请求触发了工具的保护性拒绝"))
                break
    return edges


def _detect_guards(state: BusinessState) -> list[Guard]:
    """识别工具侧的保护性拒绝。

    只认**工具调用发生的那一步**（提交/校验），不把终态步骤回显的同一 error_code 重复计数，
    否则一次拦截会被算成两次。

    审计 F-27：去重键用 `(event_id, code)` 而不是 `code`——两次独立提交各自被同码拦截
    是两次真实防护，不能被折叠成一次；终态回显已由 step_id 过滤掉。
    """
    guards: list[Guard] = []
    seen: set[tuple[str, str]] = set()
    for e in state.run.events:
        if e.step_id in ("FAIL_TASK", "TIMEOUT_TASK", "CANCEL_TASK"):
            continue
        code = e.error_code or ""
        if code in GUARD_ERROR_CODES and (e.event_id, code) not in seen:
            seen.add((e.event_id, code))
            guards.append(Guard(
                event_id=e.event_id, code=code,
                reason={
                    "CONFIRMATION_REQUIRED": "提交前置条件未满足，模拟接口按规则拒绝——属于正确防护，不能记为接口故障",
                    "DUPLICATE_BLOCKED": "重复开票风险被业务规则拦截——属于正确防护",
                    "RISK_BLOCKED": "风险规则拦截——属于正确防护",
                }.get(code, "保护性拒绝"),
            ))
    return guards


def _invoice_created(state: BusinessState) -> tuple[bool, list[str]]:
    """发票是否真正生成：以模拟服务记录为准，并与回调证据交叉验证。"""
    notes: list[str] = []
    created = state.run.invoice_created
    status = state.callback_status
    if created and status is not None and str(status).lower() in ("failed", "rejected", "error"):
        notes.append(
            f"运行记录 invoice_created=true，但回调状态为 {status}；按「以明确回调为准」的规则存在冲突，已保留原始记录待复核。"
        )
    if not created and state.completed and state.final_state == "SUCCEEDED":
        notes.append(
            "Agent 以成功终态结束并对外报告成功，但模拟服务未生成发票（callback/终态声明与实际业务状态不一致）。"
        )
    return created, notes


#: 这两条规则表示"我们没有能力判断"，而不是"Agent 做错了"
UNKNOWN_RULE_IDS = frozenset({"R900", "R901", "R902"})


def _classify(violations: list[Violation]) -> Verdict:
    """把违规列表映射成内部三态。

    顺序很重要：**有可证实违规就是 FAIL**（证据优先）；
    只有证据不足/评测器异常时才是 UNKNOWN——这类情况绝不能被解释成 Agent 退化。
    """
    proven = [v for v in violations if v.rule_id not in UNKNOWN_RULE_IDS]
    if proven:
        return Verdict.FAIL
    if violations:
        return Verdict.UNKNOWN
    return Verdict.PASS


def _ground_violations(violations: list[Violation]) -> list[Violation]:
    """把"命中但指不出证据事件"的违规降级为 UNKNOWN。

    为什么必须做：官方提交 Schema 要求**非 `INSUFFICIENT_EVIDENCE` 的根因必须带
    属于本 run 的 `first_fault_event_id`**（`08_提交格式校验.py:139`），
    否则整份 submission 会被判"根因首个错误事件无效"并整体退回。
    规则在轨迹缺少对应阶段事件（例如没有 `PARSE_INTENT`）时确实拿不到坐标，
    此时它**不构成"可证实违规"**——降级为 R902 比伪造一个事件坐标诚实。
    """
    if not any(v.event_id is None and v.rule_id not in UNKNOWN_RULE_IDS for v in violations):
        return violations
    kept: list[Violation] = []
    downgraded: list[str] = []
    for v in violations:
        if v.event_id is not None or v.rule_id in UNKNOWN_RULE_IDS:
            kept.append(v)
            continue
        downgraded.append(f"{v.rule_id}({v.label})")
    if not downgraded:
        return violations
    # 审计 F-14：规则侧已经给出 R902 时不再追加第二条，否则 root_causes / penalty_items
    # 会出现重复的 INSUFFICIENT_EVIDENCE，报告"N 条根因"被夸大。
    existing = next((v for v in kept if v.label == "INSUFFICIENT_EVIDENCE"), None)
    detail = (
        f"规则 {'、'.join(sorted(set(downgraded)))} 命中了问题特征，但轨迹里没有可定位的证据事件，"
        "无法给出可复核的首因；按证据不足处理，不据此判定 Agent 失败。"
    )
    if existing is not None:
        return [
            replace(v, explanation=f"{v.explanation}；另：{detail}") if v is existing else v
            for v in kept
        ]
    kept.append(
        Violation(
            rule_id="R902",
            rule_version=RULE_VERSION,
            label="INSUFFICIENT_EVIDENCE",
            event_id=None,
            seq=10**9,
            evidence_ids=(),
            explanation=detail,
            dimension="PROCESS",
            points=0.0,
            confidence=0.0,
        )
    )
    return kept


def _intent_consistency_notes(state) -> list[str]:
    """多轮 PARSE_INTENT 且任务类型不一致时给复核提示（审计 F-09 的可见性）。"""
    notes: list[str] = []
    if len(state.analyzes) > 1:
        task_types = {
            event.observed_output.get("invoice_task_type")
            for event in state.analyzes
            if isinstance(event.observed_output, Mapping)
        }
        task_types.discard(None)
        if len(task_types) > 1:
            notes.append(
                "轨迹里有多条 PARSE_INTENT 且任务类型不一致（"
                + "、".join(sorted(str(t) for t in task_types))
                + "）；按契约取 seq 最小的那条判定，若以最新解析为准结论可能不同。"
            )
    return notes


def _terminal_only_assessment(case: Case, run, state, created: bool, notes: list[str]) -> RunAssessment:
    """消融变体专用：只看终态是否等于题内可接受终态（审计 B2 拆分）。"""
    acceptable = case.reference_result.get("acceptable_snapshot_state")
    violations: list[Violation] = []
    if not acceptable:
        # 没有题内可接受终态就没有判断依据——这是 UNKNOWN，不是 Agent 失败
        violations = [Violation(
            rule_id="R900", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
            event_id=None, seq=10**9, evidence_ids=(),
            explanation="题内未给出可接受终态，无法判断运行是否正确。",
            dimension="PROCESS", points=20.0, confidence=0.2,
        )]
        verdict = Verdict.UNKNOWN
    else:
        matched = state.final_state == acceptable
        if not matched:
            violations = [Violation(
                rule_id="R900", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
                event_id=None, seq=10**9, evidence_ids=(),
                explanation="仅比较终态，无法定位具体错误步骤。",
                dimension="PROCESS", points=20.0, confidence=0.2,
            )]
        verdict = Verdict.PASS if matched else Verdict.FAIL
    return RunAssessment(
        case_id=case.case_id,
        run_id=run.run_id,
        agent_version=run.agent_version,
        verdict=verdict,
        business_invoice_created=created,
        quality_score=100.0 if verdict is Verdict.PASS else 0.0,
        violations=violations,
        propagation=[],
        guards=[],
        uncertainties=list(notes) + ["消融变体：只用终态相等法，不做业务语义分析。"],
        state=state,
    )


def _apply_unrecognized_step_guard(
    state, case: Case, violations: list[Violation], uncertainties: list[str]
) -> list[Violation]:
    """未识别步骤名只在"顶替了缺失的关键业务阶段"时才降级（审计 B2 拆分）。

    审计 F-02 + R2-01：旧实现把任意未知步骤一律判 UNKNOWN；现在先看关键业务阶段
    是否缺失，缺失且存在未识别步骤才怀疑"阶段被改名/丢弃"，否则只记不确定说明。
    """
    unrecognized_steps = sorted({
        event.step_id for event in state.run.events
        if event.step_id and event.step_id not in STEP_IDS
    })
    if not unrecognized_steps:
        return violations
    critical_missing: list[str] = []
    if not state.collects:
        critical_missing.append("COLLECT_INVOICE_DATA")
    if not state.submits and case.reference_result.get("invoice_should_exist") is True:
        critical_missing.append("SUBMIT_INVOICE")
    if not state.confirms and state.submits:
        critical_missing.append("CONFIRM_INVOICE")
    if not critical_missing:
        uncertainties.append(
            "轨迹含未识别步骤 " + "、".join(unrecognized_steps[:4])
            + "；关键业务阶段齐全，按非业务步骤处理（不影响判定）。"
        )
        return violations
    explanation = (
        "轨迹缺少关键业务阶段（" + "、".join(critical_missing) + "），"
        "同时包含未识别的步骤名（" + "、".join(unrecognized_steps[:4]) + "），"
        "不能排除该阶段被改名或丢弃，无法核对处理是否正确。"
    )
    existing_unknown = next(
        (v for v in violations if v.label == "INSUFFICIENT_EVIDENCE"), None
    )
    if existing_unknown is not None:
        # 审计 R2-19：同一 run 只保留一条 INSUFFICIENT_EVIDENCE，证据缺口合并进说明
        violations = [
            replace(v, explanation=f"{v.explanation}；另：{explanation}")
            if v is existing_unknown else v
            for v in violations
        ]
    else:
        violations.append(Violation(
            rule_id="R902", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
            event_id=None, seq=10**9, evidence_ids=(),
            explanation=explanation,
            dimension="PROCESS", points=0.0, confidence=0.1,
        ))
    uncertainties.append(
        "轨迹含未识别步骤 " + "、".join(unrecognized_steps[:4]) + "，且缺少关键阶段 "
        + "、".join(critical_missing) + "；请检查适配器映射或流程字典版本。"
    )
    return violations


def _apply_unexplained_failure_fallback(
    state, case: Case, violations: list[Violation], uncertainties: list[str]
) -> list[Violation]:
    """没有任何可证实违规、但终态与题内预期不符时，给一条兜底 UNKNOWN。"""
    if violations:
        return violations
    fallback = unexplained_failure(state, case)
    if fallback is not None:
        uncertainties.append("终态与题内可接受终态不一致，但确定性规则未能定位根因。")
        return [fallback]
    return violations


def _dedupe_propagation(edges: list[PropagationEdge]) -> list[PropagationEdge]:
    """同一对 (来源事件, 去向事件) 只保留一条边（审计 R2-08 / R2-B-F-29）。"""
    seen: set[tuple[str, str]] = set()
    unique: list[PropagationEdge] = []
    for edge in edges:
        key = (edge.from_event, edge.to_event)
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return unique


def assess_run(
    case: Case,
    run,
    *,
    enabled_rule_ids: frozenset[str] | None = None,
    terminal_only: bool = False,
) -> RunAssessment:
    """评估一条运行（编排层）。

    `enabled_rule_ids` / `terminal_only` 仅供消融实验使用：后者完全跳过规则，
    只用"终态是否等于题内可接受终态"判断成功与否，代表不做任何业务语义分析时的上界。
    """
    state = reconstruct(case, run)
    created, inv_notes = _invoice_created(state)
    notes = list(inv_notes) + _intent_consistency_notes(state)

    if terminal_only:
        return _terminal_only_assessment(case, run, state, created, notes)

    violations, suppressions = evaluate_with_trace(state, case, enabled_rule_ids)
    # 非拒判型违规必须带证据事件坐标，否则官方校验器会整份退回（见 _ground_violations）
    violations = _ground_violations(violations)

    uncertainties = list(notes)
    violations = _apply_unrecognized_step_guard(state, case, violations, uncertainties)
    violations = _apply_unexplained_failure_fallback(state, case, violations, uncertainties)
    propagation = _dedupe_propagation([
        edge for violation in violations for edge in _build_propagation(state, violation)
    ])

    verdict = _classify(violations)
    if verdict is Verdict.UNKNOWN:
        uncertainties.append(
            "本运行判定为 UNKNOWN（证据不足或评测器未能完成判断），不参与版本改善/退化排名。"
        )
    score = max(0.0, 100.0 - sum(v.points for v in violations))
    return RunAssessment(
        case_id=case.case_id,
        run_id=run.run_id,
        agent_version=run.agent_version,
        verdict=verdict,
        business_invoice_created=created,
        quality_score=round(score, 1),
        violations=violations,
        propagation=propagation,
        guards=_detect_guards(state),
        uncertainties=uncertainties,
        state=state,
        suppressions=suppressions,
    )

# ------------------------------------------------------------------ 文案生成

def business_result_text(case: Case, a: RunAssessment) -> str:
    ref = case.reference_result
    if a.agent_task_success and a.business_invoice_created == bool(ref.get("invoice_should_exist")):
        return "业务结果与题内目标一致：处理路径正确，开票状态与预期相符。"
    if a.business_invoice_created and not ref.get("invoice_should_exist"):
        return "生成了不应存在的发票，业务结果偏离题内目标。"
    if not a.business_invoice_created and ref.get("invoice_should_exist"):
        return "题内目标要求生成发票，实际未生成。"
    if a.agent_task_success:
        return "处理过程符合规则，发票状态与预期一致。"
    first = a.first_error
    return f"业务结果未达题内目标，最早可证实问题：{first.explanation if first else '见根因列表'}"


def process_text(case: Case, a: RunAssessment) -> str:
    if a.agent_task_success:
        guards = "；".join(f"{g.event_id} {g.code} 为正确防护" for g in a.guards)
        return "执行步骤顺序与门禁均符合题内规则。" + (f"（{guards}）" if guards else "")
    parts = [f"{v.rule_id}：{v.explanation}" for v in a.violations]
    if a.guards:
        parts.append("工具拒绝属于正确拦截：" + "、".join(g.event_id for g in a.guards))
    return "；".join(parts)


def downstream_text(a: RunAssessment) -> str:
    if not a.propagation:
        return "未发现跨步骤传播，问题未扩散到后续业务动作。" if a.violations else "无异常传播。"
    seen: list[str] = []
    for edge in a.propagation:
        text = f"{edge.from_event} → {edge.to_event}（{edge.why}）"
        if text not in seen:
            seen.append(text)
    return "；".join(seen)


def suggestions(case: Case, a: RunAssessment) -> list[str]:
    table = {
        "AMOUNT_BASIS": "为含税/不含税表达增加确定性字段抽取校验，并在金额入参处回填口径标识做二次断言。",
        "AMOUNT_CALCULATION": "金额与税额统一走 Decimal 计算并以商品档案配置税率回归测试，禁止浮点直算。",
        "FIELD_FABRICATION": "必填字段缺失时强制走追问分支，禁止以默认值或猜测值填充。",
        "ENTITY_RESOLUTION": "购方多候选时强制进入消歧交互，选择动作必须携带用户显式选择证据。",
        "PRODUCT_MATCH": "商品匹配增加歧义检测（同名/近似名），低置信度转人工确认而非直接取第一候选。",
        "CONTEXT_UPDATE": "多轮修改后以最新轮次覆盖字段为准，采集阶段绑定轮次号并断言未被旧值覆盖。",
        "MULTI_ITEM_OMISSION": "批量任务以条目守恒断言收尾（输入条数 == 预览条数 == 提交条数）。",
        "INTENT_CLASSIFICATION": "任务类型与取消意图在解析阶段做显式分类并附置信度，低置信度转澄清。",
        "DUPLICATE_CONTROL": "重复风险标记必须作为硬门禁，命中即阻断并禁止降级为等待确认。",
        "CONFIRMATION_BYPASS": "把本次确认记录作为 invoice.submit 调用前的强制前置断言。",
        "TOOL_ERROR_HANDLING": "提交超时按三态处理：不明状态禁止自动重试，统一转人工并携带幂等键。",
        "CALLBACK_MISINTERPRETATION": "对外话术只允许读取回调结果生成，禁止由 Agent 自行宣称成功。",
        "INSUFFICIENT_EVIDENCE": "补充可观测字段（如模型抽取结果与原始文本的对应关系），以便定位根因。",
        "TOOL_ARGUMENT_ERROR": "提交参数直接复用已确认预览对象的快照哈希，禁止二次组装。",
    }
    out = [table[v.label] for v in a.violations if v.label in table]
    if not out and a.violations:
        # 任何失败运行都必须给出可执行建议：官方校验器强制要求
        out = ["针对识别到的规则违规补充确定性回归用例，并将其加入评测门禁。"]
    return list(dict.fromkeys(out))




