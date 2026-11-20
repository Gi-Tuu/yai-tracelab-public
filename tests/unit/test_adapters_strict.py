"""适配器严格类型验证——把"非法输入被静默转成正常业务状态"钉死在反例上。

覆盖两类缺陷（对应 `tracelab/adapters.py`）：

* official：`bool("false")`/`bool("0")` 会得到 True；`run_id`/`agent_version`/`event_id`
  被 `str(...)` 强转；`resource_usage` 非 object 时静默塞 `{}`；`_safe_int` 把 `True`
  当成 1。
* generic：顶层不是 object、`case`/`context`/`trajectories`/`steps`/`tools` 类型不符、
  `retries` 非法时，单条坏记录会打穿整批或静默消失。

所有反例都必须满足：**记 schema 级 IntegrityIssue（severity="error", category="schema"）
+ 落安全默认值 + 批次继续**，不允许抛异常打穿 `load()`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracelab.adapters import load_dataset
from tracelab.contracts import Dataset

BAD_BOOL_VALUES = ["false", "0", 1, None]
BAD_INT_VALUES = [True, "3", 1.5, None]


# ------------------------------------------------------------------ 构造工具


def _event(event_id: str = "E-1", seq: Any = 1, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id,
        "seq": seq,
        "step_id": "COLLECT_INVOICE_DATA",
        "module": "INVOICE",
        "action": "COLLECT_INVOICE_DATA",
        "step_status": "SUCCEEDED",
        "result_code": "E100",
        "error_code": None,
        "error_message": None,
        "retry_count": 0,
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
        "started_at": "2026-01-01T00:00:00+08:00",
        "finished_at": "2026-01-01T00:00:01+08:00",
        "duration_ms": 1000,
    }
    event.update(overrides)
    return event


def _run(
    run_id: str = "R1",
    *,
    events: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "run_id": run_id,
        "agent_version": "baseline-v1.0",
        "task_id": f"T-{run_id}",
        "trace_id": f"TR-{run_id}",
        "events": events if events is not None else [_event(f"{run_id}-E1")],
        "final_state": "SUCCEEDED",
        "snapshot_state": "SUCCEEDED",
        "workflow_terminal": True,
        "invoice_created": True,
        "resource_usage": {"total_latency_ms": 1000},
    }
    run.update(overrides)
    return run


def _case(
    case_id: str = "CASE-1",
    *,
    runs: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "dataset_version": "student-1.0",
        "data_origin": "synthetic",
        "case_id": case_id,
        "scenario": "严格类型测试",
        "user_request": "测试请求",
        "context": {},
        "reference_result": {},
        "runs": runs if runs is not None else [_run("R1"), _run("R2")],
    }
    case.update(overrides)
    return case


def _gen_step(step_id: str = "RECEIVE_REQUEST", **overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": f"{step_id}-1",
        "step": step_id,
        "state": "SUCCEEDED",
        "code": "E100",
        "in": {"k": 1},
        "out": {"k": 2},
        "tools": [],
        "error": None,
        "retries": 0,
    }
    step.update(overrides)
    return step


def _gen_traj(
    label: str = "base",
    *,
    steps: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    traj: dict[str, Any] = {
        "label": label,
        "run_id": f"GEN-{label}",
        "agent": f"{label}-v1",
        "final_state": "SUCCEEDED",
        "invoice_created": True,
        "steps": steps
        if steps is not None
        else [_gen_step("RECEIVE_REQUEST"), _gen_step("CONFIRM_INVOICE")],
    }
    traj.update(overrides)
    return traj


def _gen_case(
    case_id: str = "GEN-1",
    *,
    trajectories: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "format_version": "generic-heterogeneous/1",
        "case": {
            "id": case_id,
            "scenario": "异构场景",
            "request": "请求",
            "context": {"seller_id": "SELLER-1"},
            "goal": {"amount_basis": "tax_exclusive"},
        },
        "trajectories": trajectories
        if trajectories is not None
        else [_gen_traj("base"), _gen_traj("cand")],
    }
    record.update(overrides)
    return record


def _write_jsonl(tmp_path: Path, *records: Any, name: str = "traces.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return path


def _schema_errors(ds: Dataset) -> list[Any]:
    return [i for i in ds.issues if i.severity == "error" and i.category == "schema"]


def _codes(ds: Dataset) -> set[str]:
    return {i.code for i in ds.issues}


# ------------------------------------------------- official：业务布尔字段


@pytest.mark.parametrize("field", ["workflow_terminal", "invoice_created"])
@pytest.mark.parametrize("bad", BAD_BOOL_VALUES)
def test_business_bool_only_accepts_real_bool(tmp_path: Path, field: str, bad: Any) -> None:
    """`"false"`/`"0"`/`1`/`null` 都不得被 bool() 伪装成 True，必须记 schema 错误并落 False。"""
    bad_run = _run("R1", **{field: bad})
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[bad_run, _run("R2")])))

    assert len(ds) == 1
    assert ds.cases[0].runs[0].run_id == "R1"
    assert getattr(ds.cases[0].runs[0], field) is False, "非法布尔必须落安全默认值 False"
    errors = _schema_errors(ds)
    assert any(i.code == "BAD_FIELD_TYPE" and field in i.detail for i in errors), (
        f"{field}={bad!r} 必须记 schema 级错误"
    )
    # 同一文件里的另一条运行不受影响（批次继续）
    assert getattr(ds.cases[0].runs[1], field) is True
    assert ds.stats["skipped"] == 0


@pytest.mark.parametrize("field", ["workflow_terminal", "invoice_created"])
def test_missing_business_bool_is_schema_error(tmp_path: Path, field: str) -> None:
    """缺失的必需业务布尔同样要记 schema 错误，而不是静默 False。"""
    bad_run = _run("R1")
    del bad_run[field]
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[bad_run, _run("R2")])))

    assert any(i.code == "MISSING_FIELD" and field in i.detail for i in _schema_errors(ds))
    assert getattr(ds.cases[0].runs[0], field) is False


# --------------------------------------- official：run_id / agent_version / event_id


@pytest.mark.parametrize("field", ["run_id", "agent_version"])
def test_missing_run_identity_is_schema_error(tmp_path: Path, field: str) -> None:
    bad_run = _run("R1")
    del bad_run[field]
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[bad_run, _run("R2")])))

    assert any(i.code == "MISSING_FIELD" and field in i.detail for i in _schema_errors(ds))
    assert getattr(ds.cases[0].runs[0], field) == ""


def test_non_string_run_id_is_not_stringified(tmp_path: Path) -> None:
    """`str({"x": 1})` 会伪造出一个看似正常的 run_id，必须记错误并落空串。"""
    bad_run = _run("R1")
    bad_run["run_id"] = {"x": 1}
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[bad_run, _run("R2")])))

    assert any(i.code == "BAD_FIELD_TYPE" and "run_id" in i.detail for i in _schema_errors(ds))
    assert ds.cases[0].runs[0].run_id == ""
    assert ds.cases[0].runs[1].run_id == "R2", "坏记录不影响同批其它运行"


def test_missing_event_id_is_schema_error(tmp_path: Path) -> None:
    bad_event = _event("E-1")
    del bad_event["event_id"]
    ds = load_dataset(
        _write_jsonl(tmp_path, _case(runs=[_run("R1", events=[bad_event]), _run("R2")]))
    )

    codes = _codes(ds)
    assert {"MISSING_EVENT_FIELDS", "MISSING_EVENT_ID"} & codes
    assert _schema_errors(ds)
    assert ds.cases[0].runs[0].events[0].event_id == ""


def test_event_id_must_be_string(tmp_path: Path) -> None:
    bad_event = _event("E-1")
    bad_event["event_id"] = {"a": 1}
    ds = load_dataset(
        _write_jsonl(tmp_path, _case(runs=[_run("R1", events=[bad_event]), _run("R2")]))
    )

    assert any(i.code == "BAD_FIELD_TYPE" and "event_id" in i.detail for i in _schema_errors(ds))
    assert ds.cases[0].runs[0].events[0].event_id == ""
    # 另一条运行的事件仍然正常解析
    assert ds.cases[0].runs[1].events[0].event_id == "R2-E1"


# ------------------------------------------------- official：resource_usage


def test_resource_usage_non_object_is_reported(tmp_path: Path) -> None:
    """非 object 的 resource_usage 不能被静默塞成 {}，要记 schema 错误。"""
    bad_run = _run("R1", resource_usage=["not-an-object"])
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[bad_run, _run("R2")])))

    assert any(i.code == "BAD_RESOURCE_USAGE" for i in _schema_errors(ds))
    assert dict(ds.cases[0].runs[0].resource_usage) == {}
    assert dict(ds.cases[0].runs[1].resource_usage) == {"total_latency_ms": 1000}


# --------------------------------------- official：数值字段（seq/retry/duration）


@pytest.mark.parametrize("field", ["seq", "retry_count", "duration_ms"])
@pytest.mark.parametrize("bad", BAD_INT_VALUES)
def test_numeric_fields_reject_bool_and_non_int(tmp_path: Path, field: str, bad: Any) -> None:
    """bool 是 int 子类（True 会被当成 1）；字符串/浮点/null 也不是 integer。"""
    bad_event = _event("E-1", **{field: bad})
    ds = load_dataset(
        _write_jsonl(tmp_path, _case(runs=[_run("R1", events=[bad_event]), _run("R2")]))
    )

    event = ds.cases[0].runs[0].events[0]
    assert getattr(event, field) == 0, "非法数值必须落默认值"
    assert any(i.code == "BAD_FIELD_TYPE" and field in i.detail for i in _schema_errors(ds))
    # 同批第二条运行的事件照常解析
    assert ds.cases[0].runs[1].events[0].seq == 1


# ------------------------------------------------------- generic：结构类型


@pytest.mark.parametrize("bad_record", [[{"a": 1}], "just-a-string"])
def test_generic_non_object_record_is_skipped_not_crash(tmp_path: Path, bad_record: Any) -> None:
    """一行 JSON 是数组/字符串时，不能 AttributeError 打穿整批，要记错误 + 跳过 + 继续。"""
    ds = load_dataset(
        _write_jsonl(tmp_path, _gen_case("GEN-A"), bad_record, _gen_case("GEN-B")),
        adapter="generic",
    )

    assert [c.case_id for c in ds.cases] == ["GEN-A", "GEN-B"], "一条坏记录不影响其它记录"
    assert ds.stats["lines_total"] == 3
    assert ds.stats["cases_parsed"] == 2
    assert ds.stats["skipped"] == 1, "坏记录不能静默消失"
    assert "NOT_OBJECT" in _codes(ds)
    assert any(i.code == "PARTIAL_PARSE" for i in ds.errors)


def test_generic_case_head_must_be_object(tmp_path: Path) -> None:
    bad = _gen_case("GEN-BAD")
    bad["case"] = ["not-an-object"]
    ds = load_dataset(_write_jsonl(tmp_path, _gen_case("GEN-A"), bad), adapter="generic")

    assert [c.case_id for c in ds.cases] == ["GEN-A"]
    assert ds.stats["skipped"] == 1
    assert "BAD_CASE_HEAD" in _codes(ds)
    assert _schema_errors(ds)


def test_generic_trajectories_must_be_array(tmp_path: Path) -> None:
    bad = _gen_case("GEN-BAD")
    bad["trajectories"] = {"base": "oops"}
    ds = load_dataset(_write_jsonl(tmp_path, _gen_case("GEN-A"), bad), adapter="generic")

    assert [c.case_id for c in ds.cases] == ["GEN-A"]
    assert ds.stats["skipped"] == 1
    assert "BAD_TRAJECTORIES" in _codes(ds)


def test_generic_context_non_object_is_reported(tmp_path: Path) -> None:
    """context 是 list 时不能 dict(list) 崩掉，也不能静默吞掉：记错误 + 落 {}。"""
    bad = _gen_case("GEN-BAD")
    bad["case"]["context"] = ["seller_id"]
    ds = load_dataset(_write_jsonl(tmp_path, bad), adapter="generic")

    assert len(ds) == 1
    assert ds.stats["skipped"] == 0
    assert "BAD_CONTEXT" in _codes(ds)
    assert _schema_errors(ds)
    assert dict(ds.cases[0].context) == {}


def test_generic_steps_non_array_is_reported(tmp_path: Path) -> None:
    """steps 是 dict 时 for..in 会静默跳过全部事件，必须显式记 schema 错误。"""
    bad = _gen_case("GEN-BAD")
    bad["trajectories"][0]["steps"] = {"a": 1}
    ds = load_dataset(_write_jsonl(tmp_path, bad), adapter="generic")

    assert len(ds) == 1
    assert "BAD_STEPS" in _codes(ds)
    assert _schema_errors(ds)
    assert ds.cases[0].runs[0].events == ()
    # 第二条轨迹（合法）仍被解析，单条坏轨迹不影响同记录其它轨迹
    assert len(ds.cases[0].runs[1].events) == 2


def test_generic_tools_non_array_is_reported(tmp_path: Path) -> None:
    bad = _gen_case("GEN-BAD")
    bad["trajectories"][0]["steps"][0]["tools"] = {"tool": "x"}
    ds = load_dataset(_write_jsonl(tmp_path, bad), adapter="generic")

    assert len(ds) == 1
    assert "BAD_TOOLS" in _codes(ds)
    assert ds.cases[0].runs[0].events[0].tool_calls == ()
    assert ds.cases[0].runs[0].events[1].event_id == "CONFIRM_INVOICE-1"


# --------------------------------------------------- generic：step 级字段类型


def test_generic_bad_retries_does_not_crash_batch(tmp_path: Path) -> None:
    """`int("abc")` 曾抛 ValueError 打穿整批；现在要记错误、落 0、继续解析后续 step。"""
    bad = _gen_case("GEN-BAD")
    bad["trajectories"][0]["steps"][0]["retries"] = "abc"
    ds = load_dataset(_write_jsonl(tmp_path, bad), adapter="generic")

    assert len(ds) == 1
    assert any(i.code == "BAD_FIELD_TYPE" and "retries" in i.detail for i in _schema_errors(ds))
    run = ds.cases[0].runs[0]
    assert run.events[0].retry_count == 0
    assert len(run.events) == 2, "同一条 trajectory 的后续 step 必须继续被解析"
    assert run.events[1].event_id == "CONFIRM_INVOICE-1"


def test_generic_observed_in_out_non_object_is_reported(tmp_path: Path) -> None:
    bad = _gen_case("GEN-BAD")
    step = bad["trajectories"][0]["steps"][0]
    step["in"] = ["not-an-object"]
    step["out"] = ["not-an-object"]
    ds = load_dataset(_write_jsonl(tmp_path, bad), adapter="generic")

    assert len(ds) == 1
    codes = _codes(ds)
    assert {"BAD_OBSERVED_INPUT", "BAD_OBSERVED_OUTPUT"} <= codes
    event = ds.cases[0].runs[0].events[0]
    assert dict(event.observed_input) == {}
    assert dict(event.observed_output) == {}
    # 同记录的另一条 trajectory 不受影响
    assert dict(ds.cases[0].runs[1].events[0].observed_input) == {"k": 1}


def test_generic_invoice_created_rejects_string(tmp_path: Path) -> None:
    """generic 适配器同样不能把 `"false"` 当成 True 的业务状态。"""
    bad = _gen_case("GEN-BAD")
    bad["trajectories"][0]["invoice_created"] = "false"
    ds = load_dataset(_write_jsonl(tmp_path, bad), adapter="generic")

    run = ds.cases[0].runs[0]
    assert run.invoice_created is False
    assert any(i.code == "BAD_FIELD_TYPE" and "invoice_created" in i.detail for i in _schema_errors(ds))
    assert ds.cases[0].runs[1].invoice_created is True
