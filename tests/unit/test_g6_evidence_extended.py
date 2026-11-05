"""G6-B 扩展：依据记录与独立复核覆盖 R008 / R014 / R015。

三条规则分别对应三种"证据形态"：
* R008 取消误判：以**轨迹里有没有取消步骤 + 取消后有没有提交**为证据；
* R014 红票原票：以**采集事件里的原票引用/金额是否缺失、以及是否已进入确认/提交**为证据；
* R015 多轮旧值：以**实际金额是否命中被覆盖轮次的金额**为证据（命不中就不出该规则记录）。

每个被支持规则都要能：① 生成可复算记录；② 独立复核 VERIFIED；③ 被篡改后 MISMATCH。
"""

from __future__ import annotations

import copy

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Case, Event
from tracelab.evidence_record import (
    CONCLUSION_SATISFIED,
    CONCLUSION_VIOLATED,
    SUPPORTED_RULES,
    build_records,
    write_records,
)
from tracelab.evidence_verify import (
    STATUS_INSUFFICIENT,
    STATUS_MISMATCH,
    STATUS_VERIFIED,
    verify_record,
    verify_records,
)

# ------------------------------------------------------------------ 样本构造


def _case(case_id: str, events: list[Event], *, ctx=None, ref=None) -> tuple[Case, object]:
    run = make_run(f"{case_id}-A", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case(case_id, context=ctx or context(), reference_result=ref or reference(), runs=[run])
    return case, assess_run(case, run)


def _cancel_misread_case() -> tuple[Case, object]:
    """题内要求取消，Agent 继续推进到提交（轨迹里没有 CANCEL_TASK）。"""
    ref = reference(required_action="CANCEL")
    return _case("R008-BAD", basic_flow(prefix="c8"), ref=ref)


def _cancel_correct_case() -> tuple[Case, object]:
    """题内要求取消，Agent 真的取消且没有提交。"""
    events = [
        make_event("K-01", 1, "RECEIVE_REQUEST"),
        make_event("K-02", 2, "PARSE_INTENT", observed_output={"intent": "CANCEL"}),
        make_event("K-03", 3, "CANCEL_TASK", result_code="E800"),
    ]
    ref = reference(required_action="CANCEL", acceptable_snapshot_state="CANCELED",
                    invoice_should_exist=False)
    return _case("R008-OK", events, ref=ref)


def _red_invoice_missing_case() -> tuple[Case, object]:
    ref = reference(required_action="ASK_ORIGINAL_INVOICE_AND_AMOUNT")
    return _case("R014-BAD", basic_flow(prefix="r14"), ref=ref)


def _red_invoice_complete_case() -> tuple[Case, object]:
    events = basic_flow(prefix="r14ok")
    for index, event in enumerate(events):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["original_invoice_id"] = "INV-ORIGINAL-001"
            from dataclasses import replace

            events[index] = replace(event, observed_output=output)
    ref = reference(required_action="ASK_ORIGINAL_INVOICE_AND_AMOUNT")
    return _case("R014-OK", events, ref=ref)


def _rounds_context() -> dict:
    return context(conversation_rounds=[
        {"round": 1, "exclusive_amount": 900.0},
        {"round": 2, "supersedes_round": 1, "exclusive_amount": 1000.0},
    ])


def _stale_round_case() -> tuple[Case, object]:
    events = basic_flow(prefix="r15bad", amount_value=900.0)
    return _case("R015-BAD", events, ctx=_rounds_context())


def _latest_round_case() -> tuple[Case, object]:
    events = basic_flow(prefix="r15ok", amount_value=1000.0)
    return _case("R015-OK", events, ctx=_rounds_context())


# ------------------------------------------------------------------ 记录生成


def test_supported_rules_include_the_three_added_rules():
    assert {"R008", "R014", "R015"} <= set(SUPPORTED_RULES)
    # 第三轮加入 R902（拒判证据）
    assert "R902" in SUPPORTED_RULES
    assert len(SUPPORTED_RULES) == 8


def test_cancel_misread_record_is_generated_and_violated():
    case, assessment = _cancel_misread_case()
    records = {record.rule_id: record for record in build_records(case, case.runs[0], assessment)}
    assert "R008" in records
    record = records["R008"]
    assert record.conclusion == CONCLUSION_VIOLATED
    assert record.label == "INTENT_CLASSIFICATION"
    assert record.source_event_ids, "必须能指向具体事件"
    # 证据里要能看到"没有取消步骤"这个事实
    assert record.observed_values["run.step_ids"]["value"]


def test_cancel_correct_record_is_satisfied():
    case, assessment = _cancel_correct_case()
    records = {record.rule_id: record for record in build_records(case, case.runs[0], assessment)}
    assert "R008" in records
    assert records["R008"].conclusion == CONCLUSION_SATISFIED


def test_red_invoice_record_violated_when_original_missing():
    case, assessment = _red_invoice_missing_case()
    records = {record.rule_id: record for record in build_records(case, case.runs[0], assessment)}
    assert "R014" in records
    assert records["R014"].conclusion == CONCLUSION_VIOLATED
    assert records["R014"].observed_values["collect.original_invoice_id"]["value"] is None


def test_red_invoice_record_satisfied_when_original_present():
    case, assessment = _red_invoice_complete_case()
    records = {record.rule_id: record for record in build_records(case, case.runs[0], assessment)}
    assert "R014" in records
    assert records["R014"].conclusion == CONCLUSION_SATISFIED


def test_stale_round_record_violated():
    case, assessment = _stale_round_case()
    records = {record.rule_id: record for record in build_records(case, case.runs[0], assessment)}
    assert "R015" in records
    assert records["R015"].conclusion == CONCLUSION_VIOLATED
    assert records["R015"].observed_values["run.r015_stale_value_reused"]["value"] is True


def test_latest_round_record_satisfied():
    case, assessment = _latest_round_case()
    records = {record.rule_id: record for record in build_records(case, case.runs[0], assessment)}
    assert "R015" in records
    assert records["R015"].conclusion == CONCLUSION_SATISFIED


def test_no_round_context_means_no_r015_record():
    """没有"被覆盖的旧轮次"时不应产出 R015 记录（不能把"不适用"伪装成"已核实"）。"""
    case, assessment = _case("R015-NA", basic_flow(prefix="na"))
    records = build_records(case, case.runs[0], assessment)
    assert "R015" not in {record.rule_id for record in records}


# ------------------------------------------------------------------ 独立复核


def test_verifier_confirms_all_new_records():
    for builder in (_cancel_misread_case, _cancel_correct_case,
                    _red_invoice_missing_case, _red_invoice_complete_case,
                    _stale_round_case, _latest_round_case):
        case, assessment = builder()
        records = build_records(case, case.runs[0], assessment)
        new_rules = [r for r in records if r.rule_id in {"R008", "R014", "R015"}]
        assert new_rules, f"{builder.__name__} 应至少产出一条新规则记录"
        for record in new_rules:
            result = verify_record(record, case, case.runs[0])
            assert result.status == STATUS_VERIFIED, (record.rule_id, result.to_dict())


def test_batch_verification_counts_match_records():
    case, assessment = _cancel_misread_case()
    records = build_records(case, case.runs[0], assessment)
    batch = verify_records(records, case, case.runs[0])
    assert batch.total == len(records)
    assert batch.counts[STATUS_VERIFIED] == len(records)


def test_tampered_observed_value_is_mismatch():
    case, assessment = _cancel_misread_case()
    records = build_records(case, case.runs[0], assessment)
    record = next(r for r in records if r.rule_id == "R008")
    payload = copy.deepcopy(record.to_dict())
    payload["observed_values"]["run.submit_invoice_event_count"]["value"] = 0
    result = verify_record(payload, case, case.runs[0])
    assert result.status == STATUS_MISMATCH


def test_tampered_conclusion_is_mismatch():
    for builder in (_cancel_misread_case, _red_invoice_missing_case, _stale_round_case):
        case, assessment = builder()
        records = build_records(case, case.runs[0], assessment)
        record = next(r for r in records if r.rule_id in {"R008", "R014", "R015"})
        payload = copy.deepcopy(record.to_dict())
        payload["conclusion"] = (
            CONCLUSION_SATISFIED if record.conclusion == CONCLUSION_VIOLATED else CONCLUSION_VIOLATED
        )
        result = verify_record(payload, case, case.runs[0])
        assert result.status == STATUS_MISMATCH, record.rule_id


def test_missing_source_event_is_insufficient_or_mismatch():
    case, assessment = _red_invoice_missing_case()
    records = build_records(case, case.runs[0], assessment)
    record = next(r for r in records if r.rule_id == "R014")
    payload = copy.deepcopy(record.to_dict())
    payload["source_event_ids"] = ["EVENT-THAT-DOES-NOT-EXIST"]
    result = verify_record(payload, case, case.runs[0])
    assert result.status in (STATUS_MISMATCH, STATUS_INSUFFICIENT)


def test_rulepack_version_mismatch_is_mismatch():
    case, assessment = _cancel_misread_case()
    records = build_records(case, case.runs[0], assessment)
    record = next(r for r in records if r.rule_id == "R008")
    result = verify_record(record, case, case.runs[0], rulepack_version="rulepack-0.0-other")
    assert result.status == STATUS_MISMATCH


def test_records_roundtrip_through_jsonl(tmp_path):
    case, assessment = _stale_round_case()
    records = build_records(case, case.runs[0], assessment)
    path = write_records(records, tmp_path / "records.jsonl")
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip(), "记录文件不应为空"
