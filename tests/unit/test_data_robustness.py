"""数据健壮性回归（C-23 / C-24 / C-25 / C-26 / C-27）。

这组测试钉死的是"数据层不该撒谎"的五件事：

* generic 适配器必须和 official 做同一套收尾检查（空文件/重复主键）——C-23；
* `Dataset.read_complete` 不能在没有计数凭据时真空成立——C-24；
* 坏字节只能丢一行，不能打穿整批——C-25；
* `tool_calls` 里的非 object 元素必须记错误，不能静默消失——C-26；
* 重复 seq 是 schema 错误（首因不可判定），乱序只是 warn——C-27。

所有反例都必须满足：**批次继续 + issue 可查 + 关键计数可自证**，不许抛异常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracelab.adapters import load_dataset
from tracelab.contracts import Dataset

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


def _run(run_id: str = "R1", *, events: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
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
        "resource_usage": {},
    }
    run.update(overrides)
    return run


def _case(case_id: str = "CASE-1", *, runs: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "dataset_version": "student-1.0",
        "data_origin": "synthetic",
        "case_id": case_id,
        "scenario": "健壮性测试",
        "user_request": "请求",
        "context": {},
        "reference_result": {},
        "runs": runs if runs is not None else [_run("R1"), _run("R2")],
    }
    case.update(overrides)
    return case


def _gen_step(step_id: str, event_id: str = "S-1") -> dict[str, Any]:
    return {
        "id": event_id, "step": step_id, "state": "SUCCEEDED", "in": {}, "out": {},
        "tools": [], "error": None, "retries": 0,
    }


def _gen_traj(label: str = "base", *, run_id: str | None = None, steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "label": label,
        "run_id": run_id if run_id is not None else f"GEN-{label}",
        "agent": f"{label}-v1",
        "final_state": "SUCCEEDED",
        "invoice_created": True,
        "steps": steps if steps is not None else [
            _gen_step("RECEIVE_REQUEST", f"{label}-S-1"),
            _gen_step("SUBMIT_INVOICE", f"{label}-S-2"),
        ],
    }


def _gen_case(case_id: str = "GEN-1", *, trajectories: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "format_version": "generic-heterogeneous/1",
        "case": {"id": case_id, "scenario": "异构", "request": "请求", "context": {}, "goal": {}},
        "trajectories": trajectories if trajectories is not None else [_gen_traj("base"), _gen_traj("cand")],
    }


def _write_jsonl(tmp_path: Path, *records: Any, name: str = "traces.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return path


def _codes(ds: Dataset) -> set[str]:
    return {i.code for i in ds.issues}


# -------------------------------------------------- C-23 generic 收尾检查


def test_generic_empty_file_reports_empty_dataset(tmp_path: Path) -> None:
    """空文件不能安静地返回 0 个 Case：必须 EMPTY_DATASET + 读不全。"""
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    ds = load_dataset(path, adapter="generic")

    assert len(ds) == 0
    assert "EMPTY_DATASET" in _codes(ds)
    assert ds.stats["lines_total"] == 0 and ds.stats["cases_parsed"] == 0
    assert ds.read_complete is False


def test_generic_duplicate_case_id_is_error(tmp_path: Path) -> None:
    """official 早就有这条检查；generic 以前换个格式就漏检。"""
    ds = load_dataset(
        _write_jsonl(tmp_path, _gen_case("GEN-DUP"), _gen_case("GEN-DUP")),
        adapter="generic",
    )

    assert len(ds) == 2
    assert any(i.code == "DUPLICATE_CASE_ID" and i.severity == "error" for i in ds.issues)
    assert ds.schema_valid is False


def test_generic_duplicate_run_id_is_error(tmp_path: Path) -> None:
    same_id = [_gen_traj("base", run_id="SAME"), _gen_traj("cand", run_id="SAME")]
    ds = load_dataset(_write_jsonl(tmp_path, _gen_case("GEN-1", trajectories=same_id)), adapter="generic")

    assert any(i.code == "DUPLICATE_RUN_ID" and i.severity == "error" for i in ds.issues)
    assert ds.schema_valid is False


def test_official_shares_the_same_finalize_checks(tmp_path: Path) -> None:
    """两个适配器的空文件/重复 Case 行为必须一致（同一收尾逻辑）。"""
    path = tmp_path / "dup.jsonl"
    path.write_text(
        "".join(json.dumps(_case("CASE-DUP"), ensure_ascii=False) + "\n" for _ in range(2)),
        encoding="utf-8",
    )

    ds = load_dataset(path)
    assert "DUPLICATE_CASE_ID" in _codes(ds)

    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    ds_empty = load_dataset(empty)
    assert "EMPTY_DATASET" in _codes(ds_empty)
    assert ds_empty.read_complete is False


# ------------------------------------------------- C-24 read_complete 不自证


def test_read_complete_requires_counting_evidence() -> None:
    """没有 stats/关键计数时**不得**宣称读全（旧实现默认 {} → 真空 True）。"""
    assert Dataset(cases=[]).read_complete is False
    assert Dataset(cases=[], stats={"lines_total": 1}).read_complete is False
    assert Dataset(cases=[], stats={"lines_total": 1, "cases_parsed": 1}).read_complete is False
    assert Dataset(
        cases=[],
        stats={"lines_total": 1, "cases_parsed": 1, "skipped": 0},
    ).read_complete is False
    assert Dataset(
        cases=[],
        stats={"lines_total": 1, "cases_parsed": 1, "skipped": 0, "truncated": 0},
    ).read_complete is True


def test_adapter_datasets_still_prove_read_complete(tmp_path: Path) -> None:
    """正向：合法数据必须仍然能证明"读全"，不能把正常数据也判成不完整。"""
    ds = load_dataset(_write_jsonl(tmp_path, _case("CASE-OK")))
    assert ds.read_complete is True
    assert ds.stats["lines_total"] == 1
    assert ds.stats["cases_parsed"] == 1


# -------------------------------------------------- C-25 坏字节只丢一行


@pytest.mark.parametrize("adapter", ["official", "generic"])
def test_bad_bytes_drop_only_the_bad_line(tmp_path: Path, adapter: str) -> None:
    """坏 UTF-8 字节以前会让 read_text() 直接抛异常打穿整批；现在只丢该行。"""
    good_a = _case("CASE-A") if adapter == "official" else _gen_case("CASE-A")
    good_b = _case("CASE-B") if adapter == "official" else _gen_case("CASE-B")
    payload = (
        json.dumps(good_a, ensure_ascii=False).encode("utf-8") + b"\n"
        + b"\xff\xfe\x00 this line is not utf-8\n"
        + json.dumps(good_b, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    path = tmp_path / "bad_bytes.jsonl"
    path.write_bytes(payload)

    ds = load_dataset(path, adapter=adapter)

    assert [c.case_id for c in ds.cases] == ["CASE-A", "CASE-B"], "单行坏字节不得影响其它行"
    assert "BAD_ENCODING" in _codes(ds)
    assert any(i.category == "read" and i.severity == "error" for i in ds.issues)
    assert ds.stats["lines_total"] == 3
    assert ds.stats["skipped"] == 1
    assert ds.read_complete is False


@pytest.mark.parametrize("adapter", ["official", "generic"])
def test_json_valueerror_and_recursion_error_are_isolated(
    tmp_path: Path, adapter: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`json.loads` 的 ValueError / RecursionError 都不能中止整批（只 catch JSONDecodeError 是不够的）。"""
    import tracelab.adapters_common as adapters_common

    real_loads = json.loads

    def fake_loads(text: str, *args: Any, **kwargs: Any) -> Any:
        if "TRIGGER_RECURSION" in text:
            raise RecursionError("maximum recursion depth exceeded")
        if "TRIGGER_VALUEERROR" in text:
            raise ValueError("Unterminated string")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(adapters_common.json, "loads", fake_loads)

    good_a = _case("CASE-A") if adapter == "official" else _gen_case("CASE-A")
    good_b = _case("CASE-B") if adapter == "official" else _gen_case("CASE-B")
    path = tmp_path / "bad_json.jsonl"
    path.write_text(
        "\n".join([
            json.dumps(good_a, ensure_ascii=False),
            '"TRIGGER_RECURSION"',
            '"TRIGGER_VALUEERROR"',
            json.dumps(good_b, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    ds = load_dataset(path, adapter=adapter)

    assert [c.case_id for c in ds.cases] == ["CASE-A", "CASE-B"]
    bad_json = [i for i in ds.issues if i.code == "BAD_JSON"]
    assert len(bad_json) == 2
    assert all(i.severity == "error" and i.category == "read" for i in bad_json)
    assert ds.stats["skipped"] == 2


# ------------------------------------------- C-26 tool_calls 非 object 元素


def test_official_tool_call_non_object_is_reported_and_counted(tmp_path: Path) -> None:
    """非 object 的 tool_calls 元素不能被 `if isinstance(t, dict)` 静默过滤掉。"""
    event = _event(
        "E-1",
        tool_calls=[
            {"name": "invoice.submit", "arguments": {"amounts": {"inclusive_amount": 1.0}}, "result": {}},
            "not-an-object",
            42,
        ],
    )
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[_run("R1", events=[event]), _run("R2")])))

    bad = [i for i in ds.issues if i.code == "BAD_TOOL_CALL_TYPE"]
    assert len(bad) == 2
    assert all(i.severity == "error" and i.category == "schema" for i in bad)
    assert "tool_calls[1]" in bad[0].detail and "共 3 个元素" in bad[0].detail
    assert ds.stats["bad_tool_calls"] == 2, "丢弃数量必须保留计数，可审计"

    parsed = ds.cases[0].runs[0].events[0]
    assert [t.name for t in parsed.tool_calls] == ["invoice.submit"], "合法工具调用不能被连带丢弃"
    assert len(parsed.raw["tool_calls"]) == 3, "raw 必须原样保留（契约层不删原始数据）"
    assert ds.schema_valid is False


# ------------------------------------------------ C-27 重复/乱序 seq 分级


def test_duplicate_seq_is_error_not_warn(tmp_path: Path) -> None:
    """重复 seq 让"首因/因果顺序"不可判定，必须升为 schema error。"""
    events = [_event("E-1", 1), _event("E-2", 1)]
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[_run("R1", events=events), _run("R2")])))

    dup = [i for i in ds.issues if i.code == "SEQ_DUPLICATE"]
    assert dup and all(i.severity == "error" for i in dup)
    assert ds.schema_valid is False


def test_unsorted_seq_stays_warn(tmp_path: Path) -> None:
    """乱序只是提示：规则按 step_id 索引，顺序不伪造业务事实。"""
    events = [_event("E-2", 2), _event("E-1", 1)]
    ds = load_dataset(_write_jsonl(tmp_path, _case(runs=[_run("R1", events=events), _run("R2")])))

    unsorted_issues = [i for i in ds.issues if i.code == "SEQ_NOT_SORTED"]
    assert unsorted_issues and all(i.severity == "warn" for i in unsorted_issues)
    assert not [i for i in ds.issues if i.code == "SEQ_DUPLICATE"]
    assert ds.schema_valid is True
