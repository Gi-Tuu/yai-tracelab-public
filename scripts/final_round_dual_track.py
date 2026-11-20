"""生成双轨结果：初赛原格式（开票）+ 扩展结果（两类业务）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from tracelab.final_round.legacy import link_extended, to_legacy_case, validate_dual_track  # noqa: E402


def read_jsonl(path: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成复赛双轨结果")
    parser.add_argument("--results", required=True, help="扩展结果 results.jsonl")
    parser.add_argument("--out", default="out/final-round-dual", help="输出目录")
    parser.add_argument("--legacy-submission", default=None, help="已有的初赛原格式 submission.jsonl（开票）")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = read_jsonl(args.results)
    legacy_rows: list[dict] = []
    legacy_ids: set[str] = set()
    if args.legacy_submission:
        legacy_rows = read_jsonl(args.legacy_submission)
        legacy_ids = {str(row.get("case_id")) for row in legacy_rows}
    extended_rows: list[dict] = []
    skipped: list[dict] = []
    for result in results:
        business_type = result.get("business_type")
        if business_type == "invoice":
            if legacy_ids:
                if result["case_id"] in legacy_ids:
                    extended_rows.append(
                        link_extended(result, f"legacy_submission.jsonl#{result['case_id']}")
                    )
                else:
                    skipped.append({"case_id": result["case_id"], "reason": "初赛原格式文件缺少该 Case"})
                    extended_rows.append(link_extended(result, None, note="legacy 缺少该 Case"))
                continue
            legacy = to_legacy_case(result)
            if legacy is None:
                skipped.append(
                    {
                        "case_id": result["case_id"],
                        "reason": "缺少初赛原格式质量分/扣分来源，或布尔不完整；未生成 legacy",
                    }
                )
                extended_rows.append(
                    link_extended(result, None, note="legacy 跳过：缺少原格式依据")
                )
            else:
                legacy_rows.append(legacy)
                extended_rows.append(
                    link_extended(result, f"legacy_submission.jsonl#{result['case_id']}")
                )
        else:
            extended_rows.append(
                link_extended(result, None, note="legacy_not_applicable：初赛原格式为开票专用")
            )

    (out / "legacy_submission.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in legacy_rows) + "\n",
        encoding="utf-8",
    )
    checks = validate_dual_track(legacy_rows, extended_rows)
    (out / "extended_results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in extended_rows) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": "tracelab-final-round-dual-track/1",
        "cases": len(results),
        "legacy_cases": len(legacy_rows),
        "extended_cases": len(extended_rows),
        "skipped_legacy": skipped,
        "legacy_file": "legacy_submission.jsonl",
        "extended_file": "extended_results.jsonl",
        "legacy_source": args.legacy_submission or "mapped_from_extended",
        "checks": checks,
        "note": "初赛原格式仅适用于 invoice；ai_consultant 只交扩展结果（legacy_not_applicable）。",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"输出：{out}")
    print(f"Case {len(results)}；legacy {len(legacy_rows)}；extended {len(extended_rows)}")
    if skipped:
        print(f"跳过 legacy {len(skipped)} 条（缺少原格式依据）")
    if checks["duplicate_legacy_ids"] or checks["duplicate_extended_ids"] or checks["broken_legacy_ref"]:
        print(f"双轨核对失败：{checks}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
