"""CI 验证脚本：把"可复现性"和"结果没退化"变成可执行的断言。

本地等价用法：
    python scripts/ci_verify.py data
    python scripts/ci_verify.py metrics --dir out/train
    python scripts/ci_verify.py determinism --a out/train/submission.jsonl --b out/rerun/submission.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

#: 训练集上必须复现的指标（规则集/评测器改动导致下降即视为回归）
EXPECTED_METRICS: dict[str, float] = {
    "agent_task_success_accuracy": 1.0,
    "business_invoice_created_accuracy": 1.0,
    "version_direction_accuracy": 1.0,
    "first_fault_event_exact_match": 1.0,
    "root_cause_label_macro_f1": 1.0,
    "evidence_id_validity": 1.0,
}

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "official"

#: 清单条数下限（C-34）：2026-11-04 实测 **13** 条。
#: 没有这条下限时，空清单/被截断成 0 条的清单会让 `checked == 0` 也打印"检查 0 个 → 通过"。
MIN_MANIFEST_ENTRIES = 11

#: 注入实验尝试次数下限（C-35）：2026-11-04 实测 **8 类注入 / 69 次尝试**
#: （训练集 + 验证集合并）。留约 13% 余量：数据没变时它只会随注入器增强而变多，
#: 变少说明注入器被削弱、数据被截断或"零尝试"被当成零漏检。
MIN_MUTATION_TRIED = 60

# Windows 上 stdout 可能是 cp936/charmap，打印中文文件名会 UnicodeEncodeError（CI 已踩过）。
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")


def _parse_manifest(manifest: Path) -> list[tuple[str, str]] | None:
    """读 `<sha256>  <文件名>` 清单；格式不合法返回 None。"""
    entries: list[tuple[str, str]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            return None
        entries.append((parts[0].strip().lower(), parts[1].strip()))
    return entries


def check_data() -> int:
    """原始数据必须逐字节保持：任何 EOL 转换都会让评测结论失去可复现性。

    两道结构性断言（C-34），缺了它们"空清单通过"就成立：
    1. 清单条数不得少于 `MIN_MANIFEST_ENTRIES`（实测 13 条）；
    2. 目录内文件集合与清单集合**双向相等**（少文件、多文件都算失败）。
    """
    manifest = DATA / "MANIFEST.sha256"
    if not manifest.exists():
        print(f"错误：找不到 {manifest}", file=sys.stderr)
        return 1
    entries = _parse_manifest(manifest)
    if entries is None:
        print(f"错误：{manifest} 存在格式不合法的行（应为 64 位 sha256 + 文件名）", file=sys.stderr)
        return 1
    if len(entries) < MIN_MANIFEST_ENTRIES:
        print(
            f"错误：清单只有 {len(entries)} 条，低于下限 {MIN_MANIFEST_ENTRIES}"
            f"（实测 13 条；空/截断清单不得判通过）",
            file=sys.stderr,
        )
        return 1

    data_dir = DATA / "student-1.0"
    listed = {name for _, name in entries}
    actual = {path.name for path in data_dir.iterdir() if path.is_file()} if data_dir.is_dir() else set()

    bad = 0
    for name in sorted(listed - actual):
        print(f"MISSING {name}")
        bad += 1
    for name in sorted(actual - listed):
        print(f"UNLISTED {name}（目录里有、清单里没有：清单必须覆盖目录内全部文件）")
        bad += 1

    checked = 0
    for expect, name in entries:
        target = data_dir / name
        if not target.exists():
            continue  # 已在 MISSING 里记过，不重复计数
        real = hashlib.sha256(target.read_bytes()).hexdigest()
        checked += 1
        if real != expect:
            print(f"MISMATCH {name}\n  expect {expect}\n  actual {real}")
            bad += 1

    print(
        f"数据完整性：清单 {len(entries)} 条 / 实算 {checked} 个文件，"
        f"不一致 {bad} 个（目录内 {len(actual)} 个文件）"
    )
    if checked < len(entries):
        print(f"错误：只实算了 {checked} 个文件（清单 {len(entries)} 条），拒绝通过", file=sys.stderr)
        return 1
    return 1 if bad else 0


def check_metrics(directory: Path) -> int:
    path = directory / "metrics.json"
    if not path.exists():
        print(f"错误：找不到 {path}（需带 --labels 运行评测）", file=sys.stderr)
        return 1
    values = json.loads(path.read_text(encoding="utf-8"))["values"]
    failed = [(k, values.get(k), v) for k, v in EXPECTED_METRICS.items() if values.get(k) != v]
    for key, got, want in failed:
        print(f"REGRESSION {key}: got={got} want={want}")
    print("方法指标：全部达到预期" if not failed else f"方法指标：{len(failed)} 项回归")
    return 1 if failed else 0


def check_mutation(path: Path) -> int:
    """注入实验门禁：必须零漏检、零无效注入，且**尝试次数不得缩水**（C-35）。

    无效注入要单独卡住——它意味着注入器有 bug，此时即便显示"100% 检出"也是假象。
    尝试次数同样要卡：0 次尝试的 `detection_rate` 也可以写成 1.0，看起来全绿。
    """
    if not path.exists():
        print(f"错误：找不到 {path}", file=sys.stderr)
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    overall = payload.get("overall", {})
    invalid = sum(int(r.get("invalid", 0)) for r in results)
    missed = [r["injection"] for r in results if r.get("missed_runs")]
    tried = int(overall.get("tried") or 0)
    print(f"注入：{tried} 次尝试 / {overall.get('detected')} 次检出 / 无效注入 {invalid}")
    ok = (
        overall.get("detection_rate") == 1.0
        and invalid == 0
        and not missed
        and tried >= MIN_MUTATION_TRIED
    )
    if ok:
        print(f"注入门禁：通过（零漏检、零无效注入、尝试 {tried} ≥ 下限 {MIN_MUTATION_TRIED}）")
        return 0
    print(
        f"注入门禁失败：漏检={missed} 无效注入={invalid} "
        f"尝试 {tried} < 下限 {MIN_MUTATION_TRIED}（注入器被削弱或数据被截断）",
        file=sys.stderr,
    )
    return 1


def check_determinism(first: Path, second: Path) -> int:
    if not first.exists() or not second.exists():
        print("错误：缺少待比较的提交文件", file=sys.stderr)
        return 1
    a = hashlib.sha256(first.read_bytes()).hexdigest()
    b = hashlib.sha256(second.read_bytes()).hexdigest()
    print(f"first={a[:16]} rerun={b[:16]}")
    if a != b:
        print("结果不可复现：同一输入两次运行产出不同字节", file=sys.stderr)
        return 1
    print("可复现性：通过（逐字节相同）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TraceLab CI 验证")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("data", help="校验原始数据哈希")

    p_metrics = sub.add_parser("metrics", help="校验训练集方法指标未回归")
    p_metrics.add_argument("--dir", default="out/train")

    p_det = sub.add_parser("determinism", help="校验两次运行结果逐字节相同")
    p_det.add_argument("--a", required=True)
    p_det.add_argument("--b", required=True)

    p_mut = sub.add_parser("mutation", help="校验注入实验零漏检、零无效注入")
    p_mut.add_argument("--path", default="out/mutation/mutation.json")

    args = parser.parse_args(argv)
    if args.command == "data":
        return check_data()
    if args.command == "metrics":
        return check_metrics(Path(args.dir))
    if args.command == "mutation":
        return check_mutation(Path(args.path))
    if args.command == "determinism":
        return check_determinism(Path(args.a), Path(args.b))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
