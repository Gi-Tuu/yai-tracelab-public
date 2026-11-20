"""G6-B2：依据记录独立检查器（`tracelab.evidence_verify`）单测。

三个完整演示 Case（金额 / 提交前确认 / 提交结果不明）在这里构造并被复核；
篡改用例覆盖 observed / expected / conclusion / source_event_ids 四类，
外加缺字段、缺事件、规则版本变化与批量统计。样本 100% 自造。
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Case, Run
from tracelab.evidence_record import (
    CONCLUSION_SATISFIED,
    CONCLUSION_VIOLATED,
    RULE_VERSION,
    EvidenceRecord,
    build_records,
)
from tracelab.evidence_verify import (
    STATUS_INSUFFICIENT,
    STATUS_MISMATCH,
    STATUS_VERIFIED,
    verify_record,
    verify_records,
    verify_records_for_cases,
)

# ------------------------------------------------------------------ 三个演示 Case

def _transferred(events: list) -> list:
    last = events[-1]
    raw = dict(last.raw)
    raw["step_id"] = "TRANSFER_TO_HUMAN"
    raw["result_code"] = "E700"
    return events[:-1] + [replace(last, step_id="TRANSFER_TO_HUMAN", result_code="E700", raw=raw)]


def _demo_cases() -> list[Case]:
    """演示 Case A/B/C（每个 Case 两条运行，便于对照正确与错误形态）。"""
    a1 = make_run(
        "RUN-A1",
        basic_flow(
            "A1",
            amount_basis="tax_inclusive",
            computed={"exclusive_amount": 943.4, "tax_amount": 56.6, "inclusive_amount": 1000.0},
        ),
    )
    a2 = make_run(
        "RUN-A2",
        basic_flow(
            "A2",
            amount_basis="tax_exclusive",
            computed={"exclusive_amount": 1000.0, "tax_amount": 130.0, "inclusive_amount": 1130.0},
        ),
    )
    case_a = make_case("CASE-A", context=context(), reference_result=reference(), runs=[a1, a2])

    b1 = make_run(
        "RUN-B1",
        basic_flow(
            "B1",
            invoice_confirmed=False,
            submit_status="rejected",
            submit_status_step="FAILED",
            submit_error="CONFIRMATION_REQUIRED",
            callback_status=None,
            final_state="FAILED",
            invoice_created=False,
        ),
        final_state="FAILED",
        invoice_created=False,
    )
    b2 = make_run("RUN-B2", basic_flow("B2", invoice_confirmed=False, submit_status="accepted"))
    case_b = make_case(
        "CASE-B",
        context=context(user_confirmation_recorded=False),
        reference_result=reference(),
        runs=[b1, b2],
    )

    c1 = make_run(
        "RUN-C1",
        _transferred(
            basic_flow(
                "C1",
                submit_status="unknown",
                submit_status_step="TIMED_OUT",
                callback_status=None,
                final_state="FAILED",
                invoice_created=False,
            )
        ),
        final_state="TRANSFERRED_TO_HUMAN",
        invoice_created=False,
    )
    c2 = make_run(
        "RUN-C2",
        basic_flow(
            "C2",
            submit_status="unknown",
            submit_status_step="TIMED_OUT",
            callback_status=None,
            retry_count=1,
            submit_attempts=2,
        ),
    )
    case_c = make_case(
        "CASE-C",
        context=context(),
        reference_result=reference(acceptable_snapshot_state="TRANSFERRED_TO_HUMAN"),
        runs=[c1, c2],
    )
    return [case_a, case_b, case_c]


def _all_records() -> tuple[list[Case], list[EvidenceRecord]]:
    cases = _demo_cases()
    records: list[EvidenceRecord] = []
    for case in cases:
        for run in case.runs:
            records += build_records(case, run, assess_run(case, run))
    return cases, records


def _case_of(cases: list[Case], case_id: str) -> Case:
    return next(case for case in cases if case.case_id == case_id)


def _run_of(case: Case, run_id: str) -> Run:
    run = case.run(run_id)
    assert run is not None
    return run


def _record_of(records: list[EvidenceRecord], case_id: str, run_id: str, rule_id: str) -> EvidenceRecord:
    for record in records:
        if (record.case_id, record.run_id, record.rule_id) == (case_id, run_id, rule_id):
            return record
    raise AssertionError(f"没有找到 {case_id}/{run_id}/{rule_id} 的依据记录")


def _tampered(record: EvidenceRecord) -> dict:
    return copy.deepcopy(record.to_dict())


def _failed_checks(result) -> list[str]:
    return [check.name for check in result.checks if check.ok is False]


# ------------------------------------------------------------------ 正常路径

def test_three_demo_cases_verify() -> None:
    cases, records = _all_records()
    assert len(records) == 15
    for record in records:
        case = _case_of(cases, record.case_id)
        run = _run_of(case, record.run_id)
        result = verify_record(record, case, run)
        assert result.status == STATUS_VERIFIED, (record.case_id, record.rule_id, result.detail)
        assert result.checks
        assert result.unverifiable_fields == ()
        assert all(check.ok is True for check in result.checks)

    by_key = {(r.case_id, r.run_id, r.rule_id): r for r in records}
    # Case A：口径错误 → R001 违规；税率错误 → R002 违规（算术闭合仍然成立）
    assert by_key[("CASE-A", "RUN-A1", "R001")].conclusion == CONCLUSION_VIOLATED
    assert by_key[("CASE-A", "RUN-A2", "R002")].conclusion == CONCLUSION_VIOLATED
    # Case B：两条运行都是"未确认即提交"，但工具是否拦下不同
    rejected = by_key[("CASE-B", "RUN-B1", "R010")]
    bypass = by_key[("CASE-B", "RUN-B2", "R010")]
    assert rejected.conclusion == bypass.conclusion == CONCLUSION_VIOLATED
    assert rejected.observed_values["run.guard_error_codes"]["value"] == ["CONFIRMATION_REQUIRED"]
    assert bypass.observed_values["run.guard_error_codes"]["value"] == []
    # Case C：结果不明 → 转人工正确；重试并宣称成功错误
    assert by_key[("CASE-C", "RUN-C1", "R011")].conclusion == CONCLUSION_SATISFIED
    assert by_key[("CASE-C", "RUN-C2", "R011")].conclusion == CONCLUSION_VIOLATED


def test_verify_is_repeatable() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A2", "R002")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A2")
    first = verify_record(record, case, run)
    second = verify_record(record, case, run)
    assert first.status == second.status == STATUS_VERIFIED
    assert [check.name for check in first.checks] == [check.name for check in second.checks]
    assert first.detail == second.detail


# ------------------------------------------------------------------ 四类篡改

def test_tampered_observed_value_is_mismatch() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["observed_values"]["collect.amount_basis"]["value"] = "tax_exclusive"
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    assert "observed_values[collect.amount_basis]" in _failed_checks(result)


def test_tampered_expected_value_is_mismatch() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["expected_values"]["reference.amount_basis"]["value"] = "tax_inclusive"
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    assert "expected_values[reference.amount_basis]" in _failed_checks(result)


def test_tampered_conclusion_is_mismatch() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-B", "RUN-B2", "R010")
    case = _case_of(cases, "CASE-B")
    run = _run_of(case, "RUN-B2")
    data = _tampered(record)
    assert data["conclusion"] == CONCLUSION_VIOLATED
    data["conclusion"] = CONCLUSION_SATISFIED
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    assert "conclusion_matches" in _failed_checks(result)
    assert "复算=VIOLATED" in result.detail


def test_tampered_source_event_ids_breaks_ref_coverage() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["source_event_ids"] = ["A1-01", "A1-02"]
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    failed = _failed_checks(result)
    assert "source_event_ids_cover_refs" in failed
    assert "source_events_relevant" in failed


def test_tampered_source_event_ids_cites_irrelevant_event() -> None:
    """R010 记录没有事件级取值地址，靠"相关事件范围"这一条独立检查拦住。"""
    cases, records = _all_records()
    record = _record_of(records, "CASE-B", "RUN-B1", "R010")
    case = _case_of(cases, "CASE-B")
    run = _run_of(case, "RUN-B1")
    data = _tampered(record)
    data["source_event_ids"] = ["B1-01", "B1-10"]
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    assert "source_events_relevant" in _failed_checks(result)


def test_tampered_condition_result_is_mismatch() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["required_conditions"][0]["result"] = True
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    assert "condition:amount_basis_matches_reference" in _failed_checks(result)


def test_tampered_digest_is_mismatch() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["trace_digest"] = "sha256:" + "0" * 64
    result = verify_record(data, case, run)
    assert result.status == STATUS_MISMATCH
    assert "trace_digest" in _failed_checks(result)


# ------------------------------------------------------------------ 证据不足

@pytest.mark.parametrize(
    "field",
    ["observed_values", "required_conditions", "conclusion", "trace_digest", "rulepack_version"],
)
def test_missing_field_is_insufficient(field: str) -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data.pop(field)
    result = verify_record(data, case, run)
    assert result.status == STATUS_INSUFFICIENT
    assert field in result.unverifiable_fields


def test_missing_event_is_insufficient() -> None:
    """引用了轨迹里不存在的事件 → 无法复核（不是"证明记录错"）。"""
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["source_event_ids"] = ["A1-05", "A1-99"]
    result = verify_record(data, case, run)
    assert result.status == STATUS_INSUFFICIENT
    assert "source_event_ids_present" in result.unverifiable_fields


def test_unresolvable_ref_is_insufficient() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    data = _tampered(record)
    data["observed_values"]["collect.amount_basis"]["ref"] = "event:A1-05:observed_output.no_such_field"
    result = verify_record(data, case, run)
    assert result.status == STATUS_INSUFFICIENT
    assert "observed_values[collect.amount_basis]" in result.unverifiable_fields


# ------------------------------------------------------------------ 规则版本

def test_rulepack_version_mismatch_is_mismatch() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    result = verify_record(record, case, run, rulepack_version="rulepack-9.9")
    assert result.status == STATUS_MISMATCH
    assert "rulepack_version" in _failed_checks(result)


def test_rulepack_version_pinned_ok() -> None:
    cases, records = _all_records()
    record = _record_of(records, "CASE-A", "RUN-A1", "R001")
    case = _case_of(cases, "CASE-A")
    run = _run_of(case, "RUN-A1")
    result = verify_record(record, case, run, rulepack_version=RULE_VERSION)
    assert result.status == STATUS_VERIFIED


# ------------------------------------------------------------------ 批量统计

def test_batch_statistics() -> None:
    cases, records = _all_records()
    batch = verify_records_for_cases(records, cases)
    assert batch.total == 15
    assert batch.counts == {STATUS_VERIFIED: 15, STATUS_MISMATCH: 0, STATUS_INSUFFICIENT: 0}
    assert sum(batch.counts.values()) == batch.total
    assert batch.by_rule["R001"] == {STATUS_VERIFIED: 6, STATUS_MISMATCH: 0, STATUS_INSUFFICIENT: 0}
    assert batch.by_rule["R002"][STATUS_VERIFIED] == 5
    assert batch.by_rule["R010"][STATUS_VERIFIED] == 2
    assert batch.by_rule["R011"][STATUS_VERIFIED] == 2
    payload = batch.to_dict()
    assert payload["total"] == 15
    assert len(payload["records"]) == 15


def test_batch_statistics_with_tamper() -> None:
    cases, records = _all_records()
    data = [copy.deepcopy(record.to_dict()) for record in records]
    data[0]["conclusion"] = CONCLUSION_SATISFIED if data[0]["conclusion"] == CONCLUSION_VIOLATED else CONCLUSION_VIOLATED
    data[1]["observed_values"]["collect.amount_basis"]["value"] = "tampered"
    batch = verify_records_for_cases(data, cases)
    assert batch.total == 15
    assert batch.counts == {STATUS_VERIFIED: 13, STATUS_MISMATCH: 2, STATUS_INSUFFICIENT: 0}


def test_verify_records_resolves_run_from_case() -> None:
    cases, records = _all_records()
    case = _case_of(cases, "CASE-B")
    subset = [record for record in records if record.case_id == "CASE-B"]
    assert len(subset) == 6
    batch = verify_records(subset, case)
    assert batch.total == 6
    assert batch.counts[STATUS_VERIFIED] == 6


def test_verify_records_reports_unknown_run() -> None:
    cases, records = _all_records()
    case = _case_of(cases, "CASE-B")
    subset = [record for record in records if record.case_id == "CASE-C"]
    batch = verify_records(subset, case)
    assert batch.total == len(subset)
    assert batch.counts[STATUS_INSUFFICIENT] == len(subset)
