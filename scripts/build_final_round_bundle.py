"""构建复赛提交包：代码 + 文档 + 聚合结果；排除 Key/官方原始数据/原始轨迹。

用法：
    python scripts/build_final_round_bundle.py \
        --private-root E:\\Competition\\final-round-2026 \
        --out E:\\Competition\\final-round-2026\\dist
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round.metadata import build_index  # noqa: E402

CODE_FILES = ["pyproject.toml", "README.md", "启动体验.cmd", "scripts/final_round_console_page.html"]
CODE_DIRS = ["tracelab", "scripts"]
DOC_FILES = [
    "docs/method.md",
    "docs/architecture.md",
    "docs/final-round-core.md",
    "docs/final-round-report.md",
    "docs/final-round-results.md",
    "docs/final-round-plan.md",
    "docs/r2g2-plan.md",
    "docs/final-round-experiment-records.md",
    "docs/final-round-demo-plan.md",
    "docs/final-round-submit-checklist.md",
    "docs/receipt-migration.md",
    "CHANGELOG.md",
    "docs/baseline-comparison.md",
    "docs/evaluator-version-compare.md",
    "docs/final-round-video-script.md",
]
TEST_FILES = [
    "tests/unit/test_r2g1_input_contract.py",
    "tests/unit/test_r2g1_checkpoint_identity.py",
    "tests/unit/test_r2g1_verdict_comparison.py",
    "tests/unit/test_r2g1_dual_track.py",
    "tests/unit/test_r2g1_metrics_integrity.py",
    "tests/unit/test_r2g1_call_records.py",
    "tests/unit/test_r2g1_domain_rules.py",
    "tests/unit/test_r2g1_script_coverage.py",
    "tests/unit/test_r2g2_cross_domain.py",
    "tests/unit/test_r2g2_experiment_index.py",
    "tests/unit/test_r2g2_bundle.py",
    "tests/unit/test_r2g2_metadata_index.py",
    "tests/unit/test_r2g2_mirror_manifest.py",
    "tests/unit/test_r2g2_mirror_manifest_json.py",
    "tests/unit/test_r2g2_receipt_verifier.py",
    "tests/unit/test_r2g2_soak.py",
    "tests/unit/test_r2g11_evidence_pipeline.py",
    "tests/unit/test_r2g11_metrics_consistency.py",
    "tests/unit/test_r2g11_bundle_readiness.py",
    "tests/unit/test_r2g12_delivery.py",
    "tests/unit/test_r2g12_controls.py",
    "tests/unit/test_r2g12_evidence_metrics.py",
    "tests/unit/test_r2g12_version_identity.py",
    "tests/unit/test_r2g12_business_type.py",
]
PRIVATE_FILES = [
    "freeze-public-v1.json",
    "metadata-index.json",
]
PRIVATE_DIRS = [
    "report-ai-public100",
    "report-invoice-public10",
    "report-model-compare-public100",
    "report-controls-model-compare",
    "dual-invoice10",
    "experiment-index",
    "r2g-final-verify-1case-fixed",
    "r2g-v2-receipts-3case",
    "soak-synthetic-1000",
    "soak-synthetic-5000",
    "soak-live-8w-150",
    "soak-official-4w",
    "soak-official-8w",
    "soak-official-8w-16case",
]
SECRET_MARKERS = ("api." + "suanli.cn", "own-" + "api.key", "BEGIN " + "PRIVATE KEY")
#: 正式交付检查：必需 / 可选 / 只能标记未核实的内容。
FORMAL_REPORT = "docs/final-round-report.md"
STARTUP_FILES = ("README.md", "启动体验.cmd", "pyproject.toml")
REQUIRED_PRIVATE = (
    "report-invoice-public10/metrics.json",
    "report-ai-public100/metrics.json",
    "dual-invoice10/extended_results.jsonl",
    "dual-invoice10/manifest.json",
    "r2g-v2-receipts-3case/results.jsonl",
    "r2g-v2-receipts-3case/receipts.json",
)
OPTIONAL_FILES = (
    "docs/final-round-demo-plan.md",
    "docs/final-round-submit-checklist.md",
    "docs/final-round-results.md",
)
VIDEO_CANDIDATES = (
    "docs/final-round-demo.mp4",
    "docs/demo.mp4",
    "final-round-demo.mp4",
    "视频.mp4",
)
OFFICIAL_SECTIONS = tuple(range(1, 11))
PACKAGE_REQUIRED = (
    "README.md",
    "启动体验.cmd",
    "pyproject.toml",
    "docs/final-round-report.md",
    "scripts/final_round_console_page.html",
    "results/report-invoice-public10/metrics.json",
    "results/report-ai-public100/metrics.json",
    "results/dual-invoice10/extended_results.jsonl",
    "results/r2g-v2-receipts-3case/results.jsonl",
)
EXTERNAL_REQUIRED = ("video",)
OPTIONAL_MATERIALS = ("docs/final-round-demo-plan.md", "docs/final-round-submit-checklist.md")


def _report_section_numbers(path: Path) -> set[int]:
    numbers: set[int] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"^##\s+(\d+)\.", line.strip())
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def _entry(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "status": "missing", "required": required}
    return {"path": str(path), "status": "present", "required": required}


def check_readiness(private_root: Path, *, repo_root: Path = ROOT) -> dict[str, Any]:
    """正式交付检查，不改变默认打包路径。"""
    required: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    not_verified: list[str] = []
    missing_required: list[str] = []

    for rel in STARTUP_FILES:
        item = _entry(repo_root / rel, required=True)
        required.append(item)
        if item["status"] != "present":
            missing_required.append(rel)

    report = repo_root / FORMAL_REPORT
    if not report.is_file():
        required.append({"path": FORMAL_REPORT, "status": "missing", "required": True})
        missing_required.append(FORMAL_REPORT)
    else:
        sections = _report_section_numbers(report)
        missing_sections = [n for n in OFFICIAL_SECTIONS if n not in sections]
        status = "present" if not missing_sections else "invalid"
        required.append(
            {
                "path": FORMAL_REPORT,
                "status": status,
                "required": True,
                "sections_found": sorted(sections),
                "sections_missing": missing_sections,
            }
        )
        if missing_sections:
            missing_required.append(f"{FORMAL_REPORT}（缺十节结构：{missing_sections}）")
        else:
            # 结构可检查，但正文内容没有被程序核验。
            not_verified.append(f"{FORMAL_REPORT}（正文内容未人工核验）")

    for rel in REQUIRED_PRIVATE:
        item = _entry(private_root / rel, required=True)
        required.append(item)
        if item["status"] != "present":
            missing_required.append(f"private:{rel}")

    video = next((repo_root / rel for rel in VIDEO_CANDIDATES if (repo_root / rel).is_file()), None)
    if video is None:
        required.append({"path": "video", "status": "missing", "required": True})
        missing_required.append("video")
    else:
        # 只检查存在，不把"未核验时长/音频"说成已验证。
        required.append({"path": str(video), "status": "not_verified", "required": True})
        not_verified.append(str(video))

    for rel in OPTIONAL_FILES:
        item = _entry(repo_root / rel, required=False)
        optional.append(item)

    ready = not missing_required
    return {
        "schema": "tracelab-delivery-readiness/1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repo_root": str(repo_root),
        "private_root": str(private_root),
        "ready": ready,
        "fully_verified": ready and not not_verified,
        "required": required,
        "optional": optional,
        "missing_required": missing_required,
        "not_verified": not_verified,
        "note": "ready 只表示必需材料存在；video 等未实际核验的内容进入 not_verified。",
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add(zf: zipfile.ZipFile, src: Path, arc: str) -> dict:
    arc = arc.replace("\\", "/")
    zf.write(src, arc)
    return {"path": arc, "sha256": _sha(src), "bytes": src.stat().st_size}


def validate_bundle(zip_path: Path) -> dict[str, Any]:
    """读取实际 ZIP，核对 Manifest、必需路径、摘要与重复归档。"""
    issues: list[str] = []
    required_missing: list[str] = []
    duplicates: list[str] = []
    path_mismatch: list[str] = []
    digest_mismatch: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if "MANIFEST.json" not in names:
            issues.append("ZIP 缺少 MANIFEST.json")
            manifest: dict[str, Any] = {}
        else:
            manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
        seen: set[str] = set()
        for name in names:
            if name in seen:
                duplicates.append(name)
            seen.add(name)
        actual = [name for name in names if name != "MANIFEST.json"]
        entries = manifest.get("entries") or []
        entry_paths = [str(item.get("path")) for item in entries]
        if len(entry_paths) != len(set(entry_paths)):
            duplicates.extend(sorted({path for path in entry_paths if entry_paths.count(path) > 1}))
        if manifest.get("files") != len(entries):
            issues.append("Manifest.files 与 entries 数量不一致")
        if set(entry_paths) != set(actual):
            path_mismatch.extend(sorted(set(entry_paths) ^ set(actual)))
        for required in PACKAGE_REQUIRED:
            if required not in names:
                required_missing.append(required)
        for entry in entries:
            path = str(entry.get("path") or "")
            try:
                data = zf.read(path)
            except KeyError:
                path_mismatch.append(path)
                continue
            if entry.get("bytes") != len(data):
                digest_mismatch.append(f"{path}: bytes")
            actual_sha = hashlib.sha256(data).hexdigest()
            if entry.get("sha256") != actual_sha:
                digest_mismatch.append(f"{path}: sha256")
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                issues.append(f"非法归档路径：{name}")
    if required_missing:
        issues.append("缺少 ZIP 必需文件")
    if duplicates:
        issues.append("存在重复归档路径")
    if path_mismatch:
        issues.append("Manifest 与 ZIP 实际路径不一致")
    if digest_mismatch:
        issues.append("Manifest 摘要/字节数不一致")
    return {
        "zip": str(zip_path),
        "files": len(names),
        "manifest_files": len(entries),
        "required_missing": required_missing,
        "duplicates": sorted(set(duplicates)),
        "path_mismatch": sorted(set(path_mismatch)),
        "digest_mismatch": sorted(set(digest_mismatch)),
        "issues": issues,
        "valid": not issues,
    }


def _collect(private_root: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    items: list[tuple[Path, str]] = []
    missing: list[str] = []
    for rel in CODE_FILES:
        path = ROOT / rel
        if path.is_file():
            items.append((path, rel))
    for directory in CODE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "build_final_round_bundle.py":
                continue
            items.append((path, str(path.relative_to(ROOT))))
    for rel in DOC_FILES:
        path = ROOT / rel
        if path.is_file():
            items.append((path, rel))
        else:
            missing.append(rel)
    for rel in TEST_FILES:
        path = ROOT / rel
        if path.is_file():
            items.append((path, rel))
        else:
            missing.append(rel)
    for rel in PRIVATE_FILES:
        path = private_root / rel
        if path.is_file():
            items.append((path, f"results/{rel}"))
        else:
            missing.append(f"private:{rel}")
    for rel in PRIVATE_DIRS:
        base = private_root / rel
        if not base.is_dir():
            missing.append(f"private:{rel}")
            continue
        for name in (
            "report.md",
            "metrics.json",
            "manifest.json",
            "legacy_submission.jsonl",
            "extended_results.jsonl",
            "results.jsonl",
            "receipts.json",
            "metadata-status.json",
            "soak-report.json",
        ):
            path = base / name
            if path.is_file():
                items.append((path, f"results/{rel}/{name}"))
        if rel == "experiment-index":
            for path in sorted(base.glob("*.json")):
                items.append((path, f"results/{rel}/{path.name}"))
        batch = base / "batch"
        if batch.is_dir():
            for name in ("results.jsonl", "receipts.json", "manifest.json", "metadata-status.json"):
                path = batch / name
                if path.is_file():
                    items.append((path, f"results/{rel}/batch/{name}"))
    return items, missing


def build(private_root: Path, out_dir: Path, bundle_name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # 打包前刷新 metadata-index，避免新增收据批次后包内索引过期。
    index = build_index(private_root)
    (private_root / "metadata-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    items, missing = _collect(private_root)
    leaked = []
    for path, _arc in items:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in SECRET_MARKERS:
            if marker in text:
                leaked.append(f"{path}: {marker}")
    if leaked:
        raise SystemExit("包内含敏感标记，拒绝打包：\n" + "\n".join(leaked[:10]))
    zip_path = out_dir / f"{bundle_name}.zip"
    entries: list[dict] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arc in items:
            entries.append(_add(zf, path, arc))
        manifest = {
            "schema": "tracelab-final-round-bundle/1",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "bundle": bundle_name,
            "files": len(entries),
            "missing": missing,
            "entries": entries,
            "note": "不含 API Key、官方原始数据、原始轨迹与隐藏集；结果均为聚合/双轨/冻结记录。",
        }
        zf.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="构建复赛提交包")
    parser.add_argument("--private-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--name", default="YAI_TraceLab_复赛提交包")
    parser.add_argument("--readiness", action="store_true", help="只做正式交付检查，不生成 ZIP")
    parser.add_argument("--repo-root", default=str(ROOT), help="正式检查的仓库根（测试可指向临时目录）")
    args = parser.parse_args()
    if args.readiness:
        report = check_readiness(Path(args.private_root), repo_root=Path(args.repo_root))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        target = out / "delivery-readiness.json"
        zip_path = build(Path(args.private_root), out, args.name)
        zip_check = validate_bundle(zip_path)
        report["zip"] = zip_check
        report["zip_valid"] = bool(zip_check["valid"])
        report["ready"] = bool(report["ready"] and zip_check["valid"])
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"交付检查：{'READY' if report['ready'] else 'NOT READY'}")
        print(f"ZIP 核对：{'PASS' if zip_check['valid'] else 'FAIL'}（{zip_check['files']} 个文件）")
        print(f"未核实：{report['not_verified'] or '无'}")
        print(f"清单：{target}")
        if report["missing_required"]:
            for item in report["missing_required"]:
                print(f"缺少必需材料：{item}")
        if zip_check["issues"]:
            for item in zip_check["issues"]:
                print(f"ZIP 问题：{item}")
        return 0 if report["ready"] else 1
    zip_path = build(Path(args.private_root), Path(args.out), args.name)
    print(f"提交包：{zip_path}")
    print(f"大小：{zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
