"""调用官方提交格式校验器的安全包装。

为什么需要这一层
----------------
官方 `08_提交格式校验.py` 是只读第三方文件，里面直接 `print` 中文，没有任何编码兜底。
在 Windows（默认 cp1252）或 `PYTHONIOENCODING` 被设成非 UTF-8 的环境里，
它会在打印成功信息时抛 `UnicodeEncodeError`，表现为"校验失败"，实际是**编码问题**。

修不了官方文件，就把兜底放进代码：
1. 子进程用 `-X utf8` 启动（隔离父进程的编码设置）；
2. 显式覆盖 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`；
3. 用 UTF-8 解码子进程输出，避免在**读**的时候再崩一次；
4. 对输出做语义检查（必须出现"格式检查通过"），把"编码炸了"和"真的校验失败"区分开。

用法：
    python scripts/run_official_validator.py \
        --validator data/official/student-1.0/08_提交格式校验.py \
        --cases data/official/student-1.0/02_公开训练轨迹.jsonl \
        --submission out/train/submission.jsonl
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

#: 官方脚本成功时会打印这句话（中文，正是编码问题的高发点）
SUCCESS_MARKER = "格式检查通过"
ENCODING_FAILURE_MARKERS = ("UnicodeEncodeError", "charmap", "codec can't encode")

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")


def build_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：强制 UTF-8，不受父进程设置影响。"""
    env = dict(os.environ if base is None else base)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Windows 控制台代码页也一并压成 UTF-8，避免子进程再被 cp936/cp1252 影响
    env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
    return env


def build_command(validator: Path, cases: Path, submission: Path) -> list[str]:
    return [
        sys.executable, "-X", "utf8", str(validator),
        "--cases", str(cases),
        "--submission", str(submission),
    ]


def run_validation(
    validator: Path, cases: Path, submission: Path, *, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """执行官方校验，返回（退出码，合并输出）。

    输出一律按 UTF-8 解码并对坏字节宽容替换——解码失败不能掩盖真实退出码。
    """
    proc = subprocess.run(
        build_command(validator, cases, submission),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=build_env(env),
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def classify(returncode: int, output: str) -> tuple[bool, str]:
    """把结果分成三类：通过 / 编码故障 / 真实校验失败。"""
    if any(marker in output for marker in ENCODING_FAILURE_MARKERS):
        return False, "编码故障：官方脚本的输出无法在当前环境编码（不是校验结论）"
    if returncode == 0 and SUCCESS_MARKER in output:
        return True, "通过"
    if returncode == 0:
        return False, "退出码为 0 但未出现成功标记，判定不可信"
    return False, f"校验失败（退出码 {returncode}）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全调用官方提交格式校验器")
    parser.add_argument("--validator", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--submission", required=True)
    args = parser.parse_args(argv)

    validator = Path(args.validator)
    cases = Path(args.cases)
    submission = Path(args.submission)
    for path, name in ((validator, "校验器"), (cases, "Case 文件"), (submission, "提交文件")):
        if not path.exists():
            print(f"错误：找不到{name} {path}", file=sys.stderr)
            return 2
        if not path.is_file():
            # 审计 R4-19：目录当参数属"输入类型错误"，应与"校验失败"区分（退出码 2）。
            print(f"错误：{name}不是文件：{path}；未执行校验。", file=sys.stderr)
            return 2

    returncode, output = run_validation(validator, cases, submission)
    ok, reason = classify(returncode, output)
    print(output.strip())
    print(f"[{Path(cases).name}] {reason}", file=sys.stdout if ok else sys.stderr)
    if ok:
        return 0
    # 编码故障用 3 区分，便于 CI 一眼看出"不是校验不通过"
    return 3 if "编码故障" in reason else 1


if __name__ == "__main__":
    raise SystemExit(main())
