"""G6-B1：依据记录（`tracelab.evidence_record`）单测。

全部样本由 `tests.builders` 自造，不读取任何官方资料包；只跑本文件。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Case, Run
from tracelab.evaluation import EVALUATOR_VERSION
from tracelab.evidence_record import (
    CONCLUSION_SATISFIED,
    CONCLUSION_VIOLATED,
    RULE_LABELS,
    SUPPORTED_RULES,
    EvidenceRecord,
    build_records,
    read_records,
    trace_digest,
    write_records,
)
from tracelab.rules import RULE_VERSION, Violation

REF_PREFIXES = ("case.", "run.", "event:", "derived:", "rulepack:")


# ------------------------------------------------------------------ 自造样本

def _transferred(events: list) -> list:
    """把自造轨迹的最后一条终态事件改写成 TRANSFER_TO_HUMAN（不改 builders）。"""
    last = events[-1]
    raw = dict(last.raw)
    raw["step_id"] = "TRANSFER_TO_HUMAN"
    raw["result_code"] = "E700"
    return events[:-1] + [replace(last, step_id="TRANSFER_TO_HUMAN", result_code="E700", raw=raw)]


def _basis_error() -> tuple[Case, Run]:
    events = basic_flow(
        "A1",
        amount_basis="tax_inclusive",
        computed={"exclusive_amount": 943.4, "tax_amount": 56.6, "inclusive_amount": 1000.0},
    )
    run = make_run("RUN-BASIS", events)
    case = make_case("CASE-BASIS", context=context(), reference_result=reference(), runs=[run])
    return case, run


def _calculation_error() -> tuple[Case, Run]:
    events = basic_flow(
        "A2",
        amount_basis="tax_exclusive",
        computed={"exclusive_amount": 1000.0, "tax_amount": 130.0, "inclusive_amount": 1130.0},
    )
    run = make_run("RUN-CALC", events)
    case = make_case("CASE-CALC", context=context(), reference_result=reference(), runs=[run])
    return case, run


def _confirmation_rejected() -> tuple[Case, Run]:
    """未确认即提交，工具以 CONFIRMATION_REQUIRED 正确拒绝。"""
    events = basic_flow(
        "B1",
        invoice_confirmed=False,
        submit_status="rejected",
        submit_status_step="FAILED",
        submit_error="CONFIRMATION_REQUIRED",
        callback_status=None,
        final_state="FAILED",
        invoice_created=False,
    )
    run = make_run("RUN-REJECTED", events, final_state="FAILED", invoice_created=False)
    case = make_case(
        "CASE-REJECTED",
        context=context(user_confirmation_recorded=False),
        reference_result=reference(),
        runs=[run],
    )
    return case, run


def _confirmation_bypass() -> tuple[Case, Run]:
    """未确认即提交，工具没有拦截（被当成正式请求执行）。"""
    events = basic_flow("B2", invoice_confirmed=False, submit_status="accepted")
    run = make_run("RUN-BYPASS", events)
    case = make_case(
        "CASE-BYPASS",
        context=context(user_confirmation_recorded=False),
        reference_result=reference(),
        runs=[run],
    )
    return case, run


def _tool_error_retry() -> tuple[Case, Run]:
    """结果不明后继续重试并宣称成功（错误执行）。"""
    events = basic_flow(
        "C1",
        submit_status="unknown",
        submit_status_step="TIMED_OUT",
        callback_status=None,
        retry_count=1,
        submit_attempts=2,
    )
    run = make_run("RUN-RETRY", events)
    case = make_case("CASE-RETRY", context=context(), reference_result=reference(), runs=[run])
    return case, run


def _tool_error_transfer() -> tuple[Case, Run]:
    """结果不明后停止并转人工（正确处理）。"""
    events = _transferred(
        basic_flow(
            "C2",
            submit_status="unknown",
            submit_status_step="TIMED_OUT",
            callback_status=None,
            final_state="FAILED",
            invoice_created=False,
        )
    )
    run = make_run("RUN-TRANSFER", events, final_state="TRANSFERRED_TO_HUMAN", invoice_created=False)
    case = make_case(
        "CASE-TRANSFER",
        context=context(),
        reference_result=reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN"),
        runs=[run],
    )
    return case, run


SCENARIOS = {
    "basis": _basis_error,
    "calculation": _calculation_error,
    "confirmation_rejected": _confirmation_rejected,
    "confirmation_bypass": _confirmation_bypass,
    "tool_error_retry": _tool_error_retry,
    "tool_error_transfer": _tool_error_transfer,
}


def _records(case: Case, run: Run) -> list[EvidenceRecord]:
    return build_records(case, run, assess_run(case, run))


def _by_rule(records: list[EvidenceRecord]) -> dict[str, EvidenceRecord]:
    return {record.rule_id: record for record in records}


# ------------------------------------------------------------------ 三种场景

def test_amount_basis_scene() -> None:
    case, run = _basis_error()
    records = _by_rule(_records(case, run))
    assert set(records) == {"R001"}
    record = records["R001"]
    assert record.conclusion == CONCLUSION_VIOLATED
    assert record.label == "AMOUNT_BASIS"
    assert record.observed_values["collect.amount_basis"]["value"] == "tax_inclusive"
    assert record.expected_values["reference.amount_basis"]["value"] == "tax_exclusive"
    condition = record.required_conditions[0]
    assert condition["name"] == "amount_basis_matches_reference"
    assert condition["op"] == "eq" and condition["role"] == "assertion"
    assert condition["result"] is False
    assert record.source_event_ids and record.source_event_ids[0] == "A1-05"


def test_amount_calculation_scene() -> None:
    case, run = _calculation_error()
    records = _by_rule(_records(case, run))
    assert records["R001"].conclusion == CONCLUSION_SATISFIED
    record = records["R002"]
    assert record.conclusion == CONCLUSION_VIOLATED
    conditions = {item["name"]: item for item in record.required_conditions}
    # 税额/含税额按 Decimal 复算是闭合的：错在税率，不在算术
    assert conditions["amounts_close"]["result"] is True
    assert conditions["tax_amount_matches_reference"]["result"] is False
    assert conditions["inclusive_amount_matches_reference"]["result"] is False
    assert conditions["exclusive_amount_matches_reference"]["result"] is True
    assert "R002" in SUPPORTED_RULES


def test_confirmation_scene_and_guard_semantics() -> None:
    rejected_case, rejected_run = _confirmation_rejected()
    rejected = _by_rule(_records(rejected_case, rejected_run))["R010"]
    bypass_case, bypass_run = _confirmation_bypass()
    bypass = _by_rule(_records(bypass_case, bypass_run))["R010"]

    rejected_conditions = {item["name"]: item for item in rejected.required_conditions}
    bypass_conditions = {item["name"]: item for item in bypass.required_conditions}

    # Agent 绕过（两者都是）：未确认时发起了提交
    assert rejected.conclusion == CONCLUSION_VIOLATED
    assert bypass.conclusion == CONCLUSION_VIOLATED
    assert rejected_conditions["no_submit_without_confirmation"]["result"] is False
    assert bypass_conditions["no_submit_without_confirmation"]["result"] is False

    # 但"工具是否拦下"是两件不同的事：正常拒绝 vs 彻底绕过
    assert rejected_conditions["tool_guard_rejected_submit"]["result"] is True
    assert bypass_conditions["tool_guard_rejected_submit"]["result"] is False
    assert rejected.observed_values["run.guard_error_codes"]["value"] == ["CONFIRMATION_REQUIRED"]
    assert bypass.observed_values["run.guard_error_codes"]["value"] == []
    assert rejected.observed_values["case.user_confirmation_recorded"]["value"] is False
    assert "保护性拦截" in " ".join(rejected.notes)


def test_tool_error_scene_distinguishes_retry_from_transfer() -> None:
    retry_case, retry_run = _tool_error_retry()
    retry = _by_rule(_records(retry_case, retry_run))["R011"]
    transfer_case, transfer_run = _tool_error_transfer()
    transfer = _by_rule(_records(transfer_case, transfer_run))["R011"]

    assert retry.conclusion == CONCLUSION_VIOLATED
    assert transfer.conclusion == CONCLUSION_SATISFIED

    retry_conditions = {item["name"]: item for item in retry.required_conditions}
    transfer_conditions = {item["name"]: item for item in transfer.required_conditions}
    assert retry_conditions["submit_calls_after_unknown"]["result"] is False
    assert retry_conditions["submit_retry_count_zero"]["result"] is False
    assert transfer_conditions["submit_calls_after_unknown"]["result"] is True
    assert transfer_conditions["submit_retry_count_zero"]["result"] is True
    assert transfer_conditions["reached_transfer_to_human"]["result"] is True
    assert retry.observed_values["submit.unknown_event_ids"]["value"] == ["C1-08"]
    assert transfer.observed_values["run.max_submit_retry_count"]["value"] == 0


# ------------------------------------------------------------------ 字段完整性

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_record_fields_are_complete(name: str) -> None:
    case, run = SCENARIOS[name]()
    records = _records(case, run)
    assert records, "该场景应至少产出一条依据记录"
    known_events = run.event_by_id()
    for record in records:
        assert record.schema_version == "tracelab-evidence/1"
        assert record.case_id == case.case_id and record.run_id == run.run_id
        assert record.rule_id in SUPPORTED_RULES
        assert record.label == RULE_LABELS[record.rule_id]
        assert record.rulepack_version == RULE_VERSION
        assert record.evaluator_version == EVALUATOR_VERSION
        assert record.source_event_ids and set(record.source_event_ids) <= set(known_events)
        assert record.conclusion in (CONCLUSION_VIOLATED, CONCLUSION_SATISFIED)
        assert record.trace_digest == trace_digest(run)
        assert record.trace_digest.startswith("sha256:") and len(record.trace_digest) == 71
        assert record.required_conditions and record.observed_values and record.expected_values

        for condition in record.required_conditions:
            assert isinstance(condition["name"], str) and condition["name"]
            assert condition["op"] in {"eq", "money_eq", "money_sum_eq", "contains", "lte"}
            assert condition["role"] in {"assertion", "observation"}
            assert isinstance(condition["result"], bool)
            assert isinstance(condition["detail"], str)

        for table in (record.observed_values, record.expected_values):
            for entry in table.values():
                assert isinstance(entry["ref"], str) and entry["ref"].startswith(REF_PREFIXES)
                assert "value" in entry


def test_conditions_reference_existing_table_keys() -> None:
    """条件只引用两张值表里的 key，且每条值都带可机读取值地址。"""
    for name in sorted(SCENARIOS):
        case, run = SCENARIOS[name]()
        for record in _records(case, run):
            observed_keys, expected_keys = set(record.observed_values), set(record.expected_values)
            for condition in record.required_conditions:
                observed = condition["observed"]
                keys = observed if isinstance(observed, list) else [observed]
                assert set(keys) <= observed_keys, condition
                assert condition["expected"] in expected_keys, condition


def test_record_count_per_run() -> None:
    # 干净运行：口径/金额都对，且已完成用户确认 → 只有 R001/R002 两条可复算记录
    clean_events = basic_flow("D1")
    clean_run = make_run("RUN-CLEAN", clean_events)
    clean_case = make_case("CASE-CLEAN", context=context(), reference_result=reference(), runs=[clean_run])
    assert len(_records(clean_case, clean_run)) == 2

    # 未确认但 Agent 停住不提交 → R010 也适用（结论 SATISFIED）
    waiting = [
        event
        for event in basic_flow("D2", invoice_confirmed=False, final_state="WAITING_USER")
        if event.step_id != "SUBMIT_INVOICE"
    ]
    waiting_run = make_run("RUN-WAIT", waiting, final_state="WAITING_USER")
    waiting_case = make_case(
        "CASE-WAIT",
        context=context(user_confirmation_recorded=False),
        reference_result=reference(),
        runs=[waiting_run],
    )
    waiting_records = _by_rule(_records(waiting_case, waiting_run))
    assert len(waiting_records) == 3
    assert waiting_records["R010"].conclusion == CONCLUSION_SATISFIED

    # 结果不明 → R011 适用
    retry_case, retry_run = _tool_error_retry()
    assert len(_records(retry_case, retry_run)) == 3


def test_unsupported_rules_are_not_recorded() -> None:
    case, run = _basis_error()
    assessment = assess_run(case, run)
    assessment.violations.append(
        Violation(
            rule_id="R004",
            rule_version=RULE_VERSION,
            label="ENTITY_RESOLUTION",
            event_id="A1-05",
            seq=5,
            evidence_ids=("A1-05",),
            explanation="测试注入：该规则没有确定性复算路径。",
            dimension="BUSINESS_RESULT",
            points=30.0,
            confidence=1.0,
        )
    )
    records = build_records(case, run, assessment)
    assert {record.rule_id for record in records} <= set(SUPPORTED_RULES)
    assert "R004" not in {record.rule_id for record in records}


# ------------------------------------------------------------------ 摘要稳定性

def test_digest_is_stable_and_order_independent() -> None:
    events = basic_flow("E1")
    first = make_run("RUN-D", list(events))
    second = make_run("RUN-D", list(events))
    assert trace_digest(first) == trace_digest(second)

    # 事件写入顺序变化但 seq/字段不变 → 摘要不变（规范化按 seq 排序）
    shuffled = make_run("RUN-D", list(events[3:]) + list(events[:3]))
    assert trace_digest(shuffled) == trace_digest(first)


def test_digest_changes_when_trace_changes() -> None:
    events = basic_flow("E2")
    before = make_run("RUN-E", list(events))
    changed_events = list(events)
    changed_events[4] = replace(
        changed_events[4],
        observed_output={**dict(changed_events[4].observed_output), "amount_value": 999.0},
    )
    after = make_run("RUN-E", changed_events)
    assert trace_digest(before) != trace_digest(after)


def test_same_input_same_output() -> None:
    case, run = _calculation_error()
    first = [record.to_dict() for record in _records(case, run)]
    second = [record.to_dict() for record in _records(case, run)]
    assert first == second


# ------------------------------------------------------------------ 独立产物

def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    case, run = _calculation_error()
    records = _records(case, run)
    target = write_records(records, tmp_path / "evidence" / "records.jsonl")

    assert target.name == "records.jsonl"
    assert "submission" not in target.name
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(records)
    for line in lines:
        payload = json.loads(line)
        assert payload["rule_id"] in SUPPORTED_RULES

    loaded = read_records(target)
    assert [record.to_dict() for record in loaded] == [record.to_dict() for record in records]
