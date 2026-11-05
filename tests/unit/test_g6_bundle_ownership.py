"""G6-0B：构建标记里的「上一次产出文件清单」与重建拒绝语义。

规则（与实现一致）：
1. 标记有效 → 身份核对通过 → 文件清单核对通过 → 才允许清空重建；
2. 目录里出现清单以外的文件 → **保持目录原样**（拒绝重建）；
3. 记录的文件被改过/被删掉 → **停止本次重建**；
4. 旧版本标记（没有文件清单）→ 拒绝重建（无法证明内容归属）。

全部使用临时目录里的最小项目，不碰真仓库与官方资料包。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_submission_bundle.py"


@pytest.fixture(scope="module")
def bundle_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bundle_script_g6", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mini_project(tmp_path: Path) -> Path:
    """刚好满足白名单 + 必需文档的最小项目（不含任何官方资料）。"""
    root = tmp_path / "proj"
    _write(root / "README.md", "# mini\n")
    _write(root / "LICENSE", "MIT\n")
    _write(root / "NOTICE", "notice\n")
    _write(root / "pyproject.toml", "[project]\nname='mini'\n")
    _write(root / ".gitignore", "out/\n")
    _write(root / ".gitattributes", "* text=auto\n")
    _write(root / "docs" / "method.md", "# method\n")
    _write(root / "docs" / "architecture.md", "# arch\n")
    _write(root / "tracelab" / "__init__.py", "")
    _write(root / "tracelab" / "demo.py", "VALUE = 1\n")
    _write(root / "tests" / "test_smoke.py", "def test_ok():\n    assert True\n")
    _write(root / "scripts" / "ci_verify.py", "")
    return root


def _build(module: ModuleType, root: Path, out: Path) -> int:
    options = module.BundleOptions(root=root, out_root=out, allow_outside=True, skip_checks=True)
    return int(module.build_bundle(options))


def _marker(out: Path, module: ModuleType) -> dict[str, object]:
    payload = json.loads((out / module.MARKER_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_marker_records_generated_files(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """构建成功后，标记里必须有本次产出的完整文件清单（路径/大小/哈希）。"""
    out = tmp_path / "dist-files"
    assert _build(bundle_script, mini_project, out) == 0

    payload = _marker(out, bundle_script)
    files = payload["files"]
    assert isinstance(files, list) and files, "标记里必须记录文件清单"
    recorded = {item["path"] for item in files}  # type: ignore[index]
    assert any(path.startswith("submission/") for path in recorded)
    assert any(path.endswith(".zip") for path in recorded)
    assert bundle_script.MARKER_FILENAME not in recorded, "标记自身不进清单"

    # 清单与实际内容逐项一致（这是下次重建的判据）
    actual = {
        path.relative_to(out).as_posix(): bundle_script._file_sha256(path)
        for path in out.rglob("*")
        if path.is_file() and path.name != bundle_script.MARKER_FILENAME
    }
    assert recorded == set(actual)
    for item in files:  # type: ignore[union-attr]
        assert actual[item["path"]] == item["sha256"]
        assert (out / item["path"]).stat().st_size == item["size"]


def test_clean_marked_dir_can_be_rebuilt(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """内容与清单一致的目录：允许清空重建（正常重复构建不受影响）。"""
    out = tmp_path / "dist-rebuild"
    assert _build(bundle_script, mini_project, out) == 0
    before = _marker(out, bundle_script)["files"]
    assert _build(bundle_script, mini_project, out) == 0
    after = _marker(out, bundle_script)["files"]
    assert isinstance(before, list) and isinstance(after, list)
    assert len(after) == len(before)


def test_extra_file_keeps_directory_untouched(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """清单以外出现文件 → 拒绝重建，且一个字节都不改。"""
    out = tmp_path / "dist-extra"
    assert _build(bundle_script, mini_project, out) == 0
    stray = out / "user-notes.txt"
    _write(stray, "do not delete\n")
    snapshot = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)

    message = str(excinfo.value)
    assert "清单以外的文件" in message
    assert stray.is_file() and stray.read_text(encoding="utf-8") == "do not delete\n"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == snapshot


def test_modified_recorded_file_stops_rebuild(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """记录过的文件被改动 → 停止重建（不要用"删掉重来"掩盖内容被动过的事实）。"""
    out = tmp_path / "dist-modified"
    assert _build(bundle_script, mini_project, out) == 0
    target = out / "submission" / "README.md"
    assert target.is_file()
    target.write_text("# tampered\n", encoding="utf-8")

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)
    assert "内容已变化" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "# tampered\n", "拒绝时不得改动目录"


def test_deleted_recorded_file_stops_rebuild(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """记录过的文件被删除 → 停止重建。"""
    out = tmp_path / "dist-deleted"
    assert _build(bundle_script, mini_project, out) == 0
    (out / "submission" / "pyproject.toml").unlink()

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)
    assert "已不存在" in str(excinfo.value)


def test_legacy_marker_without_file_list_is_refused(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """旧版本标记（没有文件清单）无法证明内容归属 → 拒绝重建。"""
    out = tmp_path / "dist-legacy"
    assert _build(bundle_script, mini_project, out) == 0
    marker_path = out / bundle_script.MARKER_FILENAME
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload.pop("files", None)
    marker_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)
    assert "没有文件清单" in str(excinfo.value)


def test_marker_identity_still_wins_over_content(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """身份不匹配优先于内容核对：别的项目的目录即便文件齐全也不能清。"""
    out = tmp_path / "dist-identity"
    assert _build(bundle_script, mini_project, out) == 0
    marker_path = out / bundle_script.MARKER_FILENAME
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload["project"] = "someone-else"
    marker_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)
    assert "项目身份不匹配" in str(excinfo.value)
