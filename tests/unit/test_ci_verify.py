"""`scripts/ci_verify.py` 的门禁断言（C-34 空清单通过 / C-35 尝试次数不设下限）。

全部在临时目录里构造 mini 数据目录与 mutation.json，只调纯函数，不碰真数据。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_verify.py"


@pytest.fixture(scope="module")
def ci_verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verify_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    count: int = 12,
    manifest_count: int | None = None,
    extra_file: str | None = None,
    drop_file: bool = False,
    corrupt_hash: bool = False,
) -> Path:
    """构造 data/official/{MANIFEST.sha256, student-1.0/*} 并把模块的 DATA 指过去。"""
    data = tmp_path / "data" / "official"
    folder = data / "student-1.0"
    folder.mkdir(parents=True)
    lines: list[str] = []
    hashes: list[str] = []
    for index in range(count):
        name = f"{index:02d}_item.txt"
        payload = f"payload {index}\n".encode()
        (folder / name).write_bytes(payload)
        hashes.append(hashlib.sha256(payload).hexdigest())
        lines.append(f"{hashes[-1]}  {name}")
    if corrupt_hash:
        lines[0] = f"{'0' * 64}  {lines[0].split(maxsplit=1)[1]}"
    if drop_file:
        (folder / f"{count - 1:02d}_item.txt").unlink()
    if extra_file:
        (folder / extra_file).write_text("extra\n", encoding="utf-8")
    limit = len(lines) if manifest_count is None else manifest_count
    (data / "MANIFEST.sha256").write_text("\n".join(lines[:limit]) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "DATA", data)
    return data


def test_data_check_passes_on_consistent_manifest(
    ci_verify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_data(tmp_path, monkeypatch, ci_verify, count=12)
    assert ci_verify.check_data() == 0


def test_empty_manifest_is_rejected(
    ci_verify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空清单 = 检查 0 个文件也能"通过"（C-34 的核心漏洞）。"""
    data = _make_data(tmp_path, monkeypatch, ci_verify, count=12, manifest_count=0)
    assert (data / "MANIFEST.sha256").read_text(encoding="utf-8") == "\n"
    assert ci_verify.check_data() == 1
    assert "低于下限" in capsys.readouterr().err


def test_truncated_manifest_below_floor_is_rejected(
    ci_verify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ci_verify.MIN_MANIFEST_ENTRIES == 11
    _make_data(tmp_path, monkeypatch, ci_verify, count=2)
    assert ci_verify.check_data() == 1
    assert "低于下限 11" in capsys.readouterr().err


def test_unlisted_extra_file_is_rejected(
    ci_verify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """目录里有、清单里没有 → 清单没有覆盖目录（双向核对）。"""
    _make_data(tmp_path, monkeypatch, ci_verify, count=12, extra_file="99_smuggled.txt")
    assert ci_verify.check_data() == 1
    assert "UNLISTED 99_smuggled.txt" in capsys.readouterr().out


def test_missing_listed_file_is_rejected(
    ci_verify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_data(tmp_path, monkeypatch, ci_verify, count=12, drop_file=True)
    assert ci_verify.check_data() == 1
    assert "MISSING 11_item.txt" in capsys.readouterr().out


def test_hash_mismatch_is_rejected(
    ci_verify: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_data(tmp_path, monkeypatch, ci_verify, count=12, corrupt_hash=True)
    assert ci_verify.check_data() == 1
    assert "MISMATCH 00_item.txt" in capsys.readouterr().out


def _write_mutation(
    path: Path, *, tried: int, invalid: int = 0, rate: float = 1.0, missed: list[str] | None = None
) -> Path:
    payload = {
        "results": [
            {"injection": "demo", "invalid": invalid, "missed_runs": missed or []},
        ],
        "overall": {
            "injections": 1,
            "tried": tried,
            "detected": tried,
            "detection_rate": rate,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_mutation_zero_tried_is_rejected(
    ci_verify: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """0 次尝试时 detection_rate 也可以写成 1.0——没有下限就成了假绿（C-35）。"""
    path = _write_mutation(tmp_path / "mutation.json", tried=0)
    assert ci_verify.check_mutation(path) == 1
    err = capsys.readouterr().err
    assert "下限" in err


def test_mutation_below_floor_is_rejected(ci_verify: ModuleType, tmp_path: Path) -> None:
    assert ci_verify.MIN_MUTATION_TRIED == 60
    path = _write_mutation(tmp_path / "mutation.json", tried=59)
    assert ci_verify.check_mutation(path) == 1


def test_mutation_at_floor_passes(ci_verify: ModuleType, tmp_path: Path) -> None:
    path = _write_mutation(tmp_path / "mutation.json", tried=ci_verify.MIN_MUTATION_TRIED)
    assert ci_verify.check_mutation(path) == 0


def test_mutation_still_requires_zero_missed_and_zero_invalid(
    ci_verify: ModuleType, tmp_path: Path
) -> None:
    good = _write_mutation(tmp_path / "good.json", tried=ci_verify.MIN_MUTATION_TRIED)
    assert ci_verify.check_mutation(good) == 0

    invalid = _write_mutation(tmp_path / "invalid.json", tried=ci_verify.MIN_MUTATION_TRIED, invalid=2)
    assert ci_verify.check_mutation(invalid) == 1

    missed = _write_mutation(tmp_path / "missed.json", tried=ci_verify.MIN_MUTATION_TRIED, missed=["demo"])
    assert ci_verify.check_mutation(missed) == 1

    low_rate = _write_mutation(tmp_path / "rate.json", tried=ci_verify.MIN_MUTATION_TRIED, rate=0.5)
    assert ci_verify.check_mutation(low_rate) == 1
