"""回归快照三态化（公共测试：全部使用合成数据，不依赖官方资料包）。

核心不变量（对应 G5 三态判定 PASS/FAIL/UNKNOWN）：

* 只有 PASS → FAIL 才是回归；FAIL → PASS 才是改善；
* 任何涉及 UNKNOWN 的变化（证据不足 / 恢复 / 丢失）既不是回归也不是改善，
  **也不能算作"复测通过"**；
* 旧格式 ``/1`` 的 ``agent_task_success=false`` 默认被拒绝，保守模式下也只记为
  UNKNOWN，**绝不**被解释成 FAIL；
* 历史快照默认不被覆盖，显式覆盖时旧文件归档不丢。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab import cli
from tracelab.contracts import Case, Dataset, Verdict
from tracelab.evaluation import evaluate_dataset
from tracelab.regression import (
    LEGACY_PACK_SCHEMAS,
    PACK_SCHEMA,
    Expectation,
    LegacyPackError,
    PackExistsError,
    PackFormatError,
    build_expectations,
    load_pack,
    render_verify,
    verify_pack,
    write_pack,
)

PASS, FAIL, UNKNOWN = Verdict.PASS, Verdict.FAIL, Verdict.UNKNOWN
LEGACY_SCHEMA = next(iter(LEGACY_PACK_SCHEMAS))


# ------------------------------------------------------------------ 构造工具

def _case_for(verdict: Verdict, case_id: str = "C1") -> Case:
    """构造一条能稳定产出指定三态的合成运行。"""
    prefix = f"{case_id}A"
    if verdict is PASS:
        run = make_run(f"{case_id}-A", basic_flow(prefix), final_state="SUCCEEDED", invoice_created=True)
        return make_case(case_id, context=context(), reference_result=reference(), runs=[run])
    if verdict is FAIL:
        # 金额口径用错：可证实的规则违规 → FAIL
        run = make_run(
            f"{case_id}-A", basic_flow(prefix, amount_basis="tax_inclusive"),
            final_state="SUCCEEDED", invoice_created=True,
        )
        return make_case(case_id, context=context(), reference_result=reference(), runs=[run])
    # 终态偏离且确定性规则定位不到根因 → UNKNOWN（不是 Agent 失败）
    run = make_run(
        f"{case_id}-A", basic_flow(prefix, final_state="WAITING_USER", invoice_created=False),
        final_state="WAITING_USER", invoice_created=False,
    )
    return make_case(
        case_id, context=context(),
        reference_result=reference(acceptable_snapshot_state="TIMEOUT_TASK", invoice_should_exist=False),
        runs=[run],
    )


def _dataset(*verdicts: Verdict) -> Dataset:
    return Dataset(cases=[_case_for(v, f"C{i + 1}") for i, v in enumerate(verdicts or (PASS,))])


def _snapshot(dataset: Dataset) -> list[Expectation]:
    return build_expectations(evaluate_dataset(dataset))


def _pack(expectations: list[Expectation], *, schema: str = PACK_SCHEMA,
          created_from: dict[str, str] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"schema": schema, "expectations": [e.to_dict() for e in expectations]}
    if created_from is not None:
        payload["created_from"] = created_from
    return payload


def _legacy_entry(*, success: bool, case_id: str = "C1", run_id: str = "C1-A",
                  labels: list[str] | None = None, first: str | None = None,
                  quality: float = 70.0) -> dict[str, object]:
    return {
        "case_id": case_id, "run_id": run_id, "agent_version": "baseline-v1.0",
        "agent_task_success": success, "labels": labels or [],
        "first_fault_event_id": first, "quality_score": quality,
    }


def _legacy_pack(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"schema": LEGACY_SCHEMA, "created_from": {"dataset_sha256": ""}, "expectations": entries}


def _official_line(case: Case) -> dict[str, object]:
    """合成 Case → 官方 JSONL 一行（供 CLI 端到端测试用）。"""
    runs = [{**r.raw, "events": [dict(e.raw) for e in r.events]} for r in case.runs]
    return {**case.raw, "runs": runs}


def _write_official(path: Path, *verdicts: Verdict) -> Path:
    lines = [_official_line(_case_for(v, f"C{i + 1}")) for i, v in enumerate(verdicts)]
    path.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8"
    )
    return path


# ------------------------------------------------------------- 七种三态组合

_MATRIX = [
    (PASS, FAIL, "regressed", "regression"),
    (FAIL, PASS, "improved", "improvement"),
    (PASS, UNKNOWN, "insufficient_evidence", "pass_to_unknown"),
    (FAIL, UNKNOWN, "insufficient_evidence", "fail_to_unknown"),
    (UNKNOWN, PASS, "evidence_recovered", "unknown_to_pass"),
    (UNKNOWN, FAIL, "evidence_lost", "unknown_to_fail"),
    (UNKNOWN, UNKNOWN, "insufficient_evidence", "unknown_to_unknown"),
]


@pytest.mark.parametrize(("expected", "actual", "bucket", "classification"), _MATRIX)
def test_three_state_matrix(expected, actual, bucket, classification):
    """七种组合必须落进各自的桶，不能互相串。"""
    dataset = _dataset(actual)
    expectations = [replace(e, verdict=expected) for e in _snapshot(dataset)]
    report = verify_pack(_pack(expectations), dataset)

    rows = getattr(report, bucket)
    assert len(rows) == 1
    row = rows[0]
    assert row["classification"] == classification
    assert row["expected_verdict"] == expected.value
    assert row["actual_verdict"] == actual.value
    for other in ("regressed", "improved", "insufficient_evidence",
                  "evidence_recovered", "evidence_lost"):
        if other != bucket:
            assert getattr(report, other) == [], f"{expected}→{actual} 不应进 {other}"
    assert report.matches == [], "三态变化不能被算成一致"


@pytest.mark.parametrize(("expected", "actual", "problem", "code"), [
    (PASS, PASS, False, 0),
    (FAIL, PASS, False, 0),          # 改善不是问题，但仍需人工确认
    (PASS, FAIL, True, 1),           # 回归
    (PASS, UNKNOWN, True, 1),        # 证据不足：不是回归，但也不能算通过
    (FAIL, UNKNOWN, True, 1),        # 证据不足：不是改善，也不能算通过
    (UNKNOWN, PASS, True, 1),        # 证据恢复：不得认定为 Agent 改善
    (UNKNOWN, FAIL, True, 1),        # 证据丢失：不得认定为 Agent 退化
    (UNKNOWN, UNKNOWN, True, 1),     # 无效结论
])
def test_has_problem_and_exit_code(expected, actual, problem, code):
    dataset = _dataset(actual)
    expectations = [replace(e, verdict=expected) for e in _snapshot(dataset)]
    report = verify_pack(_pack(expectations), dataset)
    assert report.has_problem is problem
    assert report.is_clean is (not problem)
    assert report.exit_code == code
    assert report.to_dict()["summary"]["exit_code"] == code


def test_compatibility_error_exits_4():
    dataset = _dataset(PASS)
    pack = _pack(_snapshot(dataset), created_from={"dataset_sha256": "deadbeef" * 8})
    report = verify_pack(pack, dataset)
    assert report.compatibility_errors
    assert report.has_problem is True
    # R3-A-08：2 已被"输入/路径错误"占用；复测前提不成立改用 4，避免两种语义混淆。
    assert report.exit_code == 4, "环境/数据不兼容必须是 4，不能和'有回归'或'输入错'混码"


def test_unknown_is_not_counted_as_pass_and_not_label_alarm():
    """UNKNOWN → UNKNOWN：不计入一致、不按标签差异报警，但也不是通过。"""
    dataset = _dataset(UNKNOWN)
    expectations = [replace(e, labels=("SOMETHING_ELSE",)) for e in _snapshot(dataset)]
    report = verify_pack(_pack(expectations), dataset)
    assert report.matches == []
    assert report.changed == [], "结论是 UNKNOWN 时不应按标签差异报警"
    assert len(report.insufficient_evidence) == 1
    assert report.has_problem is True


def test_same_verdict_label_drift_is_changed_not_problem():
    dataset = _dataset(FAIL)
    expectations = [replace(e, labels=("OTHER_LABEL",), first_fault_event_id="X-99")
                    for e in _snapshot(dataset)]
    report = verify_pack(_pack(expectations), dataset)
    assert len(report.changed) == 1
    assert report.has_problem is False
    assert report.exit_code == 0


def test_identical_snapshot_is_clean():
    dataset = _dataset(PASS, FAIL)
    report = verify_pack(_pack(_snapshot(dataset)), dataset)
    assert len(report.matches) == 2
    assert report.has_problem is False
    assert report.exit_code == 0


# ------------------------------------------------------------ 渲染口径

def test_render_verify_never_calls_unknown_a_pass():
    dataset = _dataset(PASS)
    expectations = [replace(e, verdict=UNKNOWN) for e in _snapshot(dataset)]
    report = verify_pack(_pack(expectations), dataset)
    text = render_verify(report, {})
    assert "复测通过" not in text
    assert "三态变化明细" in text
    assert "证据恢复" in text
    assert "UNKNOWN" in text and "PASS" in text


def test_render_verify_clean_says_pass():
    dataset = _dataset(PASS)
    report = verify_pack(_pack(_snapshot(dataset)), dataset)
    text = render_verify(report, {})
    assert "复测通过" in text


def test_render_verify_marks_regression_separately_from_unknown():
    dataset = _dataset(PASS, UNKNOWN)
    snapshot = _snapshot(dataset)
    flipped = [replace(snapshot[0], verdict=FAIL), snapshot[1]]
    report = verify_pack(_pack(flipped), dataset)
    text = render_verify(report, {})
    assert "回归" in text and "证据不足" in text
    assert "复测通过" not in text


# ------------------------------------------------------------ 旧格式 (/1)

def test_legacy_pack_is_rejected_by_default(tmp_path):
    dataset = _dataset(FAIL)
    legacy = _legacy_pack([_legacy_entry(success=False)])

    with pytest.raises(LegacyPackError, match="三态"):
        verify_pack(legacy, dataset)

    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(LegacyPackError, match="--allow-legacy"):
        load_pack(path)


def test_legacy_false_is_unknown_never_fail(tmp_path):
    """保守模式：旧 false 记为 UNKNOWN，绝不产生"回归"。"""
    dataset = _dataset(FAIL)                      # 实际是 FAIL
    report = verify_pack(_legacy_pack([_legacy_entry(success=False)]), dataset, allow_legacy=True)

    assert report.legacy_pack is True
    assert report.legacy_downgraded == ["C1-A"]
    assert report.regressed == [], "旧 false 不能被当成 FAIL 再报回归"
    assert len(report.evidence_lost) == 1
    assert report.evidence_lost[0]["classification"] == "unknown_to_fail"
    assert report.has_problem is True
    assert report.exit_code == 1
    assert "旧快照降级读取" in render_verify(report, {})
    assert report.compatibility and report.compatibility[-1].field == "schema"


def test_legacy_true_vs_actual_unknown_is_insufficient_not_regression():
    dataset = _dataset(UNKNOWN)                   # 当时 true，现在无法判断
    report = verify_pack(_legacy_pack([_legacy_entry(success=True)]), dataset, allow_legacy=True)
    assert report.regressed == []
    assert report.insufficient_evidence[0]["classification"] == "pass_to_unknown"


def test_legacy_false_vs_actual_unknown_is_invalid_conclusion():
    dataset = _dataset(UNKNOWN)
    report = verify_pack(_legacy_pack([_legacy_entry(success=False)]), dataset, allow_legacy=True)
    assert report.regressed == []
    assert report.insufficient_evidence[0]["classification"] == "unknown_to_unknown"
    assert report.has_problem is True


def test_from_dict_refuses_to_guess_three_state_from_boolean():
    with pytest.raises(PackFormatError, match="agent_task_success"):
        Expectation.from_dict({"case_id": "C1", "run_id": "C1-A", "agent_task_success": True})


# ---------------------------------------------------- 新 schema 与历史保护

def test_new_schema_roundtrip(tmp_path):
    dataset = _dataset(FAIL, PASS)
    expectations = _snapshot(dataset)
    path = write_pack(expectations, tmp_path / "pack", dataset)
    payload = load_pack(path)

    assert payload["schema"] == PACK_SCHEMA
    raw = payload["expectations"][0]
    assert raw["verdict"] in {"PASS", "FAIL", "UNKNOWN"}
    assert "agent_task_success" not in raw, "新格式只存三态，不存会被误解的布尔值"
    assert [Expectation.from_dict(x) for x in payload["expectations"]] == expectations


def test_unknown_schema_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema": "unknown/9", "expectations": []}), encoding="utf-8")
    with pytest.raises(PackFormatError, match="未知的回归包格式"):
        load_pack(path)


def test_write_pack_refuses_to_overwrite_history(tmp_path):
    dataset = _dataset(FAIL)
    expectations = _snapshot(dataset)
    path = write_pack(expectations, tmp_path, dataset)
    before = path.read_bytes()
    md_before = (tmp_path / "regression-pack.md").read_bytes()

    with pytest.raises(PackExistsError, match="--force"):
        write_pack(expectations, tmp_path, dataset)

    assert path.read_bytes() == before, "拒绝覆盖时不得写坏已有快照"
    assert (tmp_path / "regression-pack.md").read_bytes() == md_before
    assert not (tmp_path / "archive").exists(), "没写新快照就不该产生归档"


def test_force_overwrite_archives_legacy_snapshot(tmp_path):
    legacy = _legacy_pack([_legacy_entry(success=False)])
    (tmp_path / "regression-pack.json").write_text(json.dumps(legacy), encoding="utf-8")
    (tmp_path / "regression-pack.md").write_text("旧快照", encoding="utf-8")

    dataset = _dataset(FAIL)
    path = write_pack(_snapshot(dataset), tmp_path, dataset, allow_overwrite=True)

    archived = tmp_path / "archive" / "regression-pack.v1.json"
    assert archived.exists(), "旧快照必须原样归档，不能删除"
    assert json.loads(archived.read_text(encoding="utf-8"))["schema"] == LEGACY_SCHEMA
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == PACK_SCHEMA


# --------------------------------------------------------------- CLI 退出码

def test_cli_exit_codes_follow_three_state(tmp_path, capsys):
    clean = _write_official(tmp_path / "clean.jsonl", PASS, FAIL)
    out = tmp_path / "pack"
    assert cli.main(["regression", "build", "--cases", str(clean), "--out", str(out)]) == 0
    assert (out / "regression-pack.json").exists()

    code = cli.main(["regression", "verify", "--pack", str(out / "regression-pack.json"),
                     "--cases", str(clean), "--out", str(tmp_path / "v1")])
    assert code == 0
    assert "复测：一致 2" in capsys.readouterr().out
    assert "复测通过" in (tmp_path / "v1" / "regression-verify.md").read_text(encoding="utf-8")

    # 数据本身只有 UNKNOWN：UNKNOWN→UNKNOWN 是"无效结论"，不能算通过 → 非 0
    unknown = _write_official(tmp_path / "unknown.jsonl", UNKNOWN)
    out2 = tmp_path / "pack-unknown"
    assert cli.main(["regression", "build", "--cases", str(unknown), "--out", str(out2)]) == 0
    code = cli.main(["regression", "verify", "--pack", str(out2 / "regression-pack.json"),
                     "--cases", str(unknown), "--out", str(tmp_path / "v2")])
    captured = capsys.readouterr()
    assert code == 1
    assert "复测未通过" in captured.err
    assert "复测通过" not in (tmp_path / "v2" / "regression-verify.md").read_text(encoding="utf-8")


def test_cli_unknown_change_without_regression_is_nonzero(tmp_path, capsys):
    """存在 UNKNOWN 变化但没有回归：也必须非 0，因为不能宣称通过。"""
    path_ok = _write_official(tmp_path / "pass.jsonl", PASS)
    out = tmp_path / "pack"
    assert cli.main(["regression", "build", "--cases", str(path_ok), "--out", str(out)]) == 0

    path_unknown = _write_official(tmp_path / "unknown.jsonl", UNKNOWN)   # 同一个 C1-A
    code = cli.main(["regression", "verify", "--pack", str(out / "regression-pack.json"),
                     "--cases", str(path_unknown), "--out", str(tmp_path / "v"),
                     "--allow-data-evolution"])
    captured = capsys.readouterr()
    assert code == 1
    assert "回归 0" in captured.out
    assert "证据不足 1" in captured.out
    assert "复测未通过" in captured.err
    md = (tmp_path / "v" / "regression-verify.md").read_text(encoding="utf-8")
    assert "复测通过" not in md
    assert "pass_to_unknown" in (tmp_path / "v" / "regression-verify.json").read_text(encoding="utf-8")


def test_cli_legacy_pack_rejected_then_conservative(tmp_path, capsys):
    cases = _write_official(tmp_path / "fail.jsonl", FAIL)
    out = tmp_path / "pack"
    assert cli.main(["regression", "build", "--cases", str(cases), "--out", str(out)]) == 0

    payload = json.loads((out / "regression-pack.json").read_text(encoding="utf-8"))
    legacy = {
        "schema": LEGACY_SCHEMA,
        "created_from": payload["created_from"],
        "expectations": [
            _legacy_entry(
                success=e["verdict"] == "PASS",
                case_id=e["case_id"], run_id=e["run_id"],
                labels=e["labels"], first=e["first_fault_event_id"], quality=e["quality_score"],
            )
            for e in payload["expectations"]
        ],
    }
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    code = cli.main(["regression", "verify", "--pack", str(legacy_path),
                     "--cases", str(cases), "--out", str(tmp_path / "v1")])
    assert code == 2, "旧快照默认不可用：数据格式不兼容退出码 2"
    assert "三态" in capsys.readouterr().err

    code = cli.main(["regression", "verify", "--pack", str(legacy_path),
                     "--cases", str(cases), "--out", str(tmp_path / "v2"), "--allow-legacy"])
    captured = capsys.readouterr()
    assert code == 1, "降级读取不能宣称通过"
    assert "降级读取" in captured.err
    md = (tmp_path / "v2" / "regression-verify.md").read_text(encoding="utf-8")
    assert "旧快照降级读取" in md
    assert "复测通过" not in md
