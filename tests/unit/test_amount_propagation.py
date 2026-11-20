"""WS-C：金额传播完整性回归测试。

三条不变量：

1. 目标金额组里只要**有一个字段不是合法金额**（缺失 / 非法 / 类型不支持），
   就不得建立任何传播边——绝不能退化成"只比较剩下的合法字段"；
2. 没建立边的原因必须可查询，并能区分 缺失 / 非法 / 类型不支持 三种情况；
3. 传播边必须记录**字段名 + 规范化后的值**，且候选侧非法仍然不算匹配。
"""

from __future__ import annotations

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.state import (
    MONEY_INVALID,
    MONEY_MISSING,
    MONEY_UNSUPPORTED,
    AmountPropagation,
    reconstruct,
)

#: 一组完整合法的金额（与 builders.basic_flow 的默认口径一致）
FULL_AMOUNTS = {"exclusive_amount": 1000.0, "tax_amount": 60.0, "inclusive_amount": 1060.0}


@pytest.fixture()
def state():
    run = make_run("C1-A", basic_flow())
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    return reconstruct(case, run)


def steps(edges) -> list[str]:
    return [e.step_id for e, _ in edges]


# ------------------------------------------------ 混合输入：任一非法即零边

def test_invalid_field_blocks_all_edges(state):
    """合法 + 非法：tax_amount="abc" 必须让整组金额失效，而不是只比 total。"""
    probe = state.carries_amounts_probe({"exclusive_amount": 1000.0, "tax_amount": "abc"})

    assert isinstance(probe, AmountPropagation)
    assert probe.edges == (), "含非法字段的金额组不得建立传播边"
    assert probe.rejected["tax_amount"].status == MONEY_INVALID
    assert "abc" in probe.rejected["tax_amount"].reason
    # 兼容接口必须同步收紧：旧实现会丢掉非法字段、只比 exclusive_amount 后误建边
    assert state.carries_amounts_detailed({"exclusive_amount": 1000.0, "tax_amount": "abc"}) == ()
    assert state.carries_amounts({"exclusive_amount": 1000.0, "tax_amount": "abc"}) == ()


def test_missing_field_blocks_all_edges(state):
    """合法 + 缺失：缺失字段既不能当 0，也不能被静默忽略。"""
    probe = state.carries_amounts_probe({"exclusive_amount": 1000.0, "tax_amount": None})

    assert probe.edges == ()
    assert probe.rejected["tax_amount"].status == MONEY_MISSING
    assert state.carries_amounts_detailed({"exclusive_amount": 1000.0, "tax_amount": None}) == ()


def test_unsupported_type_blocks_all_edges(state):
    """合法 + 类型不支持：列表不是金额，不得与任何候选建立传播链。"""
    probe = state.carries_amounts_probe({"exclusive_amount": 1000.0, "tax_amount": ["60.00"]})

    assert probe.edges == ()
    assert probe.rejected["tax_amount"].status == MONEY_UNSUPPORTED
    assert state.carries_amounts_detailed({"exclusive_amount": 1000.0, "tax_amount": ["60.00"]}) == ()


def test_invalid_field_cannot_be_rescued_by_matching_siblings(state):
    """修 bug 回归：非法字段不能因为"其它字段都匹配"就产生传播边。"""
    # 先证明两个合法字段本身确实能匹配——这正是旧实现误报的入口
    assert state.carries_amounts_detailed(
        {"exclusive_amount": 1000.0, "inclusive_amount": 1060.0}
    ), "前提：两个合法字段单独比较时能匹配"

    mixed = {"exclusive_amount": 1000.0, "inclusive_amount": 1060.0, "tax_amount": "abc"}
    probe = state.carries_amounts_probe(mixed)

    assert probe.edges == (), "非法 tax_amount 必须让整组金额整体失效"
    assert state.carries_amounts(mixed) == ()
    assert set(probe.rejected) == {"tax_amount"}


def test_all_invalid_fields_listed_in_rejected(state):
    """所有字段都不合法：零边，且三种拒绝原因各自可区分。"""
    probe = state.carries_amounts_probe(
        {"exclusive_amount": "abc", "tax_amount": None, "inclusive_amount": {"amount": 1060.0}}
    )

    assert probe.edges == ()
    assert {key: parsed.status for key, parsed in probe.rejected.items()} == {
        "exclusive_amount": MONEY_INVALID,
        "tax_amount": MONEY_MISSING,
        "inclusive_amount": MONEY_UNSUPPORTED,
    }


# ---------------------------------------------- 全合法：候选缺字段 / 完整匹配

def test_valid_but_incomplete_candidate_has_no_edge(state):
    """全部合法但候选缺一个字段：零边（缺字段不自动忽略）。"""
    probe = state.carries_amounts_probe({**FULL_AMOUNTS, "discount_amount": 0})

    assert probe.edges == ()
    # 拒绝发生在候选侧，不是目标解析失败，因此 rejected 为空
    assert probe.rejected == {}


def test_full_valid_amounts_produce_edges_with_field_evidence(state):
    """全部合法且完整匹配：有边，且证据里字段名与规范化值都正确。"""
    probe = state.carries_amounts_probe(FULL_AMOUNTS)

    assert probe.rejected == {}
    assert probe.edges, "完整合法的金额组应当建立传播边"
    assert "CONFIRM_INVOICE" in steps(probe.edges)
    assert "SUBMIT_INVOICE" in steps(probe.edges)

    event, matched = probe.edges[0]
    assert matched == {
        "exclusive_amount": "1000.00",
        "tax_amount": "60.00",
        "inclusive_amount": "1060.00",
    }
    assert event.seq > 0
    # 兼容接口与 probe 的边完全一致
    assert probe.edges == state.carries_amounts_detailed(FULL_AMOUNTS)


def test_candidate_invalid_field_does_not_match():
    """候选侧金额非法时不算匹配（现有语义保持不变）。"""
    events = basic_flow(
        preview_amounts={"exclusive_amount": 1000.0, "tax_amount": "abc",
                         "inclusive_amount": 1060.0},
        submit_amounts=dict(FULL_AMOUNTS),
    )
    run = make_run("C2-A", events)
    case = make_case(context=context(), reference_result=reference(), runs=[run])
    st = reconstruct(case, run)

    probe = st.carries_amounts_probe(FULL_AMOUNTS)
    assert "CONFIRM_INVOICE" not in steps(probe.edges)
    assert "SUBMIT_INVOICE" in steps(probe.edges)


# ------------------------------------------------------------ 空金额组边界

def test_empty_amount_group_is_not_evidence(state):
    """空金额组不是传播证据：零边、无拒绝明细。"""
    probe = state.carries_amounts_probe({})

    assert probe == AmountPropagation()
    assert probe.edges == () and probe.rejected == {}
    assert state.carries_amounts_detailed({}) == ()
    assert state.carries_amounts({}) == ()
