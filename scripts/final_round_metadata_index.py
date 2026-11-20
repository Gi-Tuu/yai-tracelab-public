"""汇总所有收据批次的 metadata-status 侧车，生成 metadata-index.json。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round.metadata import build_index  # noqa: E402,F401


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总收据 metadata 状态")
    parser.add_argument("--root", required=True, help="私有复赛目录")
    parser.add_argument("--out", required=True, help="输出 metadata-index.json")
    args = parser.parse_args()
    index = build_index(Path(args.root))
    Path(args.out).write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"批次：{index['batches']}，历史元数据不可靠：{index['historical_metadata_unreliable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
