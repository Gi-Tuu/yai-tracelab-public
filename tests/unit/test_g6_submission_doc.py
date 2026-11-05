"""报告"数据性质"横幅 + 单文件作品说明（Batch 4 的交付保险）。

目的：初赛评审主要用国内头部大模型，最怕把"公开训练集上的拟合值"读成"泛化能力"。
因此：① 五份报告（综合 + 官方模板 + 业务/研发/产品）头部都带数据性质声明；
② 仓库与提交包里都有一份**单文件**作品说明，平台若只收一个文件也能交。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.builders import basic_flow, context, make_case, make_run, reference
from tracelab.adapters import load_dataset
from tracelab.evaluation import evaluate_dataset
from tracelab.official_report import write_official_report
from tracelab.reports import write_audience_reports, write_markdown

BUNDLE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_submission_bundle.py"
MIRROR_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_public_mirror.py"
DOC = Path(__file__).resolve().parents[2] / "docs" / "作品说明.md"


@pytest.fixture(scope="module")
def bundle_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bundle_script_batch4", BUNDLE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mirror_script() -> ModuleType | None:
    """镜像生成器本身不随公开镜像发布；不存在时返回 None（由测试改为断言文档已发布）。"""
    if not MIRROR_SCRIPT.is_file():
        return None
    spec = importlib.util.spec_from_file_location("mirror_script_batch4", MIRROR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dataset(tmp_path: Path):
    """一个最小 A/B 成对数据集（自造），只要够渲染报告即可。"""
    events = basic_flow(prefix="DOC")
    base = make_run("DOC-BASE", events, final_state="SUCCEEDED", invoice_created=True,
                    agent_version="baseline-v1")
    cand = make_run("DOC-CAND", events, final_state="SUCCEEDED", invoice_created=True,
                    agent_version="candidate-v2")
    case = make_case("DOC-CASE", context=context(), reference_result=reference(),
                     runs=[base, cand])
    raw = dict(case.raw)
    raw["runs"] = []
    for run in (base, cand):
        run_raw = dict(run.raw)
        run_raw["events"] = [event.raw for event in run.events]
        raw["runs"].append(run_raw)
    path = tmp_path / "cases.jsonl"
    path.write_text(__import__("json").dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
    return load_dataset(path)


def test_official_report_has_data_nature_banner(tmp_path):
    dataset = _dataset(tmp_path)
    result = evaluate_dataset(dataset)
    path = write_official_report(result, tmp_path / "report-official.md")
    text = path.read_text(encoding="utf-8")
    assert "数据性质" in text
    assert "拟合值" in text
    assert "不代表隐藏集泛化能力" in text


def test_all_reports_have_data_nature_banner(tmp_path):
    dataset = _dataset(tmp_path)
    result = evaluate_dataset(dataset)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = write_audience_reports(result, out_dir)
    paths["report"] = write_markdown(result, out_dir / "report.md")
    for name, path in paths.items():
        text = path.read_text(encoding="utf-8")
        assert "数据性质" in text, name
        assert "拟合值" in text, name


def test_single_file_submission_doc_exists_and_covers_deliverables():
    text = DOC.read_text(encoding="utf-8")
    for keyword in ("作品说明", "数据性质", "交付物", "评分口径", "凭什么可信",
                    "怎么复现", "边界", "不需要资料包"):
        assert keyword in text, keyword
    # 单文件必须自带"怎么跑"，不能只指向别的文件
    assert "python -m tracelab evaluate" in text
    assert "python -m tracelab experiments" in text


def test_bundle_and_mirror_whitelists_include_single_file_doc(bundle_script, mirror_script):
    assert "docs/作品说明.md" in bundle_script.ALLOW_FILES
    if mirror_script is not None:
        assert "docs/作品说明.md" in mirror_script.ALLOW_DIRS
    else:  # pragma: no cover - 仅公开镜像会走到
        # 公开镜像里没有生成器，就直接断言"文档确实被发布了"（更强的等价检查）
        assert DOC.is_file(), "公开镜像内必须真的发布 docs/作品说明.md"


def test_submission_note_mentions_single_file_doc(bundle_script, tmp_path):
    bundle = tmp_path / "submission"
    bundle.mkdir(parents=True, exist_ok=True)
    bundle_script._write_submission_note(bundle, include_official=False, include_regression=False)
    text = (bundle / "提交说明.md").read_text(encoding="utf-8")
    assert "docs/作品说明.md" in text
    assert "只允许上传一个文件" in text
