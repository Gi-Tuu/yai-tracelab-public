"""synthetic soak 脚本测试（小规模、不调用模型）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "final_round_soak", ROOT / "scripts" / "final_round_soak.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_synthetic_soak_resume_and_order(tmp_path: Path):
    module = _load()
    report = module.run_synthetic(10, 2, tmp_path)
    assert report["runs"] == 10
    assert report["receipts"] == 10
    assert report["resume_calls"] == 0
    assert report["issues"] == []
