"""收据链兼容校验器：自动识别 v0 / v1-extended / v2 并分别验证。

用法：
    python scripts/verify_receipts.py --receipts <receipts.json> [--json] [--write-status]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round.receipts import JudgeReceipt, ReceiptChain  # noqa: E402


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    receipts = [JudgeReceipt.from_dict(row) for row in rows]
    problems = ReceiptChain(receipts).verify()
    statuses = Counter(receipt.metadata_status for receipt in receipts)
    return {
        "schema": "tracelab-receipt-verify/1",
        "receipts": len(receipts),
        "valid": not problems,
        "problems": problems,
        "metadata_status": dict(statuses),
        "historical_metadata_unreliable": any(
            status != "reliable_v2" for status in statuses
        ),
        "schema_versions": sorted({receipt.schema_version or "v0_or_v1ext" for receipt in receipts}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="收据链兼容校验")
    parser.add_argument("--receipts", required=True, help="receipts.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--write-status",
        action="store_true",
        help="在收据目录写入 metadata-status.json（旧批次标记）",
    )
    args = parser.parse_args()
    path = Path(args.receipts)
    rows = json.loads(path.read_text(encoding="utf-8"))
    report = build_report(rows)
    if args.write_status:
        target = path.parent / "metadata-status.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"状态文件：{target}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"收据数：{report['receipts']}")
        print(f"链校验：{'通过' if report['valid'] else '失败'}")
        print(f"元数据：{report['metadata_status']}")
        if report["problems"]:
            for problem in report["problems"][:10]:
                print(f" - {problem}", file=sys.stderr)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
