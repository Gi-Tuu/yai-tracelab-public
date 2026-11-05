"""最后两条 P4/POTENTIAL 的回归测试（第四轮收尾）。

R4-A-09：`snapshot_state` / `workflow_terminal` 是 `final_state` 的派生字段，
          改终态时必须一起重算，否则 IR 与 raw 都不自洽；
R4-A-08：`build_manifest` 的 `evaluated` 改为显式参数，False 不得输出布尔计数，
          True 时未传的计数按 0（责任在调用方）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.contracts import Dataset
from tracelab.evaluation import build_manifest, evaluate_dataset
from tracelab.metamorphic import rebuild_run
from tracelab.reports import ab_pair

ROOT = Path(__file__).resolve().parents[2]


def _case(case_id: str = "P4", runs: int = 1):
    run = make_run(f"{case_id}-A", basic_flow(prefix=case_id), final_state="SUCCEEDED",
                   invoice_created=True)
    runs_obj = tuple(
        replace(run, run_id=f"{case_id}-{index}") for index in range(runs)
    )
    return make_case(case_id, context=context(), reference_result=reference(), runs=runs_obj)


def _dataset(cases) -> Dataset:
    return Dataset(cases=list(cases), issues=[], source="t", sha256="",
                   stats={"lines_total": len(cases), "cases_parsed": len(cases),
                          "skipped": 0, "truncated": 0})


# ---------------------------------------------------------------- 派生字段


def test_rebuild_run_recomputes_derived_terminal_fields():
    run = _case().runs[0]
    rebuilt = rebuild_run(run, final_state="FAILED")
    assert rebuilt.final_state == "FAILED"
    assert rebuilt.snapshot_state == "FAILED", "快照态是 final_state 的派生字段（R4-A-09）"
    assert rebuilt.workflow_terminal is True
    assert rebuilt.raw["final_state"] == "FAILED"
    assert rebuilt.raw["snapshot_state"] == "FAILED"
    assert rebuilt.raw["workflow_terminal"] is True

    waiting = rebuild_run(run, final_state="WAITING_USER")
    assert waiting.workflow_terminal is False
    assert waiting.snapshot_state == "WAITING_USER"


# ---------------------------------------------------------------- manifest 三态


def test_build_manifest_explicit_evaluated_flag():
    ds = _dataset([_case("M")])

    # 显式 False：即便传了计数也不得输出布尔值（"未评测"优先）
    unevaluated = build_manifest(ds, extra={"unknown_runs": 2}, evaluated=False)
    assert unevaluated["integrity"]["evaluated"] is False
    assert unevaluated["integrity"]["unknown_runs"] is None
    assert unevaluated["integrity"]["complete"] is None

    # 显式 True：计数保留；未传的计数按 0（责任在调用方）
    declared = build_manifest(ds, extra={"unknown_runs": 2}, evaluated=True)
    assert declared["integrity"]["evaluated"] is True
    assert declared["integrity"]["unknown_runs"] == 2
    assert declared["integrity"]["degraded_runs"] == 0
    assert isinstance(declared["integrity"]["complete"], bool)

    # 都不给：任一计数键出现即视为已评测（不漏 degraded_runs）
    inferred = build_manifest(ds, extra={"degraded_runs": 1})
    assert inferred["integrity"]["evaluated"] is True
    assert inferred["integrity"]["degraded_runs"] == 1


# ---------------------------------------------------------------- §6 权重口径


def test_release_section_weight_counts_comparable_pairs(tmp_path):
    pair_case = _case("PAIR", runs=2)
    zero = make_case("ZERO", context=context(), reference_result=reference(), runs=())
    result = evaluate_dataset(_dataset([pair_case, zero]))
    assert sum(1 for cr in result.results if ab_pair(cr)) == 1
    from tracelab.official_report import write_official_report

    text = write_official_report(result, tmp_path / "r.md").read_text(encoding="utf-8")
    assert "1 个可比较 A/B 对" in text, "单例权重按可比较 A/B 对算（0-run 不稀释）"
    assert "6.7" not in text.split("不确定性")[1][:80]
