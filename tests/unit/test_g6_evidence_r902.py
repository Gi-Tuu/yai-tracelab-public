"""R902 的"拒判证据"：把"为什么只能判 UNKNOWN"变成可独立复核的记录。

R902 与其它规则不同：它的结论是"证据不足"，不是"Agent 做错"。
因此记录里断言的是**证据充分性**——断言不成立，才说明拒判有据。
检查器不接受记录里的布尔值，而是从原始事件重新数业务阶段、重新查采集事件与字段。
"""

from __future__ import annotations

import copy

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.attribution import assess_run
from tracelab.contracts import Case, Verdict
from tracelab.evidence_record import (
    CONCLUSION_SATISFIED,
    CONCLUSION_VIOLATED,
    build_records,
)
from tracelab.evidence_verify import STATUS_MISMATCH, STATUS_VERIFIED, verify_record


def _build(case_id: str, events: list, *, ctx=None, ref=None) -> tuple[Case, object]:
    run = make_run(f"{case_id}-A", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case(case_id, context=ctx or context(), reference_result=ref or reference(), runs=[run])
    return case, assess_run(case, run)


def _r902_record(case: Case, assessment) -> dict:
    records = build_records(case, case.runs[0], assessment)
    matched = [record for record in records if record.rule_id == "R902"]
    assert len(matched) == 1, f"应恰好一条 R902 记录，实际 {len(matched)}"
    return matched[0].to_dict()


def test_zero_event_run_has_no_r902_record_but_is_unknown():
    """零事件轨迹没有可引用事件：不产出 R902 记录（记录格式要求事件级引用），但判定仍是 UNKNOWN。"""
    case, assessment = _build("R902-EMPTY", [])
    assert assessment.verdict is Verdict.UNKNOWN
    assert not [r for r in build_records(case, case.runs[0], assessment) if r.rule_id == "R902"]


def test_shell_only_run_has_r902_record_and_verifies():
    """只有外壳阶段（RECEIVE/CREATE/COMPLETE）、没有任何业务阶段 → 拒判有据。"""
    events = [
        make_event("S-01", 1, "RECEIVE_REQUEST"),
        make_event("S-02", 2, "CREATE_TASK"),
        make_event("S-03", 3, "COMPLETE_TASK", result_code="E200"),
    ]
    case, assessment = _build("R902-SHELL", events)
    assert assessment.verdict is Verdict.UNKNOWN
    record = _r902_record(case, assessment)
    assert record["conclusion"] == CONCLUSION_VIOLATED
    assert record["source_event_ids"], "R902 记录必须能指向具体事件"
    assert record["observed_values"]["run.business_stage_count"]["value"] == 0
    verification = verify_record(record, case, case.runs[0])
    assert verification.status == STATUS_VERIFIED


def test_unknown_step_ids_run_has_r902_record_and_verifies():
    """事件全是不认识的 step_id → 业务阶段数为 0，拒判有据。"""
    events = [make_event(f"X-{i}", i + 1, f"OPAQUE_{i}") for i in range(3)]
    case, assessment = _build("R902-OPAQUE", events)
    assert assessment.verdict is Verdict.UNKNOWN
    record = _r902_record(case, assessment)
    assert record["conclusion"] == CONCLUSION_VIOLATED
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


def test_missing_collect_event_is_recorded_and_verified():
    """已进入确认/提交但缺采集事件 → 记录里 collect_event_present 为 False。"""
    events = [
        make_event("M-01", 1, "PARSE_INTENT", observed_output={"intent": "ISSUE", "invoice_task_type": "BLUE"}),
        make_event("M-02", 2, "CONFIRM_INVOICE", observed_output={"invoice_confirmed": True}),
        make_event("M-03", 3, "SUBMIT_INVOICE",
                   observed_input={"amounts": {"inclusive_amount": 1060.0}},
                   observed_output={"submit_status": "accepted"}),
    ]
    case, assessment = _build("R902-NOCOLLECT", events)
    assert assessment.verdict is Verdict.UNKNOWN
    record = _r902_record(case, assessment)
    assert record["observed_values"]["run.collect_event_present"]["value"] is False
    conditions = {c["name"]: c for c in record["required_conditions"]}
    assert conditions["collect_event_present"]["result"] is False
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


def test_collect_without_required_fields_is_recorded_and_verified():
    """有采集事件但没给出题内要求的任何字段 → 记录 required_fact_present 为 False。"""
    events = basic_flow(prefix="RF")
    for index, event in enumerate(events):
        if event.step_id == "COLLECT_INVOICE_DATA":
            from dataclasses import replace

            events[index] = replace(event, observed_output={"unrelated": "x"})
    case, assessment = _build("R902-NOFACT", events)
    assert assessment.verdict is Verdict.UNKNOWN
    record = _r902_record(case, assessment)
    assert record["observed_values"]["run.r902_required_fact_present"]["value"] is False
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


def test_healthy_run_has_no_r902_record():
    """证据充分的正常运行不产出 R902 记录（不能把"没拒判"也写成一条拒判证据）。"""
    case, assessment = _build("R902-OK", basic_flow(prefix="ok"))
    assert assessment.verdict is Verdict.PASS
    assert not [r for r in build_records(case, case.runs[0], assessment) if r.rule_id == "R902"]


def test_record_tampering_is_mismatch():
    events = [make_event(f"X-{i}", i + 1, f"OPAQUE_{i}") for i in range(3)]
    case, assessment = _build("R902-TAMPER", events)
    record = _r902_record(case, assessment)

    tampered = copy.deepcopy(record)
    tampered["observed_values"]["run.business_stage_count"]["value"] = 3
    assert verify_record(tampered, case, case.runs[0]).status == STATUS_MISMATCH

    tampered_conclusion = copy.deepcopy(record)
    tampered_conclusion["conclusion"] = CONCLUSION_SATISFIED
    assert verify_record(tampered_conclusion, case, case.runs[0]).status == STATUS_MISMATCH


def test_record_detects_changed_trace():
    """记录说"没有业务阶段"，但轨迹后来多了业务事件 → 复核必须发现（证据随轨迹变化）。"""
    from dataclasses import replace

    events = [make_event(f"X-{i}", i + 1, f"OPAQUE_{i}") for i in range(2)]
    case, assessment = _build("R902-DRIFT", events)
    record = _r902_record(case, assessment)

    run = case.runs[0]
    grown = replace(run, events=run.events + (make_event("X-9", 9, "COLLECT_INVOICE_DATA",
                                                         observed_output={"amount_value": 1000.0}),))
    result = verify_record(record, case, grown)
    assert result.status == STATUS_MISMATCH
