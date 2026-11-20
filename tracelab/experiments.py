"""G6 实验目录：语义变化变体（声明式）+ 指标汇总。

每个变体都在这里**预先声明**期望值（应触发哪些规则、判定应该是什么、不得牵连哪些规则、
证据应落在哪一步），而不是跑完评测器再回填——否则实验就变成了"用输出验证输出"。

约定：变体只改**一个业务事实**；`must_not_report` 用来统计误报（牵连了不该出现的规则）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .attribution import RunAssessment, assess_run
from .contracts import Case, Event, Run, Verdict
from .metamorphic import rebuild_case, rebuild_event, rebuild_run

# --------------------------------------------------------------------- 构造工具


def _patch_events(case: Case, patch: Callable[[Event], Event]) -> Case:
    # 审计 R3-B-08：通过重建 helper 同步 raw，变体才能被序列化后复现。
    runs = tuple(rebuild_run(run, events=tuple(patch(event) for event in run.events))
                 for run in case.runs)
    return rebuild_case(case, runs=runs)


def _first_event(case: Case, step_id: str) -> Event | None:
    for run in case.runs:
        for event in run.events:
            if event.step_id == step_id:
                return event
    return None


def _patch_output(case: Case, step_id: str, **updates: Any) -> Case:
    def patch(event: Event) -> Event:
        if event.step_id != step_id:
            return event
        output = dict(event.observed_output)
        output.update(updates)
        return rebuild_event(event, observed_output=output)

    return _patch_events(case, patch)


def _patch_input(case: Case, step_id: str, **updates: Any) -> Case:
    def patch(event: Event) -> Event:
        if event.step_id != step_id:
            return event
        data = dict(event.observed_input)
        data.update(updates)
        return rebuild_event(event, observed_input=data)

    return _patch_events(case, patch)


def _patch_event_fields(case: Case, step_id: str, **fields: Any) -> Case:
    def patch(event: Event) -> Event:
        if event.step_id != step_id:
            return event
        return rebuild_event(event, **fields)

    return _patch_events(case, patch)


def _patch_context(case: Case, **updates: Any) -> Case:
    context = dict(case.context)
    context.update(updates)
    return rebuild_case(case, context=context)


def _patch_reference(case: Case, **updates: Any) -> Case:
    reference = dict(case.reference_result)
    reference.update(updates)
    return rebuild_case(case, reference_result=reference)


# --------------------------------------------------------------------- 变体声明


@dataclass(frozen=True)
class VariantSpec:
    """一个"业务语义变化"实验：改什么、应触发什么、不得牵连什么。"""

    name: str
    description: str
    rule_under_test: str
    changed_fields: tuple[str, ...]
    mutate: Callable[[Case], Case]
    expected_rules: frozenset[str]
    expected_verdict: Verdict
    must_not_report: frozenset[str] = frozenset()
    #: 首因应落在这几步之一（规则把证据锚在业务发生处，允许等价位置）
    expected_evidence_steps: frozenset[str] = frozenset()


def _confirmation_missing(case: Case) -> Case:
    patched = _patch_context(case, user_confirmation_recorded=False)
    return _patch_input(patched, "SUBMIT_INVOICE", user_confirmed=False)


def _amount_basis_switched(case: Case) -> Case:
    return _patch_output(case, "COLLECT_INVOICE_DATA", amount_basis="tax_inclusive")


def _amount_calculation_wrong(case: Case) -> Case:
    event = _first_event(case, "COLLECT_INVOICE_DATA")
    computed = dict(event.observed_output.get("computed_amounts") or {}) if event else {}
    computed["tax_amount"] = 6.0
    return _patch_output(case, "COLLECT_INVOICE_DATA", computed_amounts=computed)


def _product_swapped(case: Case) -> Case:
    return _patch_output(case, "COLLECT_INVOICE_DATA", item_id="ITEM-999")


def _batch_line_dropped(case: Case) -> Case:
    """采集层有 2 条明细、题内也要求 2 条，但实际只保留了 1 条（少一条明细）。"""
    event = _first_event(case, "COLLECT_INVOICE_DATA")
    items = list(event.observed_output.get("line_items") or []) if event else []
    first = dict(items[0]) if items else {"item_id": "ITEM-006", "exclusive_amount": 1000.0}
    second = {"item_id": "ITEM-007", "exclusive_amount": 500.0}
    patched = _patch_output(case, "COLLECT_INVOICE_DATA",
                            line_items=[first], line_count=1)
    return _patch_reference(patched, expected_line_items=[first, second])


def _intent_misread(case: Case) -> Case:
    return _patch_output(case, "PARSE_INTENT", invoice_task_type="RED")


def _duplicate_risk_ignored(case: Case) -> Case:
    return _patch_output(case, "VALIDATE_DATA", risk_flags=["DUPLICATE_INVOICE"], create_allowed=True)


def _preview_submit_drift(case: Case) -> Case:
    """提交参数里的金额与已确认预览不一致（同时改 observed_input 与 tool_calls，避免只改到一半）。"""
    def patch(event: Event) -> Event:
        if event.step_id != "SUBMIT_INVOICE":
            return event
        amounts = dict(event.observed_input.get("amounts") or {})
        amounts["inclusive_amount"] = float(amounts.get("inclusive_amount") or 0) + 10.0
        tools = tuple(
            replace(
            tool,
            arguments={**dict(tool.arguments), "amounts": amounts},
            raw={**dict(tool.raw), "arguments": {**dict(tool.arguments), "amounts": amounts}},
        )
            for tool in event.tool_calls
        )
        return rebuild_event(event, observed_input={**dict(event.observed_input), "amounts": amounts},
                       tool_calls=tools)

    return _patch_events(case, patch)


def _submit_timeout_retried(case: Case) -> Case:
    patched = _patch_event_fields(case, "SUBMIT_INVOICE", step_status="TIMED_OUT", retry_count=1)
    return _patch_output(patched, "SUBMIT_INVOICE", submit_status="unknown")


def _callback_failed_but_success(case: Case) -> Case:
    return _patch_output(case, "WAIT_INVOICE_CALLBACK", callback_status="failed")


def _fabricated_buyer(case: Case) -> Case:
    patched = _patch_reference(case, buyer_id=None, required_action="ASK_BUYER")
    return _patch_output(patched, "COLLECT_INVOICE_DATA", buyer_id="CUST-GUESSED")


def _ambiguous_buyer_chosen(case: Case) -> Case:
    patched = _patch_context(case, buyer_candidates=[
        {"buyer_id": "CUST-001", "profile_complete": True},
        {"buyer_id": "CUST-002", "profile_complete": True},
    ])
    return _patch_reference(patched, required_action="ASK_BUYER_DISAMBIGUATION")


def _cancel_misread(case: Case) -> Case:
    """题内要求取消，Agent 却继续推进到提交（轨迹里没有 CANCEL_TASK）。"""
    return _patch_reference(case, required_action="CANCEL")


def _red_invoice_without_original(case: Case) -> Case:
    """红票题要求补充原票引用/金额，Agent 未追问即继续出票（原票引用为空）。"""
    patched = _patch_reference(case, required_action="ASK_ORIGINAL_INVOICE_AND_AMOUNT")
    return _patch_output(patched, "COLLECT_INVOICE_DATA", original_invoice_id=None)


def _stale_round_value_used(case: Case) -> Case:
    """多轮里金额已被第 2 轮改为 1000，Agent 仍沿用第 1 轮被覆盖的 900。"""
    patched = _patch_context(case, conversation_rounds=[
        {"round": 1, "exclusive_amount": 900.0},
        {"round": 2, "supersedes_round": 1, "exclusive_amount": 1000.0},
    ])
    return _patch_output(patched, "COLLECT_INVOICE_DATA", amount_value=900.0)



def _replace_events(case: Case, events: Sequence[Event]) -> Case:
    runs = tuple(rebuild_run(run, events=tuple(events)) for run in case.runs)
    return rebuild_case(case, runs=runs)


def _shell_only_run(case: Case) -> Case:
    """只保留外壳阶段，删掉全部业务阶段 → 评测器应拒判（UNKNOWN / R902）。"""
    from .samples import make_event

    shell = [
        make_event("R902-01", 1, "RECEIVE_REQUEST"),
        make_event("R902-02", 2, "CREATE_TASK"),
        make_event("R902-03", 3, "COMPLETE_TASK", result_code="E200"),
    ]
    return _replace_events(case, shell)


def _collect_event_removed(case: Case) -> Case:
    """已进入确认/提交，但轨迹里没有采集事件 → 关键业务事实无从核对 → 拒判。"""
    events = [event for event in case.runs[0].events if event.step_id != "COLLECT_INVOICE_DATA"]
    return _replace_events(case, events)


def _collect_without_required_fields(case: Case) -> Case:
    """有采集事件，但没给出题内要求的任何字段 → 同样只能拒判。"""
    def patch(event: Event) -> Event:
        if event.step_id != "COLLECT_INVOICE_DATA":
            return event
        return rebuild_event(event, observed_output={"unrelated": "x"})

    return _patch_events(case, patch)


SENSITIVITY_VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec(
        name="confirmation_missing",
        description="有效确认 → 缺少确认仍提交",
        rule_under_test="R010",
        changed_fields=("context.user_confirmation_recorded", "SUBMIT_INVOICE.observed_input.user_confirmed"),
        mutate=_confirmation_missing,
        expected_rules=frozenset({"R010"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R001", "R002", "R013"}),
        expected_evidence_steps=frozenset({"SUBMIT_INVOICE"}),
    ),
    VariantSpec(
        name="amount_basis_switched",
        description="正确口径 → 换成另一种口径",
        rule_under_test="R001",
        changed_fields=("COLLECT_INVOICE_DATA.observed_output.amount_basis",),
        mutate=_amount_basis_switched,
        expected_rules=frozenset({"R001"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R002", "R013"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA"}),
    ),
    VariantSpec(
        name="amount_calculation_wrong",
        description="正确税额 → 税额算错",
        rule_under_test="R002",
        changed_fields=("COLLECT_INVOICE_DATA.observed_output.computed_amounts.tax_amount",),
        mutate=_amount_calculation_wrong,
        expected_rules=frozenset({"R002"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R001"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA"}),
    ),
    VariantSpec(
        name="product_swapped",
        description="完整商品 → 匹配到别的商品",
        rule_under_test="R005",
        changed_fields=("COLLECT_INVOICE_DATA.observed_output.item_id",),
        mutate=_product_swapped,
        expected_rules=frozenset({"R005"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R004"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA"}),
    ),
    VariantSpec(
        name="batch_line_dropped",
        description="完整条目 → 缺一条明细",
        rule_under_test="R006",
        changed_fields=("COLLECT_INVOICE_DATA.observed_output.line_items", "reference.expected_line_items"),
        mutate=_batch_line_dropped,
        expected_rules=frozenset({"R006"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R013", "R001"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA", "CONFIRM_INVOICE"}),
    ),
    VariantSpec(
        name="intent_misread",
        description="正确任务类型 → 识别成另一类",
        rule_under_test="R007",
        changed_fields=("PARSE_INTENT.observed_output.invoice_task_type",),
        mutate=_intent_misread,
        expected_rules=frozenset({"R007"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R013"}),
        expected_evidence_steps=frozenset({"PARSE_INTENT"}),
    ),
    VariantSpec(
        name="duplicate_risk_ignored",
        description="无重复风险 → 工具已提示重复风险仍继续",
        rule_under_test="R009",
        changed_fields=("VALIDATE_DATA.observed_output.risk_flags",),
        mutate=_duplicate_risk_ignored,
        expected_rules=frozenset({"R009"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R010"}),
        expected_evidence_steps=frozenset({"VALIDATE_DATA", "SUBMIT_INVOICE", "CONFIRM_INVOICE"}),
    ),
    VariantSpec(
        name="preview_submit_drift",
        description="预览与提交一致 → 提交金额漂移",
        rule_under_test="R013",
        changed_fields=("SUBMIT_INVOICE.observed_input.amounts.inclusive_amount",),
        mutate=_preview_submit_drift,
        expected_rules=frozenset({"R013"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R002", "R001"}),
        expected_evidence_steps=frozenset({"SUBMIT_INVOICE"}),
    ),
    VariantSpec(
        name="submit_timeout_retried",
        description="提交明确成功 → 提交超时后重试且未转人工",
        rule_under_test="R011",
        changed_fields=("SUBMIT_INVOICE.step_status", "SUBMIT_INVOICE.retry_count")
        ,
        mutate=_submit_timeout_retried,
        expected_rules=frozenset({"R011"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R013"}),
        expected_evidence_steps=frozenset({"SUBMIT_INVOICE"}),
    ),
    VariantSpec(
        name="callback_failed_but_success",
        description="回调成功 → 回调失败却仍以成功终态对外报告",
        rule_under_test="R012",
        changed_fields=("WAIT_INVOICE_CALLBACK.observed_output.callback_status",),
        mutate=_callback_failed_but_success,
        expected_rules=frozenset({"R012"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R011"}),
        expected_evidence_steps=frozenset({"WAIT_INVOICE_CALLBACK", "COMPLETE_TASK"}),
    ),
    VariantSpec(
        name="fabricated_buyer",
        description="购方信息完整 → 信息缺失时自行编造购方",
        rule_under_test="R003",
        changed_fields=("reference.buyer_id", "reference.required_action", "COLLECT.observed_output.buyer_id"),
        mutate=_fabricated_buyer,
        expected_rules=frozenset({"R003"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R004"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA", "CONFIRM_INVOICE"}),
    ),
    VariantSpec(
        name="ambiguous_buyer_chosen",
        description="唯一购方 → 多候选未消歧即选定",
        rule_under_test="R004",
        changed_fields=("context.buyer_candidates", "reference.required_action"),
        mutate=_ambiguous_buyer_chosen,
        expected_rules=frozenset({"R004"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R003"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA"}),
    ),
    VariantSpec(
        name="cancel_misread",
        description="用户要求取消 → Agent 未取消反而继续推进",
        rule_under_test="R008",
        changed_fields=("reference.required_action",),
        mutate=_cancel_misread,
        expected_rules=frozenset({"R008"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R001", "R002", "R013", "R010"}),
        expected_evidence_steps=frozenset({"PARSE_INTENT", "SUBMIT_INVOICE"}),
    ),
    VariantSpec(
        name="red_invoice_without_original",
        description="红票要求补原票引用 → 未追问即继续出票",
        rule_under_test="R014",
        changed_fields=("reference.required_action", "COLLECT.observed_output.original_invoice_id"),
        mutate=_red_invoice_without_original,
        expected_rules=frozenset({"R014"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R003", "R001", "R002"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA", "CONFIRM_INVOICE"}),
    ),
    VariantSpec(
        name="stale_round_value_used",
        description="多轮金额已更新 → 仍沿用被覆盖的旧轮次金额",
        rule_under_test="R015",
        changed_fields=("context.conversation_rounds", "COLLECT.observed_output.amount_value"),
        mutate=_stale_round_value_used,
        expected_rules=frozenset({"R015"}),
        expected_verdict=Verdict.FAIL,
        must_not_report=frozenset({"R002", "R001"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA"}),
    ),
    # ---- 拒判类变体（Batch 3）：证据不足时"应该拒绝判断"，而不是猜 PASS/FAIL ----
    VariantSpec(
        name="evidence_stages_missing",
        description="正常轨迹 → 只剩外壳阶段（无任何业务阶段事件）",
        rule_under_test="R902",
        changed_fields=("run.events",),
        mutate=_shell_only_run,
        expected_rules=frozenset({"R902"}),
        expected_verdict=Verdict.UNKNOWN,
        must_not_report=frozenset({"R001", "R002", "R005", "R007", "R010", "R013"}),
        expected_evidence_steps=frozenset(),   # 拒判针对"整条轨迹缺证据"，没有单一首因事件
    ),
    VariantSpec(
        name="evidence_collect_missing",
        description="正常轨迹 → 删掉采集事件（已确认/提交但无业务事实）",
        rule_under_test="R902",
        changed_fields=("run.events",),
        mutate=_collect_event_removed,
        expected_rules=frozenset({"R902"}),
        expected_verdict=Verdict.UNKNOWN,
        must_not_report=frozenset({"R003", "R005", "R007", "R010", "R013"}),
        expected_evidence_steps=frozenset(),
    ),
    VariantSpec(
        name="evidence_collect_without_facts",
        description="正常轨迹 → 采集事件存在但不含任何题内要求字段",
        rule_under_test="R902",
        changed_fields=("COLLECT_INVOICE_DATA.observed_output",),
        mutate=_collect_without_required_fields,
        expected_rules=frozenset({"R902"}),
        expected_verdict=Verdict.UNKNOWN,
        must_not_report=frozenset({"R003", "R010", "R013"}),
        expected_evidence_steps=frozenset({"COLLECT_INVOICE_DATA"}),
    ),
)


# --------------------------------------------------------------------- 指标汇总


@dataclass(frozen=True)
class VariantOutcome:
    name: str
    rule_under_test: str
    expected_rules: tuple[str, ...]
    actual_rules: tuple[str, ...]
    detected: bool
    verdict_ok: bool
    attribution_ok: bool
    unexpected_rules: tuple[str, ...]
    verdict: str
    indeterminate: bool
    first_fault_step: str | None
    expected_verdict: str = "FAIL"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rule_under_test": self.rule_under_test,
            "expected_rules": list(self.expected_rules),
            "actual_rules": list(self.actual_rules),
            "detected": self.detected,
            "verdict_ok": self.verdict_ok,
            "attribution_ok": self.attribution_ok,
            "unexpected_rules": list(self.unexpected_rules),
            "verdict": self.verdict,
            "expected_verdict": self.expected_verdict,
            "indeterminate": self.indeterminate,
            "first_fault_step": self.first_fault_step,
        }


@dataclass
class SensitivityReport:
    outcomes: list[VariantOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def detected(self) -> int:
        return sum(1 for item in self.outcomes if item.detected and item.verdict_ok)

    @property
    def fault_sensitivity(self) -> float | None:
        return round(self.detected / self.total, 4) if self.total else None

    @property
    def attribution_stable(self) -> int:
        return sum(1 for item in self.outcomes if item.detected and item.attribution_ok)

    @property
    def attribution_stability(self) -> float | None:
        return round(self.attribution_stable / self.total, 4) if self.total else None

    @property
    def false_positive_variants(self) -> int:
        return sum(1 for item in self.outcomes if item.unexpected_rules)

    @property
    def false_positive_rate(self) -> float | None:
        return round(self.false_positive_variants / self.total, 4) if self.total else None

    @property
    def indeterminate(self) -> int:
        """**预期之外**的 UNKNOWN 数量。

        拒判类变体（R902）本来就期望 UNKNOWN，不能算进"无法判断"的坏账；
        只有"期望有明确结论却给了 UNKNOWN"才算。
        """
        return sum(1 for item in self.outcomes if item.indeterminate and not item.verdict_ok)

    @property
    def rule_coverage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.outcomes:
            counts[item.rule_under_test] = counts.get(item.rule_under_test, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "detected": self.detected,
            "fault_sensitivity": self.fault_sensitivity,
            "attribution_stable": self.attribution_stable,
            "attribution_stability": self.attribution_stability,
            "false_positive_variants": self.false_positive_variants,
            "false_positive_rate": self.false_positive_rate,
            "indeterminate": self.indeterminate,
            "rule_coverage": self.rule_coverage,
            "outcomes": [item.as_dict() for item in self.outcomes],
        }


def _first_fault_step(assessment: RunAssessment, run: Run) -> str | None:
    first = assessment.first_error
    if first is None or first.event_id is None:
        return None
    for event in run.events:
        if event.event_id == first.event_id:
            return event.step_id
    return None


def run_sensitivity(
    base_case: Case,
    *,
    variants: Sequence[VariantSpec] | None = None,
) -> SensitivityReport:
    """把每个变体施加到同一个干净基线上，核对"该报的报了、不该报的没报"。"""
    specs = list(variants) if variants is not None else list(SENSITIVITY_VARIANTS)
    report = SensitivityReport()
    for spec in specs:
        mutated = spec.mutate(base_case)
        run: Run = mutated.runs[0]
        assessment = assess_run(mutated, run)
        # 规则身份用 rule_id（业务分类标签在 labels 里，同一个标签可能由多条规则产生）
        actual = tuple(sorted({violation.rule_id for violation in assessment.violations}))
        detected = spec.expected_rules.issubset(set(actual))
        unexpected = tuple(sorted(set(actual) & set(spec.must_not_report)))
        step = _first_fault_step(assessment, run)
        attribution_ok = step in spec.expected_evidence_steps if spec.expected_evidence_steps else True
        report.outcomes.append(VariantOutcome(
            name=spec.name,
            rule_under_test=spec.rule_under_test,
            expected_rules=tuple(sorted(spec.expected_rules)),
            actual_rules=actual,
            detected=detected,
            verdict_ok=assessment.verdict is spec.expected_verdict,
            attribution_ok=attribution_ok,
            unexpected_rules=unexpected,
            verdict=assessment.verdict.value,
            expected_verdict=spec.expected_verdict.value,
            indeterminate=assessment.verdict is Verdict.UNKNOWN,
            first_fault_step=step,
        ))
    return report


def render_markdown(
    invariance: Mapping[str, Any] | None = None,
    sensitivity: Mapping[str, Any] | None = None,
) -> str:
    """把两份指标渲染成简短 markdown（供 docs 引用，不含原始 case 内容）。"""
    lines: list[str] = ["# G6 变形实验摘要", ""]
    if invariance:
        ratio = invariance.get("consistency_ratio")
        lines += [
            "## 语义保持（invariance）",
            "",
            f"- 样本对：{invariance.get('total')}",
            f"- 判定完全一致：{invariance.get('consistent')} / {invariance.get('total')}"
            f"（{ratio:.1%}）" if isinstance(ratio, (int, float)) else "- 判定完全一致：—",
            f"- 不一致：{invariance.get('inconsistent')}",
            f"- 随机种子：{invariance.get('seed')}",
            f"- 语料 Case：{invariance.get('cases_total')}；实际检验：{invariance.get('cases_tested')}"
            f"；跳过：{len(invariance.get('skipped') or [])}",
        ]
        skipped = invariance.get("skipped") or []
        for item in skipped:
            lines.append(f"  - 跳过 `{item.get('case_id')}`：{item.get('reason')}")
        lines.append("")
    if sensitivity:
        fs = sensitivity.get("fault_sensitivity")
        att = sensitivity.get("attribution_stability")
        fpr = sensitivity.get("false_positive_rate")
        lines += [
            "## 业务变化（sensitivity）",
            "",
            f"- 变体数：{sensitivity.get('total')}",
            f"- 检出（期望规则 + 期望判定都成立）：{sensitivity.get('detected')} / {sensitivity.get('total')}"
            + (f"（{fs:.1%}）" if isinstance(fs, (int, float)) else ""),
            f"- 首因位置符合预期：{sensitivity.get('attribution_stable')} / {sensitivity.get('total')}"
            + (f"（{att:.1%}）" if isinstance(att, (int, float)) else ""),
            f"- 牵连了不该出现的规则（误报变体）：{sensitivity.get('false_positive_variants')}"
            + (f"（{fpr:.1%}）" if isinstance(fpr, (int, float)) else ""),
            f"- 无法判断（UNKNOWN）：{sensitivity.get('indeterminate')}",
            f"- 覆盖规则：{', '.join(sensitivity.get('rule_coverage', {}).keys())}",
            "",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------- 自造语料

def synthetic_clean_case() -> Case:
    """一个"处处合规"的自造 Case：作为敏感度实验的基线（每个变体只改一处）。"""
    from .samples import basic_flow, context, make_case, make_run, reference

    run = make_run("G6-CLEAN-A", basic_flow(prefix="g6clean"),
                   final_state="SUCCEEDED", invoice_created=True)
    return make_case("G6-CLEAN", context=context(), reference_result=reference(), runs=[run])


def synthetic_corpus() -> list[Case]:
    """7 个自造 Case：1 个干净通过 + 5 个不同规则违规 + 1 个应被拒判（UNKNOWN）。

    刻意不依赖官方资料包：评委在任意环境都能用同一条命令复现实验数字。
    加入 UNKNOWN 样本是为了验证"拒判结论在语义保持变换下同样稳定"。
    """
    from .samples import basic_flow, context, make_case, make_run, reference

    def build(case_id: str, *, ctx: Mapping[str, Any] | None = None,
              ref: Mapping[str, Any] | None = None,
              flow: Mapping[str, Any] | None = None) -> Case:
        run = make_run(f"{case_id}-A", basic_flow(prefix=case_id, **(flow or {})),
                       final_state="SUCCEEDED", invoice_created=True)
        return make_case(case_id, context=dict(ctx or context()),
                         reference_result=dict(ref or reference()), runs=[run])

    corpus = [
        build("PASS"),
        build("BASIS", flow={"amount_basis": "tax_inclusive"}),
        build("CONFIRM", ctx=context(user_confirmation_recorded=False)),
        build("PRODUCT", flow={"item_id": "ITEM-999"}),
        build("BATCH", ref=reference(expected_line_items=[
            {"item_id": "ITEM-006", "exclusive_amount": 1000.0},
            {"item_id": "ITEM-007", "exclusive_amount": 500.0},
        ])),
        build("INTENT", flow={"task_type": "RED"}),
    ]
    # 第 7 个：只剩外壳阶段 → 证据不足 → 应稳定判 UNKNOWN
    corpus.append(_shell_only_run(build("REFUSAL")))
    return corpus


def run_experiment_suite(*, seed: int = 20261104) -> dict[str, Any]:
    """一次跑完两类实验，返回可直接落盘/上屏的结构化结果（固定种子，可复现）。"""
    from .metamorphic import cases_digest, run_invariance

    corpus = synthetic_corpus()
    invariance = run_invariance(corpus, seed=seed).as_dict()
    sensitivity = run_sensitivity(synthetic_clean_case()).as_dict()
    return {
        "schema": "tracelab-experiment-suite/1",
        "seed": seed,
        "corpus": {
            "cases": len(corpus),
            "digest": cases_digest(corpus),
            "note": "自造样本：与官方资料包无关，只用于检验评测器自身稳定性",
        },
        "invariance": invariance,
        "sensitivity": sensitivity,
    }


def render_suite_markdown(suite: Mapping[str, Any]) -> str:
    """把实验套件渲染成给人看的短报告（数字都带分子/分母）。"""
    invariance = suite.get("invariance") or {}
    sensitivity = suite.get("sensitivity") or {}
    corpus = suite.get("corpus") or {}
    header = [
        "# 变形实验报告（G6-A）",
        "",
        "> 目的：检验评测器**自身的稳定性**——业务语义不变时判定是否不变，业务语义变化时是否按预期触发。",
        f"> 样本：{corpus.get('cases')} 个**自造** Case（与官方资料包无关），"
        f"固定随机种子 `{suite.get('seed')}`；样本摘要 `{str(corpus.get('digest'))[:16]}…`。",
        "> 结论只对这批构造成立，**不是真实业务泛化准确率**。",
        "",
    ]
    return "\n".join(header) + render_markdown(invariance=invariance, sensitivity=sensitivity)
