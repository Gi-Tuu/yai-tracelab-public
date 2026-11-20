"""public-safe allowlist manifest 结构测试（不依赖 mirror builder）。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_public_allowlist_manifest_is_safe_and_consistent():
    path = ROOT / "scripts" / "public_mirror_allowlist.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "tracelab-public-mirror-allowlist/1"
    assert manifest["generator_version"] == "1.2.0"
    files = manifest["files"]
    dirs = manifest["dirs"]
    assert len(files) == len(set(files))
    assert len(dirs) == len(set(dirs))
    assert "tracelab" in dirs
    assert "scripts" in dirs
    assert "data/official" not in dirs
    assert not any("official" in item for item in files + dirs)
    assert "scripts/final_round_console_page.html" not in files  # 由 scripts 目录覆盖
