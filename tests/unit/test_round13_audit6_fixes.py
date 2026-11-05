"""第六轮审计修复回归（F6-01 ~ F6-05）。

覆盖：文档模块地图、规则注册幂等、类型反射、demo 退出码、公开 API 快照。
"""

from __future__ import annotations

import json
import subprocess
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "sample-traces.jsonl"

EXPECTED_RULES = [
    "R001", "R002", "R003", "R004", "R005", "R006", "R007", "R008",
    "R009", "R010", "R011", "R012", "R013", "R014", "R902", "R015",
]


# ------------------------------------------------------------------ F6-02


def test_rule_reload_is_idempotent():
    import importlib

    import tracelab.rules as rules
    import tracelab.rules_business as business

    assert [rule.rule_id for rule in rules.RULES] == EXPECTED_RULES
    importlib.reload(business)
    assert [rule.rule_id for rule in rules.RULES] == EXPECTED_RULES, "reload 后不得重复注册"
    assert len(rules.RULES) == len({rule.rule_id for rule in rules.RULES})


# ------------------------------------------------------------------ F6-03


def test_public_type_hints_resolvable():
    code = (
        "import typing;"
        "import tracelab.metrics as m;"
        "import tracelab.summary as s;"
        "assert typing.get_type_hints(m.score_against_labels)['result'] is typing.Any;"
        "assert typing.get_type_hints(s.summarise)['result'] is typing.Any;"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout

    # 导入 evaluation 后，应升级为真实类型而不是 Any
    import tracelab.evaluation  # noqa: F401
    import tracelab.metrics as metrics

    assert typing.get_type_hints(metrics.score_against_labels)["result"] is tracelab.evaluation.EvaluationResult


# ------------------------------------------------------------------ F6-04


def test_demo_propagates_incomplete_exit_code(tmp_path):
    from tracelab.cli import main

    case = json.loads(SAMPLE.read_text(encoding="utf-8").splitlines()[0])
    case["case_id"] = case["case_id"] + "-ZERO"
    case["runs"] = []
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")

    out = tmp_path / "demo"
    code = main(["demo", "--no-open", "--cases", str(cases), "--out", str(out)])
    assert code == 3, "demo 必须透传'数据不完整'退出码"
    assert (out / "demo.html").is_file(), "即使不完整，体验页仍应生成"


# ------------------------------------------------------------------ F6-01 / F6-05


def test_public_api_snapshot():
    import tracelab.adapters as adapters
    import tracelab.evaluation as evaluation
    import tracelab.rules as rules

    assert {"RULE_VERSION", "RULES", "Violation", "evaluate_rules", "evaluate_with_trace"} <= set(rules.__all__)
    assert {"Adapter", "STEP_ALIASES", "sha256_file", "load_dataset", "get_adapter"} <= set(adapters.__all__)
    for name in ("EVALUATOR_VERSION", "CaseResult", "EvaluationResult", "VersionComparison",
                 "compare_versions", "LabelFormatError", "Metrics", "load_reference_labels",
                 "score_against_labels", "validate_label_fields", "summarise",
                 "build_manifest", "evaluate_dataset"):
        assert name in evaluation.__all__, name
        assert getattr(evaluation, name, None) is not None, name
    for name in rules.__all__:
        assert hasattr(rules, name), name


def test_docs_reflect_split_modules():
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    method = (ROOT / "docs" / "method.md").read_text(encoding="utf-8")
    evidence = (ROOT / "tracelab" / "evidence_record.py").read_text(encoding="utf-8")

    for name in ("adapters_common.py", "adapters_official.py", "adapters_generic.py",
                 "rule_defs.py", "rules_business.py", "rules_control.py",
                 "version_compare.py", "metrics.py", "summary.py"):
        assert name in architecture, name
    assert "规则定义仍在 `tracelab/rules.py`" not in architecture
    assert "业务规则定义仍在 `tracelab/rules.py`" not in method
    assert "tracelab.rules._submit_result_unknown" not in evidence
