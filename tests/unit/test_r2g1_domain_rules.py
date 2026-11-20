"""R2-G1 P1-C：AI 顾问七域规则声明与跨域检查。"""

from __future__ import annotations

from tracelab.final_round.domains import (
    CROSS_DOMAIN_RULES,
    DOMAIN_RULES,
    get_domain_rule,
    validate_domain_rules,
)

EXPECTED = {
    "salary_tax",
    "bank_flow",
    "vat_invoice",
    "tax_payment",
    "social_insurance",
    "tax_notice",
    "mailing",
}


def test_seven_domains_have_structured_rules():
    assert set(DOMAIN_RULES) == EXPECTED
    assert validate_domain_rules() == []


def test_each_domain_distinguishes_completion_and_missing_facts():
    for _domain, rule in DOMAIN_RULES.items():
        assert rule["completion_vs_wait"]
        assert rule["missing_fact_policy"]
        assert rule["key_evidence"]
        assert isinstance(rule["dependencies"], list)


def test_unknown_domain_does_not_silently_use_another_rule():
    assert get_domain_rule("unknown") is None
    assert get_domain_rule(None) is None


def test_cross_domain_rules_are_explicit():
    assert CROSS_DOMAIN_RULES
    assert any("mailing" in key for key in CROSS_DOMAIN_RULES)
