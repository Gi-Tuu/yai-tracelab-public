"""第二轮审计修复的回归测试（2026-11-04 R2-01…R2-14）。

这批修复的核心是"拒判过宽"：上一轮把"无法判断"收紧后矫枉过正，
导致未知步骤/金额扩展键/事实来自档案的合法轨迹被误判 UNKNOWN。
这里逐条钉住"该拒判的仍拒判、不该拒判的恢复判定"。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from tests.builders import basic_flow, context, make_case, make_event, make_run, reference
from tracelab.adapters import STEP_ALIASES, load_dataset
from tracelab.attribution import assess_run
from tracelab.contracts import Verdict
from tracelab.evaluation import LabelFormatError, evaluate_dataset, load_reference_labels
from tracelab.evidence_record import build_records
from tracelab.evidence_verify import STATUS_VERIFIED, verify_record

ROOT = Path(__file__).resolve().parents[2]


def _assess(events, *, ctx=None, ref=None):
    run = make_run("R1", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case("C1", context=ctx or context(), reference_result=ref or reference(), runs=[run])
    return case, assess_run(case, run)


def _raw_events(rename=None, extra=None):
    events = []
    for event in basic_flow(prefix="x"):
        raw = json.loads(json.dumps(event.raw))
        if rename and raw["step_id"] == rename[0]:
            for key in ("step_id", "module", "action"):
                raw[key] = rename[1]
        events.append(raw)
    if extra:
        events.append(json.loads(json.dumps({
            "event_id": "X-99", "seq": 99, "step_id": extra, "module": extra, "action": extra,
            "step_status": "SUCCEEDED", "result_code": "E100", "error_code": None,
            "error_message": None, "retry_count": 0, "observed_input": {}, "observed_output": {},
            "tool_calls": [],
        })))
    return events


def _write_dataset(tmp_path: Path, raws: list[dict], name: str = "cases.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in raws) + "\n", encoding="utf-8")
    return path


def _raw_case(case_id: str, event_raws: list[dict], *, ctx=None, ref=None) -> dict:
    case = make_case(case_id, context=ctx or context(), reference_result=ref or reference(), runs=[])
    raw = dict(case.raw)
    runs = []
    for index in range(2):
        run = make_run(f"{case_id}-R{index + 1}", [], final_state="SUCCEEDED",
                       invoice_created=True, agent_version=f"v{index + 1}")
        runs.append(dict(run.raw, events=json.loads(json.dumps(event_raws))))
    raw["runs"] = runs
    return raw


# ---------------------------------------------------------------- R2-01 未知步骤


def test_r2_01_extra_non_business_step_keeps_verdict(tmp_path):
    """额外通知类步骤不再把正确运行打成 UNKNOWN（审计 R2-01）。"""
    events = list(basic_flow(prefix="n1")) + [make_event("N-99", 99, "SEND_NOTIFICATION")]
    case, assessment = _assess(events)
    assert assessment.verdict is Verdict.PASS
    assert any("未识别步骤" in note for note in assessment.uncertainties)
    dataset = load_dataset(_write_dataset(tmp_path, [_raw_case("N1", _raw_events(extra="SEND_NOTIFICATION"))]))
    # 非业务步骤只记 warn：数据完整性不因此变红
    assert not [issue for issue in dataset.errors if issue.code == "UNKNOWN_STEP_ID"]


def test_r2_01_renamed_business_step_still_refuses(tmp_path):
    """提交步骤被改名仍必须拒判（关键阶段缺失 + 未知步骤），且记 error。"""
    dataset = load_dataset(_write_dataset(
        tmp_path, [_raw_case("N2", _raw_events(rename=("SUBMIT_INVOICE", "INVOICE_SUBMIT")))]
    ))
    codes = {issue.code for issue in dataset.errors}
    assert "UNKNOWN_STEP_ID" in codes
    assessment = assess_run(dataset.cases[0], dataset.cases[0].runs[0])
    assert assessment.verdict is Verdict.UNKNOWN


def test_r2_01_generic_alias_table_maps_common_names():
    assert STEP_ALIASES["SUBMIT"] == "SUBMIT_INVOICE"
    assert STEP_ALIASES["COLLECT"] == "COLLECT_INVOICE_DATA"


# ---------------------------------------------------------------- R2-02 金额扩展键


def test_r2_02_non_money_keys_do_not_refuse():
    events = []
    for event in basic_flow(prefix="m1"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["computed_amounts"] = {**dict(output.get("computed_amounts") or {}), "currency": "CNY"}
            event = replace(event, observed_output=output)
        events.append(event)
    _, assessment = _assess(events)
    assert assessment.verdict is Verdict.PASS


def test_r2_02_non_scalar_money_value_still_refuses():
    events = []
    for event in basic_flow(prefix="m2"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["computed_amounts"] = {**dict(output.get("computed_amounts") or {}),
                                          "tax_amount": {"value": 60}}
            event = replace(event, observed_output=output)
        events.append(event)
    _, assessment = _assess(events)
    assert assessment.verdict is Verdict.UNKNOWN


# ---------------------------------------------------------------- R2-03 同 seq 定序


def test_r2_03_same_seq_records_match_verdict():

    early_complete = make_event("EV-A", 5, "COLLECT_INVOICE_DATA",
                                observed_output={"original_invoice_id": "INV-OLD", "amount_value": 1000.0,
                                                 "buyer_id": "CUST-001", "item_id": "ITEM-006"})
    late_missing = make_event("EV-Z", 5, "COLLECT_INVOICE_DATA",
                              observed_output={"original_invoice_id": None, "amount_value": 1000.0,
                                               "buyer_id": "CUST-001", "item_id": "ITEM-006"})
    events = [early_complete, late_missing, make_event("C1", 6, "CONFIRM_INVOICE"),
              make_event("S1", 7, "SUBMIT_INVOICE")]
    run = make_run("R1", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case("C1", context=context(),
                     reference_result=reference(required_action="ASK_ORIGINAL_INVOICE_AND_AMOUNT"),
                     runs=[run])
    assessment = assess_run(case, run)
    record = next(r for r in build_records(case, run, assessment) if r.rule_id == "R014")
    rule_violated = "FIELD_FABRICATION" in assessment.labels
    assert (record.conclusion == "VIOLATED") is rule_violated
    # R3-C-01: MISMATCH 正是「记录引用错事件」的返回态，不能当通过；必须严格 VERIFIED。
    status = verify_record(record, case, run)
    assert status.status == STATUS_VERIFIED, status.detail
    assert set(record.source_event_ids) <= {e.event_id for e in run.events}


# ---------------------------------------------------------------- R2-04 规则绑定


def test_r2_04_alt_amount_keys_still_verify():
    ref = reference(expected_amounts={"exclusive": 1000.0, "tax": 60.0, "inclusive": 1060.0})
    events = []
    for event in basic_flow(prefix="ak"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output["computed_amounts"] = {"exclusive": 1000.0, "tax": 60.0, "inclusive": 1060.0}
            event = replace(event, observed_output=output)
        events.append(event)
    case, assessment = _assess(events, ref=ref)
    assert assessment.verdict is Verdict.PASS
    record = next(r for r in build_records(case, case.runs[0], assessment) if r.rule_id == "R002")
    assert verify_record(record, case, case.runs[0]).status == STATUS_VERIFIED


# ---------------------------------------------------------------- R2-05 事实来源


def test_r2_05_fact_from_context_scalar_is_usable():
    events = []
    for event in basic_flow(prefix="fc"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output.pop("item_id", None)
            event = replace(event, observed_output=output)
        events.append(event)
    _, assessment = _assess(events, ctx=context(item_id="ITEM-006"), ref=reference(item_id="ITEM-006"))
    assert assessment.verdict is Verdict.PASS
    assert assessment.state.item_id == "ITEM-006"


def test_r2_05_ambiguous_candidates_still_refuse():
    events = []
    for event in basic_flow(prefix="fa"):
        if event.step_id == "COLLECT_INVOICE_DATA":
            output = dict(event.observed_output)
            output.pop("item_id", None)
            event = replace(event, observed_output=output)
        events.append(event)
    _, assessment = _assess(events, ref=reference(item_id="ITEM-006"))
    assert assessment.verdict is Verdict.UNKNOWN


# ---------------------------------------------------------------- R2-06 消融分母


def test_r2_06_ablation_report_shows_denominators_and_refuses_diff():
    from tracelab.ablation import build_markdown, run_ablation

    dataset = load_dataset(ROOT / "data" / "sample" / "sample-traces.jsonl")
    labels = {
        case.case_id: {"case_id": case.case_id, "run_assessments": [
            {"run_id": run.run_id, "agent_task_success": True,
             "business_invoice_created": bool(run.invoice_created), "root_causes": []}
            for run in case.runs
        ], "version_direction_on_this_case": "equal"}
        for case in dataset.cases
    }
    text = build_markdown(run_ablation(dataset, labels), dataset)
    assert "可评估运行数" in text and "UNKNOWN 运行数" in text
    assert "全部 16 条确定性规则" in text


# ---------------------------------------------------------------- R2-10 复用 out


def test_r2_10_reused_out_dir_does_not_keep_stale_metrics(tmp_path):
    out = tmp_path / "out"
    cases = ROOT / "data" / "sample" / "sample-traces.jsonl"
    # 标注文件放临时目录：测试不得往仓库里写文件
    labels = tmp_path / "labels.jsonl"
    labels.write_text("", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "tracelab", "evaluate", "--cases", str(cases),
                    "--labels", str(labels), "--out", str(out)],
                   cwd=str(ROOT), capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "tracelab", "evaluate", "--cases", str(cases),
                    "--out", str(out)], cwd=str(ROOT), capture_output=True, text=True)
    assert not (out / "metrics.json").exists(), "复用输出目录时必须清理上一批 metrics"


# ---------------------------------------------------------------- R2-13 标注校验


def test_r2_13_invalid_labels_raise_format_error(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps({"case_id": "C1", "run_assessments": [{"agent_task_success": True}]}) + "\n",
                    encoding="utf-8")
    try:
        load_reference_labels(path)
    except LabelFormatError as exc:
        assert "run_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺 run_id 必须报 LabelFormatError")


def test_r2_13_duplicate_case_id_is_rejected(tmp_path):
    path = tmp_path / "labels.jsonl"
    row = {"case_id": "C1", "run_assessments": [{
        "run_id": "R1", "agent_task_success": True, "business_invoice_created": False,
        "root_causes": [],
    }]}
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    try:
        load_reference_labels(path)
    except LabelFormatError as exc:
        assert "重复" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复 case_id 必须报错，不能静默覆盖")


# ---------------------------------------------------------------- R2-14 归档容错


def test_r2_14_archive_handles_non_object_json(tmp_path):
    from tracelab.regression import build_expectations, write_pack

    out = tmp_path / "pack"
    out.mkdir()
    (out / "regression-pack.json").write_text("[1,2,3]", encoding="utf-8")
    dataset = load_dataset(ROOT / "data" / "sample" / "sample-traces.jsonl")
    result = evaluate_dataset(dataset)
    write_pack(build_expectations(result), out, dataset, allow_overwrite=True)  # 不应抛 AttributeError
    assert list((out / "archive").iterdir()), "旧快照必须被归档而不是丢失"
