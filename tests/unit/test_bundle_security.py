"""提交包「路径安全 + 白名单打包」专项测试（P0）。

设计原则：
- 拒绝逻辑的测试**全部在临时目录里搭 mini 项目**，不拿真仓库当试验材料
  （否则测试本身就成了事故源）；mini 项目通过模块内部 API 驱动完整流程。
- 唯一的真仓库用例是末尾"正常 --out dist 仍能成功构建"，输出写进 tmp_path。
- 敏感串一律分片拼接：本文件会被打进提交包并被回读扫描，连写会自我命中。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_submission_bundle.py"

# --- 假敏感串（分片拼接，避免「包内安全扫描」扫到本文件自己）----------------
FAKE_GITHUB_TOKEN = "gh" + "p" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
FAKE_AWS_KEY = "AKIA" + "Q" * 16
FAKE_PEM_HEAD = "-----BEGIN RSA " + "PRIVATE KEY-----"
FAKE_PWD_VALUE = "pass" + "word=" + "hunter2"
FAKE_OPENAI_KEY = "sk" + "-" + "abcdef0123456789abcdef"
#: 新增供应商格式（同样分片：源码文本里不出现连续前缀）
FAKE_ANTHROPIC_KEY = "sk" + "-" + "ant" + "-" + "api03-" + "A" * 24
FAKE_GITLAB_TOKEN = "gl" + "pat" + "-" + "B" * 24
FAKE_GOOGLE_KEY = "AI" + "za" + "C" * 35
FAKE_SLACK_TOKEN = "xo" + "xb" + "-" + "1234567890ab"
FAKE_STRIPE_KEY = "sk" + "_" + "live" + "_" + "D" * 24
FAKE_HF_TOKEN = "hf" + "_" + "E" * 32
FAKE_JWT = "e" + "yJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
FAKE_BEARER = "Bea" + "rer " + "F" * 24
#: 2026-11-04 新增格式（同样分片：源码里不出现连续前缀/赋值形态）
FAKE_GITHUB_USER_TOKEN = "gh" + "u" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
FAKE_GITHUB_SIGNING_TOKEN = "gh" + "s" + "_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"
FAKE_OPENAI_PROJ_KEY = "sk" + "-" + "proj" + "-" + "B" * 24
FAKE_DB_CRED = "db_" + "password" + ":" + "hunter2"
FAKE_PWD_ASSIGN = "PW" + "D" + "=" + "hunter2"

def requires_official_data(fn):
    """同时打上 marker（供公开 CI 用 -m 排除）与 skipif（无数据时直接跳过）。"""
    fn = pytest.mark.requires_official(fn)
    return pytest.mark.skipif(
        not (ROOT / "data" / "official" / "student-1.0").exists(),
        reason="赛事资料包不在工作区（公开镜像不含官方数据）",
    )(fn)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_submission_bundle_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle_script() -> ModuleType:
    return _load_module()


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """目录快照（相对路径 → 大小 + 内容），用于断言"什么都没被改动"。"""
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _make_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        return False
    return True


@pytest.fixture
def mini_project(tmp_path: Path) -> Path:
    """一个"看起来像本仓库"的最小项目，含白名单内外各类文件。"""
    root = tmp_path / "proj"
    # 白名单内（应当进包）
    _write(root / "README.md", "# mini\n")
    _write(root / "LICENSE", "MIT\n")
    _write(root / "NOTICE", "notice\n")
    _write(root / "pyproject.toml", "[project]\nname = 'mini'\n")
    _write(root / ".gitignore", "dist/\n")
    _write(root / ".gitattributes", "* text=auto\n")
    _write(root / "docs" / "method.md", "# method\n")
    _write(root / "docs" / "architecture.md", "# architecture\n")
    _write(root / "docs" / "casebook.md", "# 不在白名单，必须被丢掉\n")
    _write(root / "tracelab" / "__init__.py", "")
    _write(root / "tracelab" / "cli.py", "def main() -> int:\n    return 0\n")
    _write(root / "tests" / "test_smoke.py", "def test_ok() -> None:\n    assert True\n")
    _write(root / "scripts" / "ci_verify.py", "")
    _write(root / "data" / "demo" / "generic-traces.jsonl", "{}\n")
    _write(root / "regression" / "invoice-pack" / "regression-pack.json", "{}\n")
    _write(root / ".github" / "workflows" / "ci.yml", "on: push\n")

    # 白名单目录里但必须被拒绝名单拦下的东西
    _write(root / "tracelab" / ".env", "SECRET=1\n")
    _write(root / "scripts" / ".env.production", "SECRET=1\n")
    _write(root / "tracelab" / "credentials.json", "{}\n")
    _write(root / "tests" / "server.pem", "x\n")
    _write(root / "regression" / "invoice-pack" / "signing.key", "x\n")
    _write(root / "data" / "demo" / "api-token.json", "{}\n")
    _write(root / "tracelab" / "token_store.py", "X = 1\n")
    _write(root / "tracelab" / "__pycache__" / "cli.cpython-311.pyc", "x\n")

    # 赛事资料包（默认不打包）
    _write(root / "data" / "official" / "student-1.0" / "02_公开训练轨迹.jsonl", "{}\n")
    return root


def _build(module: ModuleType, root: Path, out: Path, **kwargs: object) -> int:
    options = module.BundleOptions(root=root, out_root=out, allow_outside=True, skip_checks=True, **kwargs)
    return int(module.build_bundle(options))


def _zip_names(out: Path) -> list[str]:
    zips = list(out.glob("*.zip"))
    assert len(zips) == 1, f"应当正好一个 zip，实际：{zips}"
    with zipfile.ZipFile(zips[0]) as archive:
        return archive.namelist()


# ---------------------------------------------------------------------------
# 输出路径安全：拒绝，且不改动任何文件
# ---------------------------------------------------------------------------


def test_out_dot_is_rejected_and_project_untouched(bundle_script: ModuleType, mini_project: Path) -> None:
    before = _snapshot(mini_project)
    options = bundle_script.BundleOptions(
        root=mini_project, out_root=Path("."), allow_outside=True, skip_checks=True
    )
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.build_bundle(options)
    assert "项目根目录本身" in str(excinfo.value)
    assert _snapshot(mini_project) == before, "--out . 被拒绝时不允许改动项目目录"


def test_cli_out_dot_returns_nonzero_without_touching_project(
    bundle_script: ModuleType, mini_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 层同样拦截：脚本把 ROOT 指到 mini 项目上，跑真实的 main()。"""
    before = _snapshot(mini_project)
    monkeypatch.setattr(bundle_script, "ROOT", mini_project)
    code = bundle_script.main(["--out", ".", "--allow-outside"])
    assert code == 2
    assert "安全检查未通过" in capsys.readouterr().err
    assert _snapshot(mini_project) == before


def test_out_parent_is_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.validate_output_dir(Path(".."), mini_project, allow_outside=True)
    assert "祖先" in str(excinfo.value)


def test_out_project_root_is_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    for candidate in (mini_project, Path(mini_project)):
        with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
            bundle_script.validate_output_dir(candidate, mini_project)
        assert "项目根目录本身" in str(excinfo.value)


def test_outside_existing_dir_needs_explicit_flag(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-exists"
    _write(outside / "important.txt", "别删我\n")

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.validate_output_dir(outside, mini_project)
    assert "不在默认允许范围" in str(excinfo.value)
    assert (outside / "important.txt").exists()


def test_allow_outside_still_refuses_foreign_directory(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """--allow-outside 不是"任意目录都能删"：没有有效构建标记的非空目录仍拒绝清空。"""
    outside = tmp_path / "someone-elses-dir"
    _write(outside / "thesis.docx", "重要文件\n")
    options = bundle_script.BundleOptions(
        root=mini_project, out_root=outside, allow_outside=True, skip_checks=True
    )
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.build_bundle(options)
    # 拒绝理由必须是"标记校验"，而不是"看起来像不像产出目录"——
    # 后者只凭目录形状判断，正是 P0 误删的根因。
    assert "构建标记" in str(excinfo.value)
    assert "不像本脚本" not in str(excinfo.value)
    assert (outside / "thesis.docx").exists()


def test_user_home_and_filesystem_root_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    home = Path.home()
    for candidate in (home, Path(home.anchor), Path(mini_project.anchor)):
        with pytest.raises(bundle_script.UnsafeOutputError):
            bundle_script.validate_output_dir(candidate, mini_project, allow_outside=True)


@pytest.mark.skipif(os.name != "nt", reason="大小写不敏感是 Windows 的路径语义")
def test_windows_case_variant_of_root_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.validate_output_dir(Path(str(mini_project).upper()), mini_project)
    assert "项目根目录本身" in str(excinfo.value)


def test_symlinked_out_dir_is_rejected(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-target"
    _write(outside / "keep.txt", "keep\n")
    link = tmp_path / "link-dir"
    link_to_project = mini_project / "dist-link"
    if not _make_symlink(link, outside) or not _make_symlink(link_to_project, outside):
        pytest.skip("当前环境不允许创建符号链接（Windows 需要开发者模式或管理员）")

    for candidate in (link / "dist", link, link_to_project):
        for allow in (False, True):
            with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
                bundle_script.validate_output_dir(candidate, mini_project, allow_outside=allow)
            assert "符号链接" in str(excinfo.value)
    assert (outside / "keep.txt").exists()


def test_default_dist_and_subdirs_are_allowed(bundle_script: ModuleType, mini_project: Path) -> None:
    allowed = bundle_script.validate_output_dir(Path("dist"), mini_project)
    assert allowed == mini_project / "dist"
    nested = bundle_script.validate_output_dir(mini_project / "dist" / "sub" / "pkg", mini_project)
    assert nested == mini_project / "dist" / "sub" / "pkg"


# ---------------------------------------------------------------------------
# 白名单 / 拒绝名单
# ---------------------------------------------------------------------------


def test_env_keys_tokens_never_enter_zip(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    out = tmp_path / "dist-out"
    assert _build(bundle_script, mini_project, out) == 0
    names = _zip_names(out)

    forbidden = (".env", ".pem", ".key", "token", "credential", ".log", ".coverage", "__pycache__", ".pyc")
    for name in names:
        low = name.lower()
        for token in forbidden:
            assert token not in low, f"{name} 不应进入提交包（命中 {token}）"

    assert any(name.endswith("tracelab/cli.py") for name in names)
    assert any(name.endswith("提交清单.md") for name in names)
    assert not any("data/official" in name for name in names), "赛事资料包默认不得打包"
    assert not any("docs/casebook.md" in name for name in names), "白名单外文档不得打包"
    assert not any(".github" in name for name in names), "白名单外目录不得打包"


def test_official_data_packaged_only_with_explicit_flag(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    default_out = tmp_path / "dist-default"
    assert _build(bundle_script, mini_project, default_out) == 0
    assert not any("data/official" in name for name in _zip_names(default_out))
    checklist = (default_out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "未打包（默认保守" in checklist

    official_out = tmp_path / "dist-official"
    assert _build(bundle_script, mini_project, official_out, include_official=True) == 0
    official_names = [name for name in _zip_names(official_out) if "data/official" in name]
    assert official_names, "--include-official-data 时应当打包明确列出的资料文件"
    assert all(name.endswith("02_公开训练轨迹.jsonl") for name in official_names)


def test_zip_bundle_content_blocks(bundle_script: ModuleType, mini_project: Path, tmp_path: Path, capsys) -> None:
    _write(
        mini_project / "tracelab" / "leaked_config.py",
        "\n".join(
            [
                'TOKEN = "' + FAKE_GITHUB_TOKEN + '"',
                'AWS = "' + FAKE_AWS_KEY + '"',
                'PW' + 'D = "' + FAKE_PWD_VALUE + '"',
                'KEY = "' + FAKE_OPENAI_KEY + '"',
                FAKE_PEM_HEAD,
                "",
            ]
        ),
    )
    out = tmp_path / "dist-leak"
    assert _build(bundle_script, mini_project, out) == 3
    assert not list(out.glob("*.zip")), "安全扫描未通过时不允许产出 zip"
    err = capsys.readouterr().err
    assert "安全扫描未通过" in err
    assert "leaked_config.py" in err

    # 清单里也要留痕（扫描结果写进「包内安全扫描」）
    checklist = (out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "## 包内安全扫描" in checklist
    assert "leaked_config.py" in checklist


def test_checklist_has_security_scan_section(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    out = tmp_path / "dist-clean"
    assert _build(bundle_script, mini_project, out) == 0
    checklist = (out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "## 包内安全扫描" in checklist
    assert "无命中" in checklist
    assert "赛事资料包说明" in checklist


def _make_junction(link: Path, target: Path) -> bool:
    """Windows 目录联接（Junction）：不需要管理员权限，用来验证重解析点检查。"""
    if os.name != "nt":
        return False
    if link.exists() and not link.is_symlink():
        shutil.rmtree(link, ignore_errors=True)
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0 and link.exists()


# ---------------------------------------------------------------------------
# P0：构建标记是唯一的"清空许可"（不能凭目录形状判断归属）
# ---------------------------------------------------------------------------


def test_empty_out_dir_builds_ok(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """空输出目录正常构建：空目录不需要任何标记就能用。"""
    out = tmp_path / "dist-empty"
    out.mkdir()
    assert _build(bundle_script, mini_project, out) == 0
    assert (out / bundle_script.MARKER_FILENAME).is_file()
    assert (out / "submission" / "提交清单.md").is_file()


def test_valid_marker_allows_rebuild(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """正常旧提交目录允许重建；但 G6-0B 起"目录里有清单外文件"属拒绝条件。

    契约变化（G6-0B）：标记现在记录上一次产出的**文件清单**。原样未动的目录可以清空重来；
    一旦出现清单以外的文件（或记录文件被改动/删除），一律保持目录原样、拒绝重建——
    宁可让人手工确认，也不替用户删掉来历不明的文件。
    """
    out = tmp_path / "dist-rebuild"
    assert _build(bundle_script, mini_project, out) == 0

    marker_path = out / bundle_script.MARKER_FILENAME
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["project"] == bundle_script.PROJECT_ID
    assert payload["generator"] == bundle_script.GENERATOR_NAME
    assert payload["generator_version"] == bundle_script.GENERATOR_VERSION
    # 两个身份指纹都得在：项目根 + 输出目录
    assert payload["source_root"] and payload["output_dir"]
    assert payload["source_root"] != payload["output_dir"]
    # G6-0B：清单必须记下来，且非空
    assert isinstance(payload["files"], list) and payload["files"]

    # ① 原样未动 → 允许清空重建
    assert _build(bundle_script, mini_project, out) == 0
    assert marker_path.is_file(), "重建后标记必须重新写回"

    # ② 有人往里放了清单外的文件 → 拒绝重建，且目录保持原样（不再"顺手清掉"）
    _write(out / "submission" / "stale-from-last-run.txt", "上一轮残留\n")
    _write(out / "stray-at-root.txt", "根目录残留\n")
    with pytest.raises(bundle_script.UnsafeOutputError):
        _build(bundle_script, mini_project, out)
    assert (out / "submission" / "stale-from-last-run.txt").exists()
    assert (out / "stray-at-root.txt").exists()


def test_bare_submission_dir_is_not_deletion_permission(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """任意 `submission/` 子目录都不能成为删除许可（P0 误删根因的回归用例）。"""
    outside = tmp_path / "old-looking-bundle"
    _write(outside / "submission" / "user-notes.txt", "用户自己的东西\n")
    options = bundle_script.BundleOptions(
        root=mini_project, out_root=outside, allow_outside=True, skip_checks=True
    )
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.build_bundle(options)
    assert "构建标记" in str(excinfo.value)
    assert (outside / "submission" / "user-notes.txt").exists(), "被拒绝时一个字节都不许动"


def test_submission_dir_with_personal_files_is_refused(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """`submission/` + 其他个人文件：同样只认标记，不认形状。"""
    outside = tmp_path / "mixed-user-dir"
    _write(outside / "submission" / "submission.jsonl", "{}\n")
    _write(outside / "thesis.docx", "重要文件\n")
    _write(outside / "photos" / "trip.jpg", "x\n")
    options = bundle_script.BundleOptions(
        root=mini_project, out_root=outside, allow_outside=True, skip_checks=True
    )
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.build_bundle(options)
    assert "构建标记" in str(excinfo.value)
    assert (outside / "thesis.docx").exists()
    assert (outside / "photos" / "trip.jpg").exists()


def test_foreign_dir_with_zip_still_needs_identity(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """外部目录即使已经有"像我们产出的" ZIP/清单，也必须验证身份。"""
    outside = tmp_path / "looks-like-our-output"
    _write(outside / "yai-tracelab-submission-20260101.zip", "PK\x03\x04 fake\n")
    _write(outside / "提交清单.md", "# 也许是别人的清单\n")
    _write(outside / "SHA256SUMS.txt", "deadbeef  某文件\n")
    options = bundle_script.BundleOptions(
        root=mini_project, out_root=outside, allow_outside=True, skip_checks=True
    )
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.build_bundle(options)
    assert "构建标记" in str(excinfo.value)
    assert (outside / "yai-tracelab-submission-20260101.zip").exists()


@pytest.mark.parametrize(
    ("key", "tampered", "expected"),
    (
        ("project", "someone-elses-project", "项目身份不匹配"),
        ("generator", "other-generator", "生成器不匹配"),
        ("source_root", "0" * 64, "项目根指纹不匹配"),
        ("output_dir", "0" * 64, "输出目录指纹不匹配"),
    ),
)
def test_forged_marker_is_refused(
    bundle_script: ModuleType,
    mini_project: Path,
    tmp_path: Path,
    key: str,
    tampered: str,
    expected: str,
) -> None:
    """标记存在但身份对不上 → 仍然拒绝清空（伪造/搬运/复制都拦得住）。"""
    out = tmp_path / f"dist-forged-{key}"
    assert _build(bundle_script, mini_project, out) == 0
    marker_path = out / bundle_script.MARKER_FILENAME
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload[key] = tampered
    marker_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)
    assert "构建标记校验" in str(excinfo.value)
    assert expected in str(excinfo.value)


def test_unreadable_marker_is_refused(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    """标记文件不是合法 JSON → 视为没有有效标记。"""
    out = tmp_path / "dist-broken-marker"
    assert _build(bundle_script, mini_project, out) == 0
    (out / bundle_script.MARKER_FILENAME).write_text("not json", encoding="utf-8")
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        _build(bundle_script, mini_project, out)
    assert "构建标记" in str(excinfo.value)


@pytest.mark.skipif(os.name != "nt", reason="目录联接是 Windows 的路径语义")
def test_windows_junction_out_dir_is_rejected(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """Windows Junction 必须被识别成重解析点，不能绕过路径检查。"""
    target = tmp_path / "junction-target"
    _write(target / "keep.txt", "keep\n")
    link = tmp_path / "junction-link"
    if not _make_junction(link, target):
        pytest.skip("当前环境不允许创建目录联接")

    for allow in (False, True):
        with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
            bundle_script.validate_output_dir(link, mini_project, allow_outside=allow)
        assert "链接" in str(excinfo.value)
    assert (target / "keep.txt").exists(), "联接指向的目标内容必须原样保留"


def test_link_swap_after_marker_check_is_refused(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路径在校验之后被换成链接：删除前的复查必须拦下（并发/路径变化不能绕过）。"""
    out = tmp_path / "dist-swap"
    assert _build(bundle_script, mini_project, out) == 0
    assert (out / bundle_script.MARKER_FILENAME).is_file()

    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "precious.txt", "别删我\n")
    shutil.rmtree(out)
    if not _make_junction(out, elsewhere):
        pytest.skip("当前环境不允许创建目录联接")

    # 模拟"标记校验已经通过"：剩下的唯一拦截点就是删除前的链接/路径解析复查
    monkeypatch.setattr(bundle_script, "marker_mismatch", lambda *args, **kwargs: None)
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.prepare_output_dir(out, root=mini_project)
    assert "链接" in str(excinfo.value)
    assert (elsewhere / "precious.txt").exists()


# ---------------------------------------------------------------------------
# P1：数据边界（回归快照单独控制 + 敏感串覆盖面）
# ---------------------------------------------------------------------------


def test_regression_pack_not_shipped_by_default(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """`regression/` 是 Case 级私有快照：默认不进包，必须显式开关。"""
    out = tmp_path / "dist-no-regression"
    assert _build(bundle_script, mini_project, out) == 0
    assert not any("regression/" in name for name in _zip_names(out))
    checklist = (out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "回归快照处理" in checklist
    assert "未打包" in checklist

    out2 = tmp_path / "dist-with-regression"
    assert _build(bundle_script, mini_project, out2, include_regression=True) == 0
    assert any("regression/" in name for name in _zip_names(out2))
    checklist2 = (out2 / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "--include-regression" in checklist2


def test_more_vendor_secret_formats_are_blocked(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """敏感串扫描不能只认 5 种格式：主流供应商密钥/Bearer/JWT 也要拦。"""
    _write(
        mini_project / "tracelab" / "leaked_vendor_keys.py",
        "\n".join(
            [
                'ANTHROPIC = "' + FAKE_ANTHROPIC_KEY + '"',
                'GITLAB = "' + FAKE_GITLAB_TOKEN + '"',
                'GOOGLE = "' + FAKE_GOOGLE_KEY + '"',
                'SLACK = "' + FAKE_SLACK_TOKEN + '"',
                'STRIPE = "' + FAKE_STRIPE_KEY + '"',
                'HF = "' + FAKE_HF_TOKEN + '"',
                'JWT = "' + FAKE_JWT + '"',
                'HEADER = "' + FAKE_BEARER + '"',
                "",
            ]
        ),
    )
    out = tmp_path / "dist-vendor-leak"
    assert _build(bundle_script, mini_project, out) == 3
    assert not list(out.glob("*.zip"))
    err = capsys.readouterr().err
    assert "leaked_vendor_keys.py" in err
    for label in ("Anthropic", "GitLab", "Google", "Slack", "Stripe", "HuggingFace", "JWT", "Bearer"):
        assert label in err, f"缺少对 {label} 的命中报告"


def test_checklist_has_source_inventory(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path
) -> None:
    """提交包必须有可检查的「来源清单」，而不是只有文件清单。"""
    out = tmp_path / "dist-source"
    assert _build(bundle_script, mini_project, out) == 0
    checklist = (out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "## 来源清单" in checklist
    assert "官方原始数据是否随包分发" in checklist
    assert "Case 级回归快照是否随包分发" in checklist


def test_zip_entries_are_relative_and_prefixed(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    out = tmp_path / "dist-prefix"
    assert _build(bundle_script, mini_project, out) == 0
    for name in _zip_names(out):
        assert name.startswith("yai-tracelab/"), name
        assert ".." not in Path(name).parts
        assert not name.startswith("/")


# ---------------------------------------------------------------------------
# C-16：`\\?\` 扩展长度前缀 / 8.3 短名不能绕过"项目根/主目录"判定
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="扩展长度前缀是 Windows 的路径语义")
def test_extended_prefix_project_root_is_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    """`--out "\\\\?\\<项目根>"` 必须被拒绝，而不是"字符串不等 → 放行"（Lead 已复现过）。"""
    before = _snapshot(mini_project)
    for prefix in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
        with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
            bundle_script.validate_output_dir(
                Path(prefix + str(mini_project)), mini_project, allow_outside=True
            )
        assert "扩展长度" in str(excinfo.value)
    assert _snapshot(mini_project) == before, "拒绝时不允许改动项目目录"


@pytest.mark.skipif(os.name != "nt", reason="扩展长度前缀是 Windows 的路径语义")
def test_extended_prefix_home_is_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    before = _snapshot(mini_project)
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.validate_output_dir(
            Path("\\\\?\\" + str(Path.home())), mini_project, allow_outside=True
        )
    assert "扩展长度" in str(excinfo.value)
    assert _snapshot(mini_project) == before


@pytest.mark.skipif(os.name != "nt", reason="8.3 短名是 Windows 的路径语义")
def test_short_name_segment_is_rejected(bundle_script: ModuleType, mini_project: Path) -> None:
    """未解析成真实路径的 `SOMETHING~1` 段同样会被"项目根字符串比较"绕过。"""
    weird = mini_project / "dist" / "YAI-TR~1"
    with pytest.raises(bundle_script.UnsafeOutputError) as excinfo:
        bundle_script.validate_output_dir(weird, mini_project)
    assert "8.3" in str(excinfo.value)

    # 反向对照：带 `~` 但不是 8.3 短名的普通名字不能被误杀
    assert bundle_script.validate_output_dir(mini_project / "dist" / "backup~x", mini_project)


# ---------------------------------------------------------------------------
# C-13：denial_reason 先 normpath 再逐段 normcase
# ---------------------------------------------------------------------------


def test_denial_reason_normalizes_dotdot_before_prefix_rules(bundle_script: ModuleType) -> None:
    """`tracelab/../data/official/...` 归一化后就是资料包路径，不能因为 parts[0] 而漏掉。"""
    rel = Path("tracelab") / ".." / "data" / "official" / "02_公开训练轨迹.jsonl"
    reason = bundle_script.denial_reason(rel, include_official=False)
    assert reason is not None and "赛事资料包" in reason


def test_denial_reason_blocks_password_and_pwd_names(bundle_script: ModuleType) -> None:
    for rel in (
        "tracelab/password_store.py",
        "tracelab/db_pwd.txt",
        "tracelab/PWD_NOTES.md",
        "tracelab/PasswordPolicy.py",
    ):
        assert bundle_script.denial_reason(Path(rel), include_official=False) is not None, rel


# ---------------------------------------------------------------------------
# C-20：更全的敏感串格式 + UTF-16LE 再扫一次 + 清单写明覆盖边界
# ---------------------------------------------------------------------------


def test_new_secret_formats_are_blocked(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        mini_project / "tracelab" / "leaked_new_formats.py",
        "\n".join(
            [
                'A = "' + FAKE_GITHUB_USER_TOKEN + '"',
                'B = "' + FAKE_GITHUB_SIGNING_TOKEN + '"',
                'C = "' + FAKE_OPENAI_PROJ_KEY + '"',
                'D = "' + FAKE_DB_CRED + '"',
                'E = "' + FAKE_PWD_ASSIGN + '"',
                "",
            ]
        ),
    )
    out = tmp_path / "dist-new-formats"
    assert _build(bundle_script, mini_project, out) == 3
    assert not list(out.glob("*.zip"))
    err = capsys.readouterr().err
    assert "leaked_new_formats.py" in err
    assert "GitHub" in err
    assert "OpenAI" in err
    assert "口令/密钥赋值" in err


def test_utf16le_secret_is_detected(
    bundle_script: ModuleType, mini_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UTF-16LE 文本里 ASCII 被 \\x00 隔开，字节正则匹配不到 → 必须解出来再扫一次。"""
    payload = FAKE_PWD_ASSIGN.encode("utf-16-le")
    assert b"\x00" in payload
    (mini_project / "tracelab" / "utf16_note.txt").write_bytes(payload)
    out = tmp_path / "dist-utf16"
    assert _build(bundle_script, mini_project, out) == 3
    err = capsys.readouterr().err
    assert "utf16_note.txt" in err
    assert "UTF-16LE" in err


def test_checklist_states_scan_coverage_boundary(bundle_script: ModuleType, mini_project: Path, tmp_path: Path) -> None:
    out = tmp_path / "dist-coverage-note"
    assert _build(bundle_script, mini_project, out) == 0
    checklist = (out / "submission" / "提交清单.md").read_text(encoding="utf-8")
    assert "二进制/压缩内容不在覆盖范围" in checklist
    assert "UTF-16LE" in checklist


# ---------------------------------------------------------------------------
# C-38：scan_zip 必须识别 `C:` 盘符条目（绝对与盘符相对都算）
# ---------------------------------------------------------------------------


def test_scan_zip_flags_drive_letter_entries(bundle_script: ModuleType, tmp_path: Path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("C:/absolute.txt", "x")
        archive.writestr("C:drive-relative.txt", "x")
        archive.writestr("//server/share/unc.txt", "x")
    report = bundle_script.scan_zip(evil, include_official=False)
    assert not report.clean
    for name in ("C:/absolute.txt", "C:drive-relative.txt"):
        assert any("盘符/UNC" in problem and name in problem for problem in report.problems), (
            name,
            report.problems,
        )


def test_scan_zip_clean_bundle_is_not_flagged(bundle_script: ModuleType, tmp_path: Path) -> None:
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("yai-tracelab/tracelab/cli.py", "def main() -> int:\n    return 0\n")
    report = bundle_script.scan_zip(good, include_official=False)
    assert report.clean, report.problems
    assert report.scanned == 1


# ---------------------------------------------------------------------------
# C-36：提交包自检必须走编码安全包装（-X utf8 + 语义断言），不能只看退出码
# ---------------------------------------------------------------------------

#: 假校验器：只有同时满足 `-X utf8` 与显式 UTF-8 环境才"通过"。
#: 它让"直接跑官方校验器"的实现必然失败（不依赖宿主机默认编码）。
FAKE_VALIDATOR_BODY = (
    "import os, sys\n"
    "ok = sys.flags.utf8_mode == 1 and os.environ.get('PYTHONIOENCODING') == 'utf-8'\n"
    "print('格式检查通过' if ok else '校验失败：编码未受控')\n"
    "raise SystemExit(0 if ok else 1)\n"
)


def _fake_validator(tmp_path: Path, body: str = FAKE_VALIDATOR_BODY) -> tuple[Path, Path, Path]:
    validator = tmp_path / "08_提交格式校验.py"
    validator.write_text(body, encoding="utf-8")
    cases = tmp_path / "cases.jsonl"
    cases.write_text("{}\n", encoding="utf-8")
    submission = tmp_path / "submission.jsonl"
    submission.write_text("{}\n", encoding="utf-8")
    return validator, cases, submission


def test_official_validator_runs_through_encoding_safe_wrapper(
    bundle_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator, cases, submission = _fake_validator(tmp_path)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    passed, reason = bundle_script._run_official_validator(validator, cases, submission)
    assert passed, reason
    assert "通过" in reason


def test_official_validator_fallback_also_enforces_utf8(
    bundle_script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """包装模块不可用时，兜底实现同样必须带 `-X utf8` + 语义断言。"""
    validator, cases, submission = _fake_validator(tmp_path)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(bundle_script, "_load_sibling_module", lambda *args, **kwargs: None)
    passed, reason = bundle_script._run_official_validator(validator, cases, submission)
    assert passed, reason


def test_official_validator_semantic_assertion_rejects_silent_success(
    bundle_script: ModuleType, tmp_path: Path
) -> None:
    """退出码 0 但没打印成功标记 → 不可信（编码炸了和真通过必须分开）。"""
    validator, cases, submission = _fake_validator(
        tmp_path, "import sys\nprint('done')\nraise SystemExit(0)\n"
    )
    passed, reason = bundle_script._run_official_validator(validator, cases, submission)
    assert not passed
    assert "未出现成功标记" in reason




