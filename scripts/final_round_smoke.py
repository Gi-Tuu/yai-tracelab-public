"""复赛 Judge 流水线冒烟（脚本占位，不调用模型）。

用法（私有数据不要提交到公开仓库）：
    python scripts/final_round_smoke.py --cases <复赛Case.jsonl> --out out/final-round-smoke

产物显式标记 SCRIPTED_TEST_NOT_EXECUTED，不能当作参赛结果。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round import (  # noqa: E402
    FinalRoundFormatError,
    OpenAICompatJudge,
    ProviderError,
    ScriptedJudge,
    resolve_api_key,
    run_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="复赛 Judge 流水线冒烟（脚本占位，不调用模型）")
    parser.add_argument("--cases", required=True, help="复赛 Case JSONL（私有资料，勿提交）")
    parser.add_argument("--out", default="out/final-round-smoke", help="输出目录")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个 Case（0=全部；调试省额度）")
    parser.add_argument("--workers", type=int, default=1, help="并发 Judge 调用数（默认 1；实测网关可 4 路）")
    parser.add_argument(
        "--live",
        action="store_true",
        help="使用真实 qwen3.8-max（需要 YXAIHUB_API_KEY，或 --api-key-file；会消耗赛事额度）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="真实调用遇到 429/5xx/超时/SSL 瞬断时的额外重试次数（默认 4）",
    )
    parser.add_argument(
        "--api-key-file",
        default=None,
        help="本地私有 Key 文件路径（显式传入，不写死；文件勿提交/公开）",
    )
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容网关 base URL（默认官方）")
    parser.add_argument("--model", default=None, help="模型 ID（默认 qwen3.8-max）")
    parser.add_argument("--checkpoint", default=None, help="断点文件（默认 <out>/checkpoint.jsonl）")
    parser.add_argument("--fresh", action="store_true", help="忽略已有断点，从头跑")
    args = parser.parse_args()
    checkpoint = Path(args.checkpoint) if args.checkpoint else Path(args.out) / "checkpoint.jsonl"
    if args.fresh and checkpoint.exists():
        checkpoint.unlink()

    judge = ScriptedJudge()
    if args.live:
        try:
            resolve_api_key(key_file=args.api_key_file)
        except ProviderError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        judge = OpenAICompatJudge(
            api_key_file=args.api_key_file,
            retries=args.retries,
            base_url=args.base_url,
            model=args.model or "qwen3.8-max",
        )
        if args.model and args.model != "qwen3.8-max":
            judge.execution_origin = "LIVE_MODEL_CUSTOM"
    if args.limit > 0:
        from tracelab.final_round import load_cases, run_cases, write_batch  # noqa: PLC0415

        try:
            cases = load_cases(args.cases)[: args.limit]
        except FinalRoundFormatError as exc:
            print(f"输入结构不合规：{exc}", file=sys.stderr)
            return 2
        batch = run_cases(cases, judge, max_workers=args.workers, checkpoint_path=checkpoint)
        write_batch(args.out, batch)
    else:
        try:
            batch = run_file(
                args.cases,
                args.out,
                judge=judge,
                max_workers=args.workers,
                checkpoint_path=checkpoint,
            )
        except FinalRoundFormatError as exc:
            print(f"输入结构不合规：{exc}", file=sys.stderr)
            return 2

    print(f"输出目录：{args.out}")
    print(f"Case：{len(batch.results)}　run 快照：{len(batch.snapshots)}　收据：{len(batch.receipts)}")
    if args.live:
        used_model = args.model or "qwen3.8-max"
        mode = f"LIVE_MODEL_CUSTOM（{used_model}）" if used_model != "qwen3.8-max" else "LIVE_MODEL（qwen3.8-max）"
    else:
        mode = "SCRIPTED_TEST_NOT_EXECUTED（未调用真实模型，仅验证链路）"
    print("Judge：" + mode)
    print(f"断点文件：{checkpoint}（重跑同一命令会跳过已完成 run）")
    for warning in getattr(batch, "warnings", []) or []:
        print(f"警告：{warning}", file=sys.stderr)
    if batch.issues:
        print("结构问题：", file=sys.stderr)
        for issue in batch.issues[:10]:
            print(" - " + issue, file=sys.stderr)
        return 1
    print("扩展结果结构校验：通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
