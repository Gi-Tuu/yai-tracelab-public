"""专项验收集（抗干扰/等义/事实反事实/有效路径/错配）Judge 运行器。

用法（数据私有，勿提交）：
    python scripts/final_round_controls.py --controls <专项验收集.jsonl> --out out/controls --live --api-key-file <key>

输出 controls.jsonl / receipts.json / judge_inputs/ / manifest.json；
每行保留 baseline/variant 两侧 verdict 与 expected_boundary。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracelab.final_round import (  # noqa: E402
    ControlFormatError,
    OpenAICompatJudge,
    ProviderError,
    ScriptedJudge,
    load_controls,
    resolve_api_key,
    run_controls,
    write_control_batch,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="专项验收集 Judge 运行器")
    parser.add_argument("--controls", required=True, help="专项验收集 JSONL（私有资料，勿提交）")
    parser.add_argument("--out", default="out/final-round-controls", help="输出目录")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个 control（0=全部）")
    parser.add_argument("--workers", type=int, default=1, help="并发 Judge 调用数")
    parser.add_argument("--retries", type=int, default=4, help="真实调用额外重试次数")
    parser.add_argument("--live", action="store_true", help="使用真实 qwen3.8-max")
    parser.add_argument("--api-key-file", default=None, help="本地私有 Key 文件路径")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容网关 base URL（默认官方）")
    parser.add_argument("--model", default=None, help="模型 ID（默认 qwen3.8-max）")
    parser.add_argument("--checkpoint", default=None, help="断点文件（默认 <out>/checkpoint.jsonl）")
    parser.add_argument("--fresh", action="store_true", help="忽略已有断点，从头跑")
    args = parser.parse_args()

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
    try:
        controls = load_controls(args.controls)
    except ControlFormatError as exc:
        print(f"输入结构不合规：{exc}", file=sys.stderr)
        return 2
    if args.limit > 0:
        controls = controls[: args.limit]
    checkpoint = Path(args.checkpoint) if args.checkpoint else Path(args.out) / "checkpoint.jsonl"
    if args.fresh and checkpoint.exists():
        checkpoint.unlink()
    batch = run_controls(controls, judge, max_workers=args.workers, checkpoint_path=checkpoint)
    write_control_batch(args.out, batch)
    if args.live:
        used_model = args.model or "qwen3.8-max"
        mode = f"LIVE_MODEL_CUSTOM（{used_model}）" if used_model != "qwen3.8-max" else "LIVE_MODEL（qwen3.8-max）"
    else:
        mode = "SCRIPTED_TEST_NOT_EXECUTED"
    print(f"输出目录：{args.out}")
    print(f"controls：{len(batch.controls)}　输入：{len(batch.snapshots)}　收据：{len(batch.receipts)}")
    print("Judge：" + mode)
    print(f"断点文件：{checkpoint}（重跑同一命令会跳过已完成输入）")
    for warning in getattr(batch, "warnings", []) or []:
        print(f"警告：{warning}", file=sys.stderr)
    if batch.issues:
        for issue in batch.issues[:10]:
            print(" - " + issue, file=sys.stderr)
        return 1
    print("收据链/结构校验：通过")
    if batch.retryable_errors:
        print(f"有 {batch.retryable_errors} 条调用失败（网络/解析），未写入断点；重跑同一命令会继续重试。", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
