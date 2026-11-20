"""打开即用体验页的回归测试（评审「可体验性」）。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_demo(tmp_path: Path, name: str) -> Path:
    from tracelab.cli import main

    out = tmp_path / name
    html = out / "demo.html"
    code = main(["demo", "--no-open", "--out", str(out), "--html", str(html)])
    assert code == 0
    return html


def test_demo_page_is_deterministic_and_contains_evidence(tmp_path):
    first = _run_demo(tmp_path, "a").read_bytes()
    second = _run_demo(tmp_path, "b").read_bytes()
    assert first == second, "体验页必须确定性输出（不含时间戳/耗时）"
    text = first.decode("utf-8")
    assert "打开即用体验页" in text
    assert "SAMPLE-01" in text
    assert "证据链示例" in text
    assert "首因事件" in text
    assert "E:\\" not in text and "C:\\" not in text
    assert not re.search(r"20\d\d-\d\d-\d\d", text), "体验页不得含日期（归档可复现）"


def test_committed_demo_page_is_up_to_date(tmp_path):
    generated = _run_demo(tmp_path, "check").read_bytes()
    committed = (ROOT / "docs" / "demo.html").read_bytes()
    # git 统一 LF；Windows 工作区可能按平台转换，比较前统一换行
    def _norm(data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n")

    assert _norm(committed) == _norm(generated), (
        "docs/demo.html 过期：运行 python scripts/build_demo_page.py 重新生成"
    )
