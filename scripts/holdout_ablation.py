"""把 holdout 盲标转换为官方 ablate 标签格式，并跑对照消融。

用法：
    python scripts/holdout_ablation.py --out out/holdout-ablation
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def convert_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in rows:
        case_id = str(row["case_id"])
        grouped.setdefault(
            case_id,
            {
                "case_id": case_id,
                "run_assessments": [],
                "version_direction_on_this_case": "",
                "single_case_caveat": "holdout",
            },
        )
        grouped[case_id]["run_assessments"].append(
            {
                "run_id": row["run_id"],
                "agent_task_success": row.get("agent_task_success"),
                "business_invoice_created": row.get("business_invoice_created"),
                "root_causes": row.get("root_causes") or [],
            }
        )
    return list(grouped.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="holdout 对照消融")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--labels",
        default=str(ROOT / "docs" / "holdout" / "labels.jsonl"),
    )
    parser.add_argument(
        "--cases",
        default=str(ROOT / "data" / "official" / "student-1.0" / "04_无标注验证轨迹.jsonl"),
    )
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in Path(args.labels).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    labels = convert_labels(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False) as handle:
        for row in labels:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        temp_path = Path(handle.name)
    try:
        command = [
            sys.executable,
            "-m",
            "tracelab",
            "ablate",
            "--cases",
            args.cases,
            "--labels",
            str(temp_path),
            "--out",
            str(out),
        ]
        result = subprocess.run(command, cwd=ROOT, check=False)
    finally:
        temp_path.unlink(missing_ok=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
