"""第三轮审计修复的回归测试（组 3/4：清理归属 + 回归完整性 + 报销包口径）。

R3-04：`evaluate --out` 只能清理本工具自己的产物，绝不覆盖用户同名文件；
R3-05：`regression verify` 必须把数据完整性纳入"能否宣称通过"；
R3-06：EXP004 没有结算动作时不得判"满足"；违规必须能定位到证据事件。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.builders import make_case, make_event, make_run
from tracelab.contracts import Verdict
from tracelab.regression import verify_pack
from tracelab.rulepacks import EXPENSE_PACK, evaluate_with_pack

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "tracelab", *args],
                          cwd=str(ROOT), capture_output=True, text=True)


# ------------------------------------------------------------------ R3-04 清理归属


def test_evaluate_never_overwrites_user_files(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.md").write_text("# 我自己的笔记\n", encoding="utf-8")
    (out / "run.json").write_text('{"mine": true}', encoding="utf-8")
    proc = _run_cli(["evaluate", "--cases", str(SAMPLE), "--out", str(out)])
    assert proc.returncode == 2, proc.stderr[-400:]
    assert "不是本次产物的同名文件" in proc.stderr
    assert (out / "report.md").read_text(encoding="utf-8") == "# 我自己的笔记\n"
    assert (out / "run.json").exists()
    assert not (out / "results.json").exists(), "冲突时必须一个产物都不写"


def test_evaluate_cleans_only_own_outputs_on_rerun(tmp_path):
    out = tmp_path / "out"
    first = _run_cli(["evaluate", "--cases", str(SAMPLE), "--out", str(out)])
    assert first.returncode == 0, first.stderr[-400:]
    (out / "user-notes.md").write_text("用户文件", encoding="utf-8")
    second = _run_cli(["evaluate", "--cases", str(SAMPLE), "--out", str(out)])
    assert second.returncode == 0, second.stderr[-400:]
    assert "已清理上次评测的" in second.stderr
    assert (out / "user-notes.md").read_text(encoding="utf-8") == "用户文件"
    assert (out / ".tracelab-manifest.json").exists(), "应写下本次产出清单"
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert payload["manifest"]["dataset"]["source"].endswith("sample-traces.jsonl")


def test_bad_labels_do_not_touch_existing_outputs(tmp_path):
    out = tmp_path / "out"
    assert _run_cli(["evaluate", "--cases", str(SAMPLE), "--out", str(out)]).returncode == 0
    before = sorted(p.name for p in out.iterdir())
    labels = tmp_path / "bad-labels.jsonl"
    labels.write_text('{"case_id":"X","run_assessments":[{"agent_task_success":true}]}\n',
                      encoding="utf-8")
    proc = _run_cli(["evaluate", "--cases", str(SAMPLE), "--labels", str(labels), "--out", str(out)])
    assert proc.returncode == 2
    assert sorted(p.name for p in out.iterdir()) == before, "标注校验失败时旧产物不得被删改"


# ------------------------------------------------------------------ R3-05 回归完整性


def _dataset_with_duplicate_seq(tmp_path: Path) -> Path:
    rows = [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]
    case = next(row for row in rows if row.get("runs"))
    events = case["runs"][0]["events"]
    if len(events) >= 4:
        events[3]["seq"] = events[2]["seq"]       # 造 SEQ_DUPLICATE（schema error）
    path = tmp_path / "dup-seq.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def test_regression_verify_reports_integrity_problems(tmp_path):
    from tracelab.adapters import load_dataset
    from tracelab.regression import build_expectations, write_pack

    cases = _dataset_with_duplicate_seq(tmp_path)
    dataset = load_dataset(cases)
    assert dataset.errors, "构造的数据必须带加载期 error"
    pack_dir = tmp_path / "pack"
    from tracelab.evaluation import evaluate_dataset
    write_pack(build_expectations(evaluate_dataset(dataset)), pack_dir, dataset)
    pack = json.loads((pack_dir / "regression-pack.json").read_text(encoding="utf-8"))
    report = verify_pack(pack, dataset)
    assert report.integrity, "必须把加载期 error 记进完整性"
    assert report.has_problem is True
    assert report.exit_code == 1
    text = report.to_dict()["integrity"]
    assert any(item["field"] == "errors" for item in text)


# ------------------------------------------------------------------ R3-06 报销包口径


def _expense_run(steps: list) -> object:
    run = make_run("R1", steps, final_state="SUCCEEDED", invoice_created=False)
    case = make_case("E1", context={}, reference_result={}, runs=[run])
    return evaluate_with_pack(EXPENSE_PACK, case, run)


def _claim(already_settled: bool = False):
    return make_event(
        "E1", 1, "RECEIVE_CLAIM",
        observed_output={"claim_amount": 500.0, "already_settled": already_settled,
                         "line_items": [{"exclusive_amount": 500.0}]},
    )


def test_exp004_without_settlement_is_not_satisfied():
    assessment = _expense_run([
        _claim(),
        make_event("E2", 2, "CHECK_BUDGET", observed_output={"budget_remaining": 1000.0}),
        make_event("E3", 3, "APPROVE_MANAGER", observed_output={"approver": "MANAGER"}),
        make_event("E4", 4, "APPROVE_FINANCE", observed_output={"approver": "FINANCE"}),
    ])
    outcome = next(o for o in assessment.outcomes if o.rule_id == "EXP004")
    assert outcome.violated is not True, "没有结算动作时不得判「满足」"
    assert assessment.verdict is Verdict.UNKNOWN


def test_exp003_without_finance_event_is_unknown_not_unevidenced_violation():
    assessment = _expense_run([
        _claim(),
        make_event("F2", 2, "CHECK_BUDGET", observed_output={"budget_remaining": 1000.0}),
        make_event("F3", 3, "APPROVE_MANAGER", observed_output={"approver": "MANAGER"}),
        make_event("F4", 4, "SETTLE_EXPENSE", observed_output={}),
    ])
    outcome = next(o for o in assessment.outcomes if o.rule_id == "EXP003")
    if outcome.violated is True:
        raise AssertionError("违规必须能定位证据坐标，缺失时按证据不足处理")
    assert outcome.violated is None
    assert assessment.verdict is Verdict.UNKNOWN
