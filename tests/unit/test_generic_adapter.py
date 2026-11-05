"""异构格式适配器测试：证明"接入新 Agent 不改核心"不是口号。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracelab.adapters import get_adapter, load_dataset
from tracelab.evaluation import evaluate_dataset

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "data" / "demo" / "generic-traces.jsonl"

pytestmark = pytest.mark.skipif(not DEMO.exists(), reason="缺少异构格式演示数据")


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DEMO, adapter="generic")


def test_generic_adapter_is_registered():
    assert get_adapter("generic").name == "generic"
    assert get_adapter("official").name == "official"
    with pytest.raises(KeyError):
        get_adapter("nope")


def test_generic_format_maps_to_same_ir(dataset):
    assert len(dataset) == 3
    assert sum(len(c.runs) for c in dataset) == 6
    assert not dataset.errors
    assert dataset.stats["truncated"] == 0 and dataset.stats["skipped"] == 0

    case = dataset.cases[0]
    assert case.case_id == "GEN-01"
    assert case.context["seller_id"] == "SELLER-DEMO-01"
    assert case.reference_result["amount_basis"] == "tax_exclusive"
    # 缺 seq 的格式由适配器按顺序补齐
    assert [e.seq for e in case.runs[0].events] == list(range(1, len(case.runs[0].events) + 1))


def test_generic_events_keep_original_ids(dataset):
    ids = [e.event_id for c in dataset for r in c.runs for e in r.events]
    assert "GEN-01-C-02" in ids
    assert len(ids) == len(set(ids)), "event_id 必须唯一"


def test_same_rules_fire_on_different_format(dataset):
    """核心断言：换个输入格式，R001/R012 依然命中，规则代码零改动。"""
    result = evaluate_dataset(dataset)
    by_run = {a.run_id: a for cr in result.results for a in cr.assessments}

    assert by_run["GEN-01-base"].agent_task_success is True
    assert by_run["GEN-01-cand"].labels == ["AMOUNT_BASIS"]
    assert by_run["GEN-01-cand"].violations[0].rule_id == "R001"

    assert by_run["GEN-02-base"].agent_task_success is True
    assert by_run["GEN-02-cand"].agent_task_success is True

    assert by_run["GEN-03-base"].agent_task_success is True
    assert by_run["GEN-03-cand"].labels == ["CALLBACK_MISINTERPRETATION"]
    assert by_run["GEN-03-cand"].violations[0].rule_id == "R012"


def test_generic_directions(dataset):
    result = evaluate_dataset(dataset)
    got = {cr.case.case_id: cr.comparison.direction for cr in result.results}
    assert got == {"GEN-01": "baseline_better", "GEN-02": "equal", "GEN-03": "baseline_better"}


def test_generic_adapter_reuses_integrity_checks(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"case": {"id": "X"}, "trajectories": []}\n{oops\n', encoding="utf-8")
    ds = load_dataset(bad, adapter="generic")
    assert any(i.code == "BAD_JSON" for i in ds.errors)
    assert ds.stats["skipped"] == 1


def test_cli_evaluate_with_generic_adapter(tmp_path, capsys):
    from tracelab import cli

    code = cli.main(["evaluate", "--adapter", "generic", "--cases", str(DEMO),
                     "--out", str(tmp_path / "o")])
    assert code == 0
    assert "Case 数：3" in capsys.readouterr().out
    assert (tmp_path / "o" / "submission.jsonl").exists()


