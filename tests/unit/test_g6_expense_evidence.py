"""报销规则包（G6-C）的依据记录与独立复核。

报销包与开票规则共用同一套记录格式与检查器：
* 记录里的取值地址指向原始事件字段（`event:<id>:observed_output.*`）或检查器可重算的导出值；
* 检查器按 `rule_id` 前缀（`EXP*`）自动选择报销规则包版本，再做版本一致性核对；
* 全部使用自造样本，不接真实财务系统。
"""

from __future__ import annotations

import copy

from tests.builders import make_case, make_event, make_run
from tracelab.contracts import Case
from tracelab.evidence_record import (
    CONCLUSION_SATISFIED,
    CONCLUSION_VIOLATED,
    EXPENSE_SUPPORTED_RULES,
    build_expense_records,
    write_records,
)
from tracelab.evidence_verify import STATUS_MISMATCH, STATUS_VERIFIED, verify_record, verify_records
from tracelab.rulepacks import EXPENSE_PACK, EXPENSE_RULEPACK_VERSION, evaluate_with_pack


def _events(
    *,
    claim: float = 1000.0,
    lines: list[dict[str, object]] | None = None,
    budget: float = 5000.0,
    approvers: tuple[str, ...] = ("MANAGER", "FINANCE"),
    already_settled: bool = False,
    settle: bool = True,
) -> list:
    items = lines if lines is not None else [{"amount": 600.0}, {"amount": 400.0}]
    events = [
        make_event("E-01", 1, "RECEIVE_CLAIM", observed_output={
            "claim_amount": claim, "line_items": items, "already_settled": already_settled,
        }),
        make_event("E-02", 2, "CHECK_BUDGET", observed_output={"budget_remaining": budget}),
    ]
    seq = 3
    for approver in approvers:
        events.append(make_event(f"E-A{seq}", seq, f"APPROVE_{approver}"))
        seq += 1
    if settle:
        events.append(make_event("E-09", seq, "SETTLE_EXPENSE", observed_output={"settled": True}))
    return events


def _case(case_id: str, events: list) -> Case:
    run = make_run(f"{case_id}-A", events, final_state="COMPLETED", invoice_created=False)
    return make_case(case_id, context={}, reference_result={}, runs=[run])


def _records(case: Case) -> list:
    return build_expense_records(case, case.runs[0])


def test_expense_records_cover_all_four_rules_for_clean_case():
    case = _case("EXP-CLEAN", _events())
    records = _records(case)
    assert {record.rule_id for record in records} == set(EXPENSE_SUPPORTED_RULES)
    assert all(record.conclusion == CONCLUSION_SATISFIED for record in records)
    assert all(record.rulepack_version == EXPENSE_RULEPACK_VERSION for record in records)
    batch = verify_records(records, case, case.runs[0])
    assert batch.total == len(records)
    assert batch.counts[STATUS_VERIFIED] == len(records)


def test_expense_amount_mismatch_record_is_violated_and_verified():
    case = _case("EXP-AMT", _events(claim=1000.0, lines=[{"amount": 600.0}, {"amount": 300.0}]))
    record = next(r for r in _records(case) if r.rule_id == "EXP001")
    assert record.conclusion == CONCLUSION_VIOLATED
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED
    # 与规则包判定一致（记录与判定不能各说各话）
    assert "EXPENSE_AMOUNT_MISMATCH" in evaluate_with_pack(EXPENSE_PACK, case, case.runs[0]).labels


def test_expense_budget_record_is_violated():
    case = _case("EXP-BUD", _events(claim=9000.0, lines=[{"amount": 9000.0}], budget=1000.0))
    record = next(r for r in _records(case) if r.rule_id == "EXP002")
    assert record.conclusion == CONCLUSION_VIOLATED
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


def test_expense_approval_order_record_is_violated():
    case = _case("EXP-ORD", _events(approvers=("FINANCE", "MANAGER")))
    record = next(r for r in _records(case) if r.rule_id == "EXP003")
    assert record.conclusion == CONCLUSION_VIOLATED
    assert record.observed_values["derived.expense_approver_sequence"]["value"] == ["FINANCE", "MANAGER"]
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


def test_expense_duplicate_settlement_record_is_violated():
    case = _case("EXP-DUP", _events(already_settled=True))
    record = next(r for r in _records(case) if r.rule_id == "EXP004")
    assert record.conclusion == CONCLUSION_VIOLATED
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


def test_expense_records_skip_rules_without_facts():
    """没有结算动作时 EXP004 不适用 → 不产出记录（不把"不适用"写成"已核实"）。"""
    case = _case("EXP-NOSETTLE", _events(settle=False))
    assert "EXP004" not in {record.rule_id for record in _records(case)}


def test_expense_record_tampering_is_mismatch():
    case = _case("EXP-TAMPER", _events(claim=1000.0, lines=[{"amount": 600.0}, {"amount": 300.0}]))
    record = next(r for r in _records(case) if r.rule_id == "EXP001").to_dict()

    tampered_value = copy.deepcopy(record)
    tampered_value["observed_values"]["derived.expense_line_amounts"]["value"] = [600.0, 400.0]
    assert verify_record(tampered_value, case, case.runs[0]).status == STATUS_MISMATCH

    tampered_conclusion = copy.deepcopy(record)
    tampered_conclusion["conclusion"] = CONCLUSION_SATISFIED
    assert verify_record(tampered_conclusion, case, case.runs[0]).status == STATUS_MISMATCH

    tampered_source = copy.deepcopy(record)
    tampered_source["source_event_ids"] = ["EVENT-NOT-EXIST"]
    assert verify_record(tampered_source, case, case.runs[0]).status == STATUS_MISMATCH


def test_expense_version_binding():
    """报销记录默认按 EXP 规则包版本核对；显式给出别的版本时判不一致。"""
    case = _case("EXP-VER", _events())
    record = next(r for r in _records(case) if r.rule_id == "EXP002")
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED
    assert verify_record(
        record, case, case.runs[0], rulepack_version="rulepack-1.3"
    ).status == STATUS_MISMATCH


def test_expense_records_roundtrip_jsonl(tmp_path):
    case = _case("EXP-JSONL", _events())
    records = _records(case)
    path = write_records(records, tmp_path / "expense-records.jsonl")
    assert path.is_file()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(records)
