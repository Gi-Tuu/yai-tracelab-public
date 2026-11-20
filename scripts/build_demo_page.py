"""生成静态体验页 `docs/demo.html`（提交包 / 公开镜像用，确定性输出）。

用法：``python scripts/build_demo_page.py``（需要仓库自带 data/sample）。
页面不含时间戳、耗时与绝对路径，便于归档与测试比对。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.cli import main as cli_main  # noqa: E402

OUT = ROOT / "docs" / "demo.html"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tracelab-demo-") as tmp:
        code = cli_main([
            "demo", "--cases", "data/sample/sample-traces.jsonl",
            "--out", str(Path(tmp) / "out"), "--html", str(OUT), "--no-open",
        ])
    if code != 0:
        print(f"失败：demo 退出码 {code}", file=sys.stderr)
        return code
    print(f"已生成：{OUT}（{OUT.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
