"""把 `docs/作品说明.md` 导出为 DOCX / PDF（本地打包用，不是评测器运行时依赖）。

为什么单独一个脚本：评测器本身要求"零第三方运行时依赖"，而导出 Word/PDF 需要
`python-docx` 与 LibreOffice。因此把它放在 scripts 里，按需运行、缺依赖就跳过。

用法：
    python scripts/export_submission_doc.py --out out/submission-doc
    python scripts/export_submission_doc.py --doc docs/作品说明.md --out out/submission-doc --no-pdf

退出码：0 成功（含"跳过了 PDF"）；2 缺少必需依赖；1 输入不存在。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOC = ROOT / "docs" / "作品说明.md"


def _find_soffice() -> str | None:
    for candidate in (
        shutil.which("soffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _add_inline(paragraph, text: str) -> None:
    """把 `**粗体**` 与 `` `代码` `` 解析成 run（其余按普通文本）。"""
    import re

    tokens = re.split(r"(\*\*.+?\*\*|`[^`]+`)", text)
    for token in tokens:
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            run = paragraph.add_run(token[2:-2])
            run.bold = True
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            run.font.name = "Consolas"
        else:
            paragraph.add_run(token.replace("`", ""))


def _add_markdown(document, text: str) -> None:
    """极简 Markdown → docx：标题 / 引用 / 表格 / 列表 / 代码块 / 普通段落。"""
    from docx.shared import Pt

    lines = text.splitlines()
    index = 0
    in_code = False
    while index < len(lines):
        line = lines[index].rstrip()
        if line.startswith("```"):
            in_code = not in_code
            index += 1
            continue
        if in_code:
            paragraph = document.add_paragraph()
            run = paragraph.add_run(line)
            run.font.name = "Consolas"
            run.font.size = Pt(9)
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            document.add_heading(line.lstrip("#").strip().replace("`", ""), level=min(level, 4))
            index += 1
            continue
        if line.startswith(">"):
            # 连续的引用行合并成一段（避免被拆成多个短段）
            block: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                block.append(lines[index].lstrip()[1:].strip())
                index += 1
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Pt(12)
            for offset, chunk in enumerate(block):
                if offset:
                    paragraph.add_run("\n")
                _add_inline(paragraph, chunk)
            for run in paragraph.runs:
                run.italic = True
            continue
        if line.startswith("|"):
            block = []
            while index < len(lines) and lines[index].startswith("|"):
                block.append(lines[index].strip())
                index += 1
            rows = [
                [cell.strip() for cell in row.strip("|").split("|")]
                for row in block
                if not set(row.replace("|", "").strip()) <= set("-: ")
            ]
            if rows:
                table = document.add_table(rows=len(rows), cols=len(rows[0]))
                table.style = "Table Grid"
                for row_index, row in enumerate(rows):
                    for cell_index, value in enumerate(row[: len(rows[0])]):
                        paragraph = table.cell(row_index, cell_index).paragraphs[0]
                        _add_inline(paragraph, value)
            continue
        if line.lstrip().startswith(("- ", "* ")):
            paragraph = document.add_paragraph(style="List Bullet")
            _add_inline(paragraph, line.lstrip()[2:].strip())
            index += 1
            continue
        if line[:1].isdigit() and ". " in line:
            paragraph = document.add_paragraph(style="List Number")
            _add_inline(paragraph, line.split(". ", 1)[1].strip())
            index += 1
            continue
        paragraph = document.add_paragraph()
        _add_inline(paragraph, line.strip())
        index += 1


def export_docx(source: Path, target: Path) -> Path:
    from docx import Document

    document = Document()
    _add_markdown(document, source.read_text(encoding="utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(target))
    return target


def export_pdf(docx_path: Path, out_dir: Path) -> Path | None:
    soffice = _find_soffice()
    if soffice is None:
        return None
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
        capture_output=True, text=True, check=False, timeout=180,
    )
    pdf_path = out_dir / (docx_path.stem + ".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        print(f"警告：PDF 转换失败（{result.stderr.strip()[:200]}）", file=sys.stderr)
        return None
    return pdf_path


def _guard_out_dir(out: Path) -> str | None:
    """输出目录守卫（审计 R4-08）。

    旧脚本无校验，按 docstring 示例 `--out dist` 会往提交包目录里塞文件，
    下一次 `build_submission_bundle` 直接拒绝重建（模板清单外的文件）。
    """
    import os

    resolved = Path(os.path.realpath(str(out)))
    root = Path(os.path.realpath(str(ROOT)))
    dist = Path(os.path.realpath(str(ROOT / "dist")))
    if resolved == dist or resolved.is_relative_to(dist):
        return "不能写进提交包目录 dist/（会让下一次打包因'清单外文件'被拒绝）"
    if (resolved / ".tracelab-bundle.json").exists():
        return f"目标目录里有提交包构建标记（{resolved}），拒绝写入"
    if resolved == root:
        return "输出目录不能是项目根目录"
    if resolved.parent == resolved:
        return "输出目录不能是文件系统根"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_submission_doc",
                                     description="导出单文件《作品说明》（DOCX / PDF）")
    parser.add_argument("--doc", default=str(DEFAULT_DOC), help="Markdown 源文件")
    parser.add_argument("--out", default=str(ROOT / "out" / "submission-doc"),
                        help="输出目录（默认 out/submission-doc，刻意不放进提交包目录："
                             "提交包有文件清单归属校验，外部塞文件会让下次重建被拒绝）")
    parser.add_argument("--no-pdf", action="store_true", help="只导出 DOCX")
    args = parser.parse_args(argv)

    source = Path(args.doc)
    if not source.is_file():
        print(f"错误：找不到 {source}", file=sys.stderr)
        return 1
    try:
        import docx  # noqa: F401
    except ImportError:
        print("错误：缺少 python-docx，无法导出 DOCX（评测器本身不需要它）", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        # 相对路径统一按项目根解析（与 make_sample_dataset 一致，不受 CWD 影响）
        out_dir = ROOT / out_dir
    guard_error = _guard_out_dir(out_dir)
    if guard_error:
        print(f"错误：{guard_error}；未写入任何文件。", file=sys.stderr)
        return 2
    docx_path = export_docx(source, out_dir / (source.stem + ".docx"))
    print(f"DOCX：{docx_path}")
    if args.no_pdf:
        return 0
    pdf_path = export_pdf(docx_path, out_dir)
    if pdf_path is None:
        print("提示：未找到 LibreOffice，已跳过 PDF（DOCX 仍可用）", file=sys.stderr)
        return 0
    print(f"PDF：{pdf_path}")
    print(f"PDF 大小：{pdf_path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
