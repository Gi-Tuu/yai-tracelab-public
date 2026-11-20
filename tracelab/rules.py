"""确定性规则引擎入口：规则定义 + 按冻结顺序注册 + 引擎 API。

实现按职责拆到 `rule_defs.py`（类型/注册表）、`rules_business.py`（R001–R008）、
`rules_control.py`（R009–R015、R902）、`rule_engine.py`（执行/去重/兜底）。
公开导入名称保持不变；`RULES` 的注册顺序被 `test_rule_registration_order` 冻结。
"""

from __future__ import annotations

from . import rule_engine  # noqa: F401  # 触发规则模块按顺序注册
from .rule_defs import RULE_VERSION, RULES, RuleSpec, Violation, rule
from .rule_engine import (
    DECISION_POLICY,
    evaluate_rules,
    evaluate_with_trace,
    unexplained_failure,
)

__all__ = [
    "RULE_VERSION", "RULES", "RuleSpec", "Violation", "rule",
    "DECISION_POLICY", "evaluate_rules", "evaluate_with_trace", "unexplained_failure",
]
