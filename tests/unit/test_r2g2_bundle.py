"""R2-G2 G2-E：提交包构建与敏感标记拒绝测试。"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_final_round_bundle", ROOT / "scripts" / "build_final_round_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _private(tmp_path: Path, marker: str = "") -> Path:
    root = tmp_path / "private"
    root.mkdir()
    (root / "freeze-public-v1.json").write_text('{"model":"qwen3.8-max"}\n', encoding="utf-8")
    report = root / "report-ai-public100"
    report.mkdir()
    (report / "report.md").write_text("# report " + marker + "\n", encoding="utf-8")
    (report / "metrics.json").write_text("{}\n", encoding="utf-8")
    return root


def test_bundle_contains_code_docs_and_manifest(tmp_path: Path):
    module = _load()
    zip_path = module.build(_private(tmp_path), tmp_path / "dist", "bundle")
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "MANIFEST.json" in names
        assert "README.md" in names
        assert "tracelab/final_round/judge_input.py" in names
        if (ROOT / "scripts/final_round_console_page.html").is_file():
            assert "scripts/final_round_console_page.html" in names
        if (ROOT / "docs/final-round-report.md").is_file():
            assert "docs/final-round-report.md" in names
        assert "results/freeze-public-v1.json" in names
        assert "results/metadata-index.json" in names
        manifest = json.loads(zf.read("MANIFEST.json"))
        assert manifest["schema"] == "tracelab-final-round-bundle/1"
        assert manifest["files"] > 0


def test_bundle_rejects_secret_marker(tmp_path: Path):
    module = _load()
    with pytest.raises(SystemExit):
        module.build(_private(tmp_path, "api." + "suanli.cn"), tmp_path / "dist", "bundle")
