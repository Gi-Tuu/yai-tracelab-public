"""契约层与业务状态重建（公共测试：不需要赛事资料包）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.builders import basic_flow, make_case, make_run
from tracelab.adapters import get_adapter, load_dataset, register_adapter
from tracelab.contracts import ADAPTER_OFFICIAL_VERSION
from tracelab.state import as_decimal, dig, money_eq, reconstruct, round_money

# ------------------------------------------------------------------ 金额语义

def test_decimal_rounding_is_half_up_not_bankers():
    assert round_money("2.345") == Decimal("2.35")
    assert round_money(2.344) == Decimal("2.34")


def test_money_eq_uses_tolerance_not_float_equality():
    assert money_eq(1060.0, "1060.00")
    assert money_eq(1060.004, 1060.0)
    assert not money_eq(1060.02, 1060.0)


def test_as_decimal_rejects_bool_and_garbage():
    assert as_decimal(True) is None
    assert as_decimal("abc") is None
    assert as_decimal(0) == Decimal("0")


def test_dig_returns_none_on_missing_path():
    assert dig({"a": {"b": 1}}, "a", "b") == 1
    assert dig({"a": 1}, "a", "b") is None
    assert dig(None, "a") is None


# ------------------------------------------------------------------ 状态重建

def test_reconstruct_collects_events_by_step():
    run = make_run("C1-A", basic_flow("C1"))
    case = make_case(runs=[run])
    st = reconstruct(case, run)
    assert st.buyer_id == "CUST-001"
    assert st.amount_basis == "tax_exclusive"
    assert st.invoice_confirmed is True
    assert st.callback_status == "success"
    assert st.submit_attempts == 1


def test_propagation_requires_actual_value_flow():
    """传播链必须依赖金额真的流到下游，而不是时间先后。"""
    run = make_run("C1-A", basic_flow("C1"))
    case = make_case(runs=[run])
    st = reconstruct(case, run)
    carriers = st.carries_amounts({"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0})
    steps = [e.step_id for e in carriers]
    assert "CONFIRM_INVOICE" in steps
    assert "SUBMIT_INVOICE" in steps
    assert "RECEIVE_REQUEST" not in steps


# -------------------------------------------------------------------- 适配器

def test_bad_json_is_reported_not_swallowed(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"case_id": "X", "runs": []}\nnot-json\n', encoding="utf-8")
    ds = load_dataset(bad)
    codes = {i.code for i in ds.issues}
    assert "BAD_JSON" in codes
    assert ds.errors
    assert any(i.category == "read" for i in ds.issues)


def test_duplicate_case_id_is_error(tmp_path):
    line = '{"case_id": "DUP", "runs": [], "context": {}, "reference_result": {}}'
    p = tmp_path / "dup.jsonl"
    p.write_text(line + "\n" + line + "\n", encoding="utf-8")
    ds = load_dataset(p)
    assert any(i.code == "DUPLICATE_CASE_ID" for i in ds.errors)


def test_adapter_registry_rejects_silent_overwrite():
    with pytest.raises(ValueError):
        register_adapter("official", type(get_adapter("official")))


def test_adapter_version_is_pinned():
    assert get_adapter("official").version == ADAPTER_OFFICIAL_VERSION


def test_partial_parse_marks_dataset_incomplete(tmp_path):
    p = tmp_path / "partial.jsonl"
    p.write_text('{"case_id": "A", "runs": []}\n{ broken\n', encoding="utf-8")
    ds = load_dataset(p)
    assert ds.stats["skipped"] == 1
    assert any(i.code == "PARTIAL_PARSE" for i in ds.errors)
    assert ds.read_complete is False


# ------------------------------------------------- 适配器健壮性（G5 新增）

def test_illegal_numeric_fields_do_not_crash(tmp_path):
    """非法 seq/retry_count/duration_ms 不能抛异常崩掉整批，要记 schema 问题并降级。"""
    bad_event = {
        "event_id": "E-1", "seq": "not-a-number", "step_id": "COLLECT_INVOICE_DATA",
        "module": "m", "action": "a", "step_status": "SUCCEEDED", "result_code": "E100",
        "error_code": None, "error_message": None, "retry_count": "x",
        "observed_input": {}, "observed_output": {}, "tool_calls": [],
    }
    case = {
        "case_id": "CASE-BAD", "scenario": "s", "user_request": "r",
        "context": {}, "reference_result": {},
        "runs": [
            {"run_id": "R1", "agent_version": "v", "task_id": "t", "trace_id": "tr",
             "events": [bad_event], "final_state": "FAILED", "invoice_created": False},
            {"run_id": "R2", "agent_version": "v", "task_id": "t", "trace_id": "tr",
             "events": [dict(bad_event, event_id="E-2")], "final_state": "FAILED",
             "invoice_created": False},
        ],
    }
    p = tmp_path / "bad_numeric.jsonl"
    p.write_text(__import__("json").dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    ds = load_dataset(p)
    assert len(ds) == 1
    codes = {i.code for i in ds.issues}
    assert "BAD_FIELD_TYPE" in codes
    assert ds.schema_valid is False, "非法字段必须让 schema_valid 为 False"
    assert ds.read_complete is True, "读取本身没坏，read_complete 应为 True"


def test_wrong_observed_type_is_recorded(tmp_path):
    event = {
        "event_id": "E-1", "seq": 1, "step_id": "X", "module": "m", "action": "a",
        "step_status": "SUCCEEDED", "result_code": "E100", "error_code": None,
        "error_message": None, "retry_count": 0,
        "observed_input": "not-an-object", "observed_output": ["list"], "tool_calls": "nope",
    }
    case = {
        "case_id": "CASE-TYPE", "scenario": "s", "user_request": "r",
        "context": {}, "reference_result": {},
        "runs": [{"run_id": f"R{i}", "agent_version": "v", "task_id": "t", "trace_id": "tr",
                  "events": [dict(event, event_id=f"E-{i}")], "final_state": "FAILED",
                  "invoice_created": False} for i in (1, 2)],
    }
    p = tmp_path / "bad_types.jsonl"
    p.write_text(__import__("json").dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    ds = load_dataset(p)
    codes = {i.code for i in ds.issues}
    assert {"BAD_OBSERVED_INPUT", "BAD_OBSERVED_OUTPUT", "BAD_TOOL_CALLS"} <= codes
    assert ds.schema_valid is False
