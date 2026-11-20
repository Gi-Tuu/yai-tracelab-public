"""R2-G2 G2-C：七域跨域自造案例（不调用模型，验证标准不混用）。"""

from __future__ import annotations

from tracelab.final_round.domains import (
    CROSS_DOMAIN_RULES,
    DOMAIN_RULES,
    cross_domain_keys,
    get_domain_rule,
    get_rule_bundle,
)
from tracelab.final_round.judge_input import normalize_judge_input

DOMAINS = (
    "salary_tax",
    "bank_flow",
    "vat_invoice",
    "tax_payment",
    "social_insurance",
    "tax_notice",
    "mailing",
)


def _event(run_id: str) -> dict:
    return {
        "event_id": f"{run_id}-E01",
        "seq": 1,
        "step_id": "INPUT",
        "step_status": "SUCCEEDED",
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
    }


def _input(domain: str) -> dict:
    return {
        "business_type": "ai_consultant",
        "domain": domain,
        "user_request": f"{domain} 测试请求",
        "context": {"enterprise_id": "E1"},
        "business_objective": {"business_goal": DOMAIN_RULES[domain]["objective"]},
        "observable_trace": [_event(f"R-{domain}")],
    }


def test_seven_domain_inputs_keep_their_own_standard():
    objectives = set()
    for domain in DOMAINS:
        payload = normalize_judge_input(_input(domain), linkage={"run_id": f"R-{domain}"})
        assert payload["judge_input"]["domain"] == domain
        rule = get_domain_rule(domain)
        assert rule is not None
        assert payload["judge_input"]["business_objective"]["business_goal"] == rule["objective"]
        objectives.add(rule["objective"])
    assert len(objectives) == len(DOMAINS), "七个领域不应共享同一业务目标"


def test_cross_domain_request_keeps_both_rules():
    bundle = get_rule_bundle("salary_tax")
    assert bundle is not None
    assert "bank_flow" in bundle["dependencies"]
    key = cross_domain_keys("salary_tax", "bank_flow")
    assert key == "bank_flow+salary_tax"
    assert key in CROSS_DOMAIN_RULES


def test_unknown_domain_does_not_fall_back_to_another_rule():
    assert get_domain_rule("unknown") is None
    assert get_rule_bundle("unknown") is None


def test_mailing_does_not_silently_reuse_tax_notice_completion_rule():
    mailing = get_domain_rule("mailing")
    notice = get_domain_rule("tax_notice")
    assert mailing is not None and notice is not None
    assert mailing["completion_vs_wait"] != notice["completion_vs_wait"]
