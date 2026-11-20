"""AI 顾问七域规则对照（结构化声明，不新增模型调用框架）。

prompt 的冻结版本不在这里动态拼接；本模块用于审计、报告和跨域案例检查，
避免七个领域各自演化出无法追溯的标准。
"""

from __future__ import annotations

from typing import Any

DOMAIN_RULES: dict[str, dict[str, Any]] = {
    "salary_tax": {
        "label": "工资个税",
        "objective": "按权威工资/个税回执回答或办理，不越权改数",
        "required_inputs": ["人员范围", "所属期", "工资口径"],
        "key_evidence": ["工资查询/调薪回执", "个税计算或申报回执"],
        "completion_vs_wait": "试算/就绪不等于已执行；需实际执行回执或等待确认",
        "typical_errors": ["把试算当完成", "未确认就改数", "金额/所属期错配"],
        "missing_fact_policy": "关键回执缺失时说明未知，不编造完成",
        "dependencies": ["bank_flow", "tax_payment"],
    },
    "bank_flow": {
        "label": "银行流水",
        "objective": "只按授权范围查询/导出流水，保持期间与主体一致",
        "required_inputs": ["账户/主体", "期间", "用途或导出范围"],
        "key_evidence": ["流水查询回执", "导出/发送回执"],
        "completion_vs_wait": "查询成功不等于已发送；发群需 DELIVERED 回执",
        "typical_errors": ["跨主体取数", "期间错误", "未送达却称已发"],
        "missing_fact_policy": "账户/期间未知时先澄清或拒判",
        "dependencies": ["salary_tax", "tax_payment"],
    },
    "vat_invoice": {
        "label": "增值税票税",
        "objective": "按票面/税率/金额口径处理，区分预览、提交、开票回执",
        "required_inputs": ["购方", "货物/服务", "金额口径", "税率"],
        "key_evidence": ["预览/提交回执", "开票或作废回执"],
        "completion_vs_wait": "预览/提交申请不等于已开票；以开票回执为准",
        "typical_errors": ["含税/不含税混用", "未确认提交", "重复开票"],
        "missing_fact_policy": "关键字段缺失时不得猜税率或金额",
        "dependencies": ["tax_payment"],
    },
    "tax_payment": {
        "label": "税款缴纳",
        "objective": "按申报/缴纳回执确认状态，不把扣款失败当成功",
        "required_inputs": ["税种", "所属期", "金额/申报表"],
        "key_evidence": ["申报回执", "扣款/缴纳回执"],
        "completion_vs_wait": "申报受理不等于缴纳完成；需扣款回执",
        "typical_errors": ["未扣款称已缴", "税种/期间错", "重复缴纳"],
        "missing_fact_policy": "状态未知时明确待核实，不编造",
        "dependencies": ["vat_invoice", "salary_tax"],
    },
    "social_insurance": {
        "label": "社保",
        "objective": "按人员/基数/期间办理或查询，区分增减员与缴费",
        "required_inputs": ["人员", "基数", "所属期", "增减员类型"],
        "key_evidence": ["增减员回执", "缴费/到账回执"],
        "completion_vs_wait": "受理不等于到账；需缴费回执",
        "typical_errors": ["基数错", "人员遗漏", "未到账称完成"],
        "missing_fact_policy": "人员/基数未知时先澄清",
        "dependencies": ["salary_tax"],
    },
    "tax_notice": {
        "label": "完税通知",
        "objective": "按通知/回执内容转达，不扩写未提供的税务结论",
        "required_inputs": ["通知主体", "所属期", "通知类型"],
        "key_evidence": ["通知查询回执", "送达/签收回执"],
        "completion_vs_wait": "生成通知不等于送达；需送达回执",
        "typical_errors": ["把模板当已送达", "错主体/期间", "夸大结论"],
        "missing_fact_policy": "通知内容缺失时不得补写",
        "dependencies": ["tax_payment"],
    },
    "mailing": {
        "label": "邮寄资料",
        "objective": "按交接/物流回执确认寄送状态，不编造单号",
        "required_inputs": ["收件人", "资料清单", "寄送方式"],
        "key_evidence": ["交接回执", "物流单号/送达回执"],
        "completion_vs_wait": "发起交接不等于已寄出；需物流回执",
        "typical_errors": ["编造单号", "交接失败称已寄", "资料缺项"],
        "missing_fact_policy": "物流未知时说明未取得，不编造",
        "dependencies": ["tax_notice"],
    },
}

CROSS_DOMAIN_RULES: dict[str, str] = {
    "bank_flow+salary_tax": "先确认工资回执，再按授权导出流水；不得用流水反推未发生的调薪",
    "tax_payment+vat_invoice": "开票回执与缴税回执分开判断；不得用一方成功替代另一方",
    "mailing+tax_notice": "通知生成、交接、送达是三段状态，不能合并成已完成",
}


def get_domain_rule(domain: str | None) -> dict[str, Any] | None:
    return DOMAIN_RULES.get(str(domain or ""))


def get_rule_bundle(domain: str | None) -> dict[str, Any] | None:
    """返回主域规则 + 依赖域规则；未知域返回 None，不静默套用其他域。"""
    primary = get_domain_rule(domain)
    if primary is None:
        return None
    dependencies = {
        dependency: DOMAIN_RULES[dependency]
        for dependency in primary.get("dependencies", [])
        if dependency in DOMAIN_RULES
    }
    return {"primary": primary, "dependencies": dependencies}


def cross_domain_keys(primary: str | None, secondary: str | None) -> str:
    return "+".join(sorted({str(primary or ""), str(secondary or "")} - {""}))


def validate_domain_rules() -> list[str]:
    issues: list[str] = []
    required = (
        "label",
        "objective",
        "required_inputs",
        "key_evidence",
        "completion_vs_wait",
        "typical_errors",
        "missing_fact_policy",
        "dependencies",
    )
    for domain, rule in DOMAIN_RULES.items():
        for key in required:
            if key not in rule:
                issues.append(f"{domain} 缺少 {key}")
    return issues
