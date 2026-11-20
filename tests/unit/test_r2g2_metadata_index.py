"""metadata-index 汇总测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "final_round_metadata_index",
        ROOT / "scripts" / "final_round_metadata_index.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_metadata_index_aggregates_sidecars(tmp_path: Path):
    module = _load()
    one = tmp_path / "batch-old"
    one.mkdir()
    (one / "metadata-status.json").write_text(
        json.dumps(
            {
                "receipts": 200,
                "valid": True,
                "metadata_status": {"legacy_v0_unreliable": 200},
                "historical_metadata_unreliable": True,
                "schema_versions": ["v0_or_v1ext"],
            }
        ),
        encoding="utf-8",
    )
    two = tmp_path / "batch-v2"
    two.mkdir()
    (two / "metadata-status.json").write_text(
        json.dumps(
            {
                "receipts": 6,
                "valid": True,
                "metadata_status": {"reliable_v2": 6},
                "historical_metadata_unreliable": False,
                "schema_versions": ["2"],
            }
        ),
        encoding="utf-8",
    )
    index = module.build_index(tmp_path)
    assert index["batches"] == 2
    assert index["historical_metadata_unreliable"] is True
    assert {item["batch"] for item in index["entries"]} == {"batch-old", "batch-v2"}
