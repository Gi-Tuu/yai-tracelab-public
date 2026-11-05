"""P1-5：金额解析与传播证据的正确性测试。

两条不变量：
1. 无法解析的金额**不能**被当成"两个缺失值相等"，也不能静默通过规则；
2. 传播证据必须至少有一个有效比较字段，且必须比较**全部**字段。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.state import (
    MONEY_EQUAL,
    MONEY_INVALID,
    MONEY_MISSING,
    MONEY_UNEQUAL,
    MONEY_UNKNOWN,
    MONEY_UNSUPPORTED,
    money_compare,
    money_eq,
    parse_money,
    reconstruct,
)

# ---------------------------------------------------------------- 解析状态

@pytest.mark.parametrize(
    "value,status",
    [
        (None, MONEY_MISSING),
        ("abc", MONEY_INVALID),
        ("", MONEY_INVALID),
        ("   ", MONEY_INVALID),
        (True, MONEY_UNSUPPORTED),
        (False, MONEY_UNSUPPORTED),
        ({"a": 1}, MONEY_UNSUPPORTED),
        ([1], MONEY_UNSUPPORTED),
        (float("nan"), MONEY_INVALID),
        (float("inf"), MONEY_INVALID),
        (Decimal("1.2345678"), MONEY_INVALID),   # 小数位超过允许精度
        ("1000.00", "ok"),
        (1060.0, "ok"),
        (Decimal("60.5"), "ok"),
        (0, "ok"),
    ],
)
def test_parse_money_status(value, status):
    assert parse_money(value).status == status


def test_parse_money_keeps_value_for_valid_input():
    parsed = parse_money("1000.00")
    assert parsed.ok and parsed.value == Decimal("1000.00")


# ------------------------------------------------------------ 三态比较语义

def test_invalid_values_are_not_silently_equal():
    """旧实现会把 abc/xyz 都解析成 None 然后判相等——这是致命错误。"""
    assert money_compare("abc", "xyz") == MONEY_UNKNOWN
    assert money_eq("abc", "xyz") is False


def test_missing_vs_missing_is_unknown_not_equal():
    assert money_compare(None, None) == MONEY_UNKNOWN
    assert money_eq(None, None) is False


def test_invalid_vs_missing_is_unknown():
    assert money_compare("abc", None) == MONEY_UNKNOWN


def test_valid_string_vs_decimal_is_equal():
    assert money_compare("1000.00", Decimal("1000")) == MONEY_EQUAL
    assert money_eq("1000.00", Decimal("1000")) is True


def test_tolerance_and_boundary():
    assert money_compare(1060.0, 1060.004) == MONEY_EQUAL
    assert money_compare(1060.0, 1060.02) == MONEY_UNEQUAL


def test_bool_is_not_a_money():
    assert money_compare(True, 1) == MONEY_UNKNOWN
    assert money_eq(True, 1) is False


# ---------------------------------------------------- 传播证据（字段完备性）

@pytest.fixture()
def state():
    run = make_run("C1-A", basic_flow())
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    return reconstruct(case, run)


def test_empty_amounts_is_not_evidence(state):
    assert state.carries_amounts_detailed({}) == ()
    assert state.carries_amounts({}) == ()


def test_non_numeric_amounts_is_not_evidence(state):
    assert state.carries_amounts_detailed({"exclusive_amount": "abc"}) == ()


def test_full_amount_set_matches_with_field_detail(state):
    hits = state.carries_amounts_detailed(
        {"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0}
    )
    assert hits, "完整金额集合应当匹配到预览/提交事件"
    event, matched = hits[0]
    assert matched["exclusive_amount"] == "1000.00"
    assert matched["inclusive_amount"] == "1060.00"
    assert event.event_id


def test_partial_field_set_does_not_match(state):
    """只给一个字段就宣称传播，等于忽略其余字段——不允许。"""
    hits = state.carries_amounts_detailed(
        {"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0, "discount_amount": 0}
    )
    assert hits == (), "候选里缺 discount_amount，不应建立传播关系"


def test_different_amount_does_not_match(state):
    assert state.carries_amounts_detailed({"exclusive_amount": 9999.0}) == ()


def test_missing_field_in_candidate_does_not_match(state):
    # 目标里带一个候选没有的字段
    assert state.carries_amounts_detailed({"exclusive_amount": 1000.0, "unknown_field": 1}) == ()


def test_empty_amounts_object_in_event_is_not_evidence():
    """事件里 amounts 为空对象时，不能被当成"匹配成功"。"""
    events = basic_flow()
    # 把预览金额清空
    patched = []
    for e in events:
        if e.step_id == "CONFIRM_INVOICE":
            out = dict(e.observed_output)
            out["preview"] = {"amounts": {}}
            e = type(e)(
                event_id=e.event_id, seq=e.seq, step_id=e.step_id, module=e.module, action=e.action,
                step_status=e.step_status, result_code=e.result_code, error_code=e.error_code,
                error_message=e.error_message, retry_count=e.retry_count,
                observed_input=e.observed_input, observed_output=out, tool_calls=e.tool_calls,
                started_at=e.started_at, finished_at=e.finished_at, duration_ms=e.duration_ms,
                raw={**dict(e.raw), "observed_output": out},
            )
        patched.append(e)
    run = make_run("C2-A", patched)
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    st = reconstruct(case, run)
    hits = st.carries_amounts_detailed({"exclusive_amount": 1000.0})
    assert all(e.step_id != "CONFIRM_INVOICE" for e, _ in hits)


def test_propagation_edges_carry_field_evidence():
    """归因层建立的传播边必须能追溯到字段与值。"""
    from tracelab.attribution import assess_run

    run = make_run("C3-A", basic_flow(computed={"exclusive_amount": 900.0,
                                                "tax_amount": 54.0,
                                                "inclusive_amount": 954.0}))
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    assessment = assess_run(case, run)
    assert assessment.violations, "应命中金额计算错误"
    edges = assessment.propagation
    assert edges, "金额错误应产生传播边"
    assert any("=" in edge.why for edge in edges), f"传播边应包含字段与值：{[e.why for e in edges]}"
