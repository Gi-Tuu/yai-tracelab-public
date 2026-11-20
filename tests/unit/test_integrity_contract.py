"""C-04 / C-06 / C-07 / C-08 / C-11 / C-13 / C-40 / EVAL-06 / P3 的回归测试。

全部使用**合成对象**（`tests/builders.py` + 直接构造的 RunAssessment），
不依赖官方资料包，因此在公开镜像里也能跑。

这些测试的写法要求：**旧实现必须失败**。每条断言都对应一个已复现的缺陷，
不是"把当前行为拍成快照"。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab import cli, reports
from tracelab.contracts import Dataset, Verdict
from tracelab.evaluation import (
    CaseResult,
    EvaluationResult,
    build_manifest,
    compare_versions,
    evaluate_dataset,
    score_against_labels,
    summarise,
)
from tracelab.official_report import _collect_stats, build_official_report
from tracelab.rules import Violation

# --------------------------------------------------------------- 构造工具


def _violation(
    label: str,
    *,
    event_id: str | None = "C1-A-01",
    dimension: str = "PROCESS",
    points: float = 10.0,
) -> Violation:
    return Violation(
        rule_id="R999", rule_version="test", label=label, event_id=event_id, seq=1,
        evidence_ids=(event_id,) if event_id else (), explanation="测试根因",
        dimension=dimension, points=points, confidence=1.0,
    )


def _assess(
    verdict: Verdict,
    labels: tuple[str, ...] = (),
    *,
    run_id: str,
    score: float = 50.0,
    dimension: str = "PROCESS",
    event_id: str = "C1-A-01",
) -> object:
    if verdict is Verdict.UNKNOWN and not labels:
        labels = ("INSUFFICIENT_EVIDENCE",)
    return _make_assessment(
        verdict, labels, run_id=run_id, score=score, dimension=dimension, event_id=event_id
    )


def _make_assessment(verdict, labels, *, run_id, score, dimension, event_id="C1-A-01"):
    from tracelab.attribution import RunAssessment

    return RunAssessment(
        case_id=run_id.rsplit("-", 1)[0], run_id=run_id, agent_version="test-v1",
        verdict=verdict, business_invoice_created=False, quality_score=score,
        violations=[_violation(lab, event_id=event_id, dimension=dimension) for lab in labels],
    )


def _real_case(case_id: str):
    # 两版必须给不同 agent_version：否则评测器无法证明"第 0 条是基线"（C-09 守卫）
    return make_case(
        case_id,
        context=context(),
        reference_result=reference(),
        runs=[make_run(f"{case_id}-A", basic_flow(f"{case_id}A"), agent_version="baseline-v1.0"),
              make_run(f"{case_id}-B", basic_flow(f"{case_id}B"), agent_version="candidate-v2.0")],
    )


def _pair(
    case_id: str,
    base: object,
    cand: object,
) -> CaseResult:
    """用真实 compare_versions 生成方向，避免测试自己编方向。"""
    case = _real_case(case_id)
    return CaseResult(
        case=case,
        assessments=[base, cand],
        comparison=compare_versions(case, base, cand),
    )


def _result(case_results: list[CaseResult]) -> EvaluationResult:
    cases = [cr.case for cr in case_results]
    dataset = Dataset(
        cases=cases,
        source="synthetic.jsonl",
        sha256="0" * 64,
        stats={
            "lines_total": len(cases),
            "cases_parsed": len(cases),
            "runs_total": sum(len(c.runs) for c in cases),
            "skipped": 0,
            "truncated": 0,
        },
        adapter_name="official",
        adapter_version="test",
    )
    unknown_runs = sum(1 for cr in case_results for a in cr.assessments if a.is_unknown)
    empty_cases = sum(1 for cr in case_results if not cr.assessments)
    manifest = build_manifest(
        dataset,
        extra={
            "unknown_runs": unknown_runs,
            "degraded_runs": 0,
            "empty_cases": empty_cases,
            "evaluation_complete": unknown_runs == 0 and empty_cases == 0,
        },
    )
    return EvaluationResult(dataset=dataset, results=case_results, manifest=manifest)


def _gold(run_id: str, labels: list[str], *, events: dict[str, str | None] | None = None) -> dict:
    events = events or {}
    return {
        "run_id": run_id,
        "agent_task_success": False,
        "business_invoice_created": False,
        "root_causes": [
            {"label": lab, "first_fault_event_id": events.get(lab)} for lab in labels
        ],
    }


# ------------------------------------------------- C-04 统一正确率口径


def _unknown_mixed_result() -> EvaluationResult:
    """基线 PASS、候选 UNKNOWN：候选版没有任何"判错"，只有"判不了"。"""
    return _result([_pair("C1", _assess(Verdict.PASS, (), run_id="C1-A", score=100.0),
                          _assess(Verdict.UNKNOWN, run_id="C1-B", score=0.0))])


def test_c04_official_accuracy_denominator_excludes_unknown():
    result = _unknown_mixed_result()
    base, cand = _collect_stats(result)
    assert (base.runs, base.evaluated, cand.runs, cand.evaluated) == (1, 1, 1, 0)
    assert base.accuracy == 1.0
    # 旧实现：cand.accuracy == 0.0（把"判不了"算成"判错"）
    assert cand.accuracy is None, "UNKNOWN 不进分母；无可评估 run 时必须为 None，不能是 0"

    sec = build_official_report(result)
    line = next(x for x in sec.splitlines() if x.startswith("| Agent 处理正确率"))
    assert "A 1，B 0；UNKNOWN 不进分母" in line, line
    assert "| 0.0% |" not in line, f"UNKNOWN 不能被渲染成 0% 正确率：{line}"


def test_c04_section4_change_uses_direction_not_boolean_recount():
    """§3 说证据不足、§4 不能说候选版退化（旧实现正是如此）。"""
    text = build_official_report(_unknown_mixed_result())
    sec4 = text[text.index("## 4."):text.index("## 5.")]
    row = next(x for x in sec4.splitlines() if "批量、多轮及其他" in x)
    assert "不可评估（证据不足）" in row, row
    assert "退化" not in row, f"insufficient_evidence 不得写成退化：{row}"


def test_c04_markdown_mark_unknown_is_not_a_cross():
    md = reports.build_markdown(_unknown_mixed_result())
    row = next(x for x in md.splitlines() if x.startswith("| C1 |"))
    assert "⚠️ 证据不足" in row
    assert "❌" not in row, f"UNKNOWN 不能用 ❌ 当成判错：{row}"
    # 标签必须保留
    assert "INSUFFICIENT_EVIDENCE" in row


# ------------------------------- C-06 聚合把"两版都错"当"无差异"


def test_c06_summarise_splits_equal_both_fail_from_both_pass():
    improved = _pair("C1", _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-A"),
                     _assess(Verdict.PASS, (), run_id="C1-B"))
    both_fail = _pair("C2", _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C2-A"),
                      _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C2-B"))
    both_pass = _pair("C3", _assess(Verdict.PASS, (), run_id="C3-A", score=100.0),
                      _assess(Verdict.PASS, (), run_id="C3-B", score=100.0))
    summary = summarise(_result([improved, both_fail, both_pass]))
    assert summary["equal_both_fail"] == 1
    assert summary["equal_both_pass"] == 1
    assert both_fail.comparison.direction == "equal"


def test_c06_both_fail_blocks_release_and_names_the_count():
    improved = _pair("C1", _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-A"),
                     _assess(Verdict.PASS, (), run_id="C1-B"))
    both_fail = _pair("C2", _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C2-A"),
                      _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C2-B"))
    recommendation, reason = reports.aggregate_recommendation(_result([improved, both_fail]))
    # 旧实现：只有 candidate_better + equal → release
    assert recommendation != "release"
    assert recommendation in {"defer", "investigate"}
    assert "1 个 Case 两版同错" in reason, reason


def test_c06_all_fail_batch_is_investigate_not_gray():
    both_fail = [
        _pair("C1", _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-A"),
              _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-B")),
        _pair("C2", _assess(Verdict.FAIL, ("DUPLICATE_CONTROL",), run_id="C2-A"),
              _assess(Verdict.FAIL, ("DUPLICATE_CONTROL",), run_id="C2-B")),
    ]
    recommendation, reason = reports.aggregate_recommendation(_result(both_fail))
    # 旧实现：方向全 equal → gray「无差异」
    assert recommendation == "investigate"
    assert "两版同错" in reason


def test_c06_all_pass_equal_still_gray():
    both_pass = [
        _pair("C1", _assess(Verdict.PASS, (), run_id="C1-A", score=100.0),
              _assess(Verdict.PASS, (), run_id="C1-B", score=100.0)),
    ]
    recommendation, _ = reports.aggregate_recommendation(_result(both_pass))
    assert recommendation == "gray"


# ------------------------------------------------ C-11 平均质量分分母


def test_c11_avg_quality_excludes_unknown_in_summary():
    result = _result([
        _pair("C1", _assess(Verdict.PASS, (), run_id="C1-A", score=100.0),
              _assess(Verdict.UNKNOWN, run_id="C1-B", score=0.0)),
    ])
    summary = summarise(result)
    # 旧实现：avg_quality = (100 + 0) / 2 = 50.0
    assert summary["avg_quality"] == 100.0
    assert summary["avg_quality_scored_runs"] == 1
    assert summary["avg_quality_excluded_unknown"] == 1
    assert "分母 1 条可评估运行" in reports.build_markdown(result)


def test_c11_official_avg_quality_excludes_unknown_and_states_denominator():
    result = _result([
        _pair("C1", _assess(Verdict.PASS, (), run_id="C1-A", score=100.0),
              _assess(Verdict.UNKNOWN, run_id="C1-B", score=0.0)),
    ])
    _base, cand = _collect_stats(result)
    # 旧实现：cand.avg_quality == 0.0（UNKNOWN 的 0 分进了平均）
    assert cand.avg_quality is None
    row = next(x for x in build_official_report(result).splitlines() if x.startswith("| 平均质量分"))
    assert "分母排除 UNKNOWN" in row
    assert "（A 1，B 0" in row, row


# ------------------------------------- C-13 标注与数据 run_id 对不上


def _labels_for_c13() -> dict:
    return {
        "C1": {
            "run_assessments": [
                _gold("C1-A", []),
                _gold("C1-GHOST", []),
            ],
        }
    }


def test_c13_score_reports_matched_and_unmatched_labels():
    result = _result([
        _pair("C1", _assess(Verdict.PASS, (), run_id="C1-A", score=100.0),
              _assess(Verdict.PASS, (), run_id="C1-B", score=100.0)),
    ])
    metrics = score_against_labels(result, _labels_for_c13()).values
    assert metrics["labels_matched"] == 1
    assert metrics["labels_unmatched"] == 1


def _official_case_line(case_id: str = "C1") -> str:
    """用 builders 的 raw 拼一行官方格式 JSON（run_id 可控）。"""
    runs = [make_run(f"{case_id}-A", basic_flow(f"{case_id}A")),
            make_run(f"{case_id}-B", basic_flow(f"{case_id}B"))]
    raw_runs = []
    for run in runs:
        raw = dict(run.raw)
        raw["events"] = [e.raw for e in run.events]  # builders 的 Run.raw 不含 events
        raw_runs.append(raw)
    obj = {
        "case_id": case_id,
        "scenario": "测试",
        "user_request": "测试请求",
        "context": context(),
        "reference_result": reference(),
        "runs": raw_runs,
        "dataset_version": "test",
        "data_origin": "synthetic",
    }
    return json.dumps(obj, ensure_ascii=False)


def test_c13_cli_warns_when_labels_do_not_match_runs(tmp_path, capsys):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(_official_case_line() + "\n", encoding="utf-8")
    labels = tmp_path / "labels.jsonl"
    label_entry = {"case_id": "C1", **_labels_for_c13()["C1"]}
    labels.write_text(json.dumps(label_entry, ensure_ascii=False) + "\n", encoding="utf-8")

    code = cli.main(["evaluate", "--cases", str(cases), "--labels", str(labels),
                     "--out", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 0
    assert "labels_unmatched=1" in captured.err, captured.err
    assert "警告" in captured.err


# ------------------------------------- C-40 label_exact_match_rate 命名


def test_c40_label_exact_match_rate_is_the_documented_name():
    result = _result([
        _pair("C1", _assess(Verdict.PASS, (), run_id="C1-A", score=100.0),
              _assess(Verdict.PASS, (), run_id="C1-B", score=100.0)),
    ])
    metrics = score_against_labels(result, {"C1": {"run_assessments": [
        _gold("C1-A", []), _gold("C1-B", []),
    ]}}).values
    assert metrics["label_exact_match_rate"] == 1.0
    assert metrics["label_coverage"] == metrics["label_exact_match_rate"]
    assert "已废弃" in score_against_labels.__doc__


# ------------------------------- EVAL-06 首因 EM 分母与去重


def test_eval06_first_fault_skips_undefined_gold_and_dedupes():
    result = _result([
        _pair("C1", _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-A", event_id="E1"),
              _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id="C1-B", event_id="E1")),
    ])
    labels = {"C1": {"run_assessments": [
        {
            "run_id": "C1-A",
            "agent_task_success": False,
            "business_invoice_created": False,
            "root_causes": [
                {"label": "AMOUNT_BASIS", "first_fault_event_id": "E1"},
                # 同一 (label, event_id) 重复出现 → 只能算一次
                {"label": "AMOUNT_BASIS", "first_fault_event_id": "E1"},
                # 金标首因未定义 → 跳过，不进分母
                {"label": "FIELD_FABRICATION", "first_fault_event_id": None},
            ],
        },
        _gold("C1-B", ["AMOUNT_BASIS"]),
    ]}}
    metrics = score_against_labels(result, labels).values
    # 旧实现：分母 3（重复计分 + 空金标），命中 1 → ≈0.3333
    assert metrics["first_fault_event_exact_match"] == 1.0
    assert metrics["first_fault_exact_match_undefined"] == 2


# ------------------------------------ C-07 / C-08 CLI 退出码与 0-run


def _zero_run_file(tmp_path: Path) -> Path:
    path = tmp_path / "zero-run.jsonl"
    path.write_text(
        json.dumps({
            "case_id": "EMPTY-1", "scenario": "空运行", "user_request": "测试",
            "context": {}, "reference_result": {}, "runs": [],
            "dataset_version": "test", "data_origin": "synthetic",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def test_c08_zero_run_case_does_not_crash_any_report(tmp_path, capsys):
    path = _zero_run_file(tmp_path)
    code = cli.main(["evaluate", "--cases", str(path), "--out", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert code == 3, "0-run Case 必须非 0 退出"
    for name in ("report.md", "report-official.md", "report-business.md",
                 "report-engineering.md", "report-product.md"):
        text = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert "该 Case 无可评估运行" in text, name
        assert "EMPTY-1" in text


def test_c08_zero_run_case_produces_empty_assessments_without_indexerror(tmp_path):
    from tracelab.adapters import load_dataset

    dataset = load_dataset(_zero_run_file(tmp_path))
    result = evaluate_dataset(dataset)
    cr = result.results[0]
    assert cr.assessments == []
    assert cr.base is None and cr.cand is None
    assert cr.comparison.direction == "insufficient_evidence"
    assert summarise(result)["cases_without_runs"] == 1
    assert result.manifest["integrity"]["evaluation_complete"] is False


def test_c07_allow_incomplete_explicitly_allows_exit_zero(tmp_path):
    path = _zero_run_file(tmp_path)
    code = cli.main(["evaluate", "--cases", str(path), "--out", str(tmp_path / "o"),
                     "--allow-incomplete"])
    assert code == 0


def test_c07_unreadable_file_returns_three(tmp_path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")
    code = cli.main(["evaluate", "--cases", str(path), "--out", str(tmp_path / "o")])
    captured = capsys.readouterr()
    assert code == 3
    assert "read_complete=false" in captured.err


def test_c07_manifest_missing_file_reports_cleanly(tmp_path, capsys):
    code = cli.main(["manifest", "--cases", str(tmp_path / "nope.jsonl")])
    captured = capsys.readouterr()
    assert code == 2
    assert "找不到 Case 文件" in captured.err
    assert "Traceback" not in captured.err


# ------------------------------------------------ P3 截断与排序 key


def test_p3_top_n_truncation_is_announced():
    pairs = []
    for i in range(16):
        # 只让候选版失败：失败运行正好 16 条，便于断言截断文案
        pairs.append(_pair(
            f"C{i}",
            _assess(Verdict.PASS, (), run_id=f"C{i}-A", score=100.0),
            _assess(Verdict.FAIL, ("AMOUNT_BASIS",), run_id=f"C{i}-B", score=40.0),
        ))
    md = reports.build_markdown(_result(pairs))
    assert "仅展示前 14 条失败运行 / 共 16 条" in md


def test_p3_severity_sort_uses_worst_dimension_not_first_violation():
    """严重度必须取所有违规里最重的一维；旧实现只看 violations[0]。"""
    risky = _pair(
        "RISK",
        _make_assessment(Verdict.FAIL, ("RISK_CONTROL", "PROCESS"),
                         run_id="RISK-A", score=90.0, dimension="PROCESS", event_id="E1"),
        _make_assessment(Verdict.FAIL, ("RISK_CONTROL", "PROCESS"),
                         run_id="RISK-B", score=90.0, dimension="PROCESS", event_id="E1"),
    )
    # RISK_CONTROL 是第一条、PROCESS 是第二条；把 PROCESS 放前面才考验 min()
    for a in risky.assessments:
        a.violations = [
            _violation("PROCESS_ONLY", dimension="PROCESS"),
            _violation("RISK_CONTROL", dimension="RISK_CONTROL"),
        ]
    plain = _pair(
        "PLAIN",
        _make_assessment(Verdict.FAIL, ("PROCESS",), run_id="PLAIN-A", score=50.0,
                         dimension="PROCESS"),
        _make_assessment(Verdict.FAIL, ("PROCESS",), run_id="PLAIN-B", score=50.0,
                         dimension="PROCESS"),
    )
    md = reports.build_markdown(_result([plain, risky]))
    risk_pos = md.index("### RISK /")
    plain_pos = md.index("### PLAIN /")
    assert risk_pos < plain_pos, "RISK_CONTROL 级问题必须排在纯 PROCESS 问题之前"


def test_c09_same_agent_version_refuses_to_compare():
    """C-09：runs 只按位置区分基线/候选；两版版本号相同就无法证明谁是谁 → 拒判。"""
    from tracelab.evaluation import compare_versions as _cmp

    def _one(cid):
        return make_case(
            cid, context=context(), reference_result=reference(),
            runs=[make_run(f"{cid}-A", basic_flow(f"{cid}A"), agent_version="same-v1"),
                  make_run(f"{cid}-B", basic_flow(f"{cid}B"), agent_version="same-v1")],
        )

    case = _one("SAME")
    a0, a1 = _assess(Verdict.PASS, run_id="SAME-A"), _assess(Verdict.FAIL, ("AMOUNT_BASIS",),
                                                            run_id="SAME-B")
    cmp = _cmp(case, a0, a1)
    assert cmp.direction == "insufficient_evidence"
    assert "agent_version" in cmp.reason

    # 正常数据契约（两版不同）仍然能给出方向
    ok_case = make_case(
        "OK", context=context(), reference_result=reference(),
        runs=[make_run("OK-A", basic_flow("okA"), agent_version="baseline-v1.0"),
              make_run("OK-B", basic_flow("okB"), agent_version="candidate-v2.0")],
    )
    assert _cmp(ok_case, a0, a1).direction == "baseline_better"


def test_c05_ungrounded_violation_is_downgraded_to_unknown():
    """C-05：规则命中但指不出证据事件时，不得产出"可证实违规"。

    官方 `08_提交格式校验.py:139` 要求非 INSUFFICIENT_EVIDENCE 的根因必须带本 run 事件，
    否则整份 submission 被判「根因首个错误事件无效」。这里用一条**缺 PARSE_INTENT** 的
    取消任务轨迹复现：旧实现 give INTENT_CLASSIFICATION + first_fault_event_id=None。
    """
    from tracelab.attribution import assess_run

    events = [e for e in basic_flow(prefix="g", ) if e.step_id != "PARSE_INTENT"]
    run = make_run("RUN-G", events, final_state="SUCCEEDED", invoice_created=True)
    case = make_case(
        "INV-G01",
        context=context(),
        reference_result=reference(required_action="CANCEL", acceptable_snapshot_state="CANCELED",
                                   invoice_should_exist=False),
        runs=[run],
    )
    a = assess_run(case, run)
    # 不再有"非拒判型 + 无事件坐标"的违规
    ungrounded = [v for v in a.violations
                  if v.event_id is None and v.label != "INSUFFICIENT_EVIDENCE"]
    assert ungrounded == [], f"仍存在无证据坐标的确定违规：{ungrounded}"
    assert a.verdict is Verdict.UNKNOWN, "拿不到证据事件时只能是 UNKNOWN"
    # 提交 payload 里也不允许出现 null 首因的非 INSUFFICIENT_EVIDENCE 根因
    from tracelab.evaluation import CaseResult, VersionComparison
    row = CaseResult(case=case, assessments=[a],
                     comparison=VersionComparison("insufficient_evidence", "investigate", "x")).to_submission()
    for rc in row["run_assessments"][0]["root_causes"]:
        if rc["label"] != "INSUFFICIENT_EVIDENCE":
            assert rc["first_fault_event_id"] is not None, rc
