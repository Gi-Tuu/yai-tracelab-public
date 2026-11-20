"""收据 metadata 侧车汇总（脚本与 bundle 构建共用）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_index(root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for status_file in sorted(root.rglob("metadata-status.json")):
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries.append(
            {
                "batch": status_file.parent.relative_to(root).as_posix(),
                "receipts": data.get("receipts", 0),
                "valid": bool(data.get("valid")),
                "metadata_status": data.get("metadata_status", {}),
                "historical_metadata_unreliable": bool(data.get("historical_metadata_unreliable")),
                "schema_versions": data.get("schema_versions", []),
            }
        )
    return {
        "schema": "tracelab-receipt-metadata-index/1",
        "batches": len(entries),
        "historical_metadata_unreliable": any(item["historical_metadata_unreliable"] for item in entries),
        "entries": entries,
    }
