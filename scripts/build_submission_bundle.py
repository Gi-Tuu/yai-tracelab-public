"""构建初赛提交包：一个可交给主办方的 zip + 一份自检清单。

用法：
    python scripts/build_submission_bundle.py --out dist                    # 默认，只允许 <项目根>/dist
    python scripts/build_submission_bundle.py --out /tmp/pkg --allow-outside
    python scripts/build_submission_bundle.py --out dist --include-official-data

产出：
    dist/yai-tracelab-submission-<YYYYMMDD>.zip   完整提交包
    dist/submission/                              展开后的内容（便于检查）
    dist/submission/提交清单.md                    逐项自检结果（含「包内安全扫描」一节）
    dist/submission/SHA256SUMS.txt                包内文件哈希

两条 P0 安全底线：
1. **删除前先校验**（`validate_output_dir` / `prepare_output_dir`）：
   输出目录默认只能是 `<项目根>/dist` 及其子目录；写到别处必须显式 `--allow-outside`。
   项目根本身、项目根的祖先、文件系统根、用户主目录（及其祖先）、路径上任意一级
   符号链接/目录联接，**任何时候都拒绝**，`--allow-outside` 也不放行。
   所有校验都在任何 `rmtree` 之前完成；校验失败时以非 0 退出，且不删除、不写入任何文件。
2. **白名单复制**（`ALLOW_FILES` / `ALLOW_DIRS` / `ALLOW_OFFICIAL_FILES`）：
   只复制明确列出的路径，再叠加 `DENY_*` 拒绝名单；`data/official/` 默认不打包
   （赛事授权范围不确定，保守处理），要打包必须显式 `--include-official-data`。
   打包后回读 zip 再扫一遍文件名与敏感串，命中即失败。

原则：**清单里每一项都必须是跑出来的结果，不是勾选框。**
任何一项失败，脚本以非 0 退出，不允许"看起来齐了"就交。

退出码：0 全部通过 / 1 有检查项未通过 / 2 输出目录不安全（未改动任何文件）/ 3 安全扫描未通过。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]

#: 默认输出目录（相对项目根）；只有它和它的子目录可以不经 --allow-outside 使用
DEFAULT_OUT_SUBDIR = "dist"
#: 展开后的包内容放在输出目录下的这个子目录里
BUNDLE_DIRNAME = "submission"
#: zip 内的顶层目录名
ZIP_PREFIX = "yai-tracelab"

# ---------------------------------------------------------------------------
# 白名单：只有明确列出的路径才会被复制。白名单是"允许什么"，不是"排除什么"。
# ---------------------------------------------------------------------------

#: 白名单：单个文件（相对项目根）
ALLOW_FILES: tuple[str, ...] = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "pyproject.toml",
    ".gitignore",
    ".gitattributes",
    "docs/method.md",
    "docs/architecture.md",
    # 作品索引：交付物 / 评审维度 / 加分项 → 文件与命令（只含路径与命令，不含数据内容）
    "docs/submission-index.md",
    # 单文件作品说明（平台若只允许传一个文件时的兜底）
    "docs/作品说明.md",
    # 打开即用体验：双击脚本 + 静态体验页（只含自造样例数据）
    "启动体验.cmd",
    "docs/demo.html",
    # 消融报告（README 引用了它；只含聚合数字，不含逐 Case 结果）
    "docs/ablation-report.md",
    # 审计与修复记录：证明"发现问题 → 修复 → 回归"的闭环（评审可复核）
    "docs/audit-2026-11-04-fixes.md",
)

#: 白名单：整目录（相对项目根）。**默认**就进包的部分。
ALLOW_DIRS: tuple[str, ...] = (
    "tracelab",
    "tests",
    "scripts",
    "data/demo",
)

#: 需要显式开关才进包的目录。`regression/` 是 Case 级私有快照（由官方数据归纳），
#: 默认不随包分发；要交付回归证据时加 `--include-regression`。
OPTIONAL_DIRS: tuple[str, ...] = (
    "regression",
)

#: 白名单：赛事资料包内**明确列出**的交付文件。只有 --include-official-data 时才会用到。
#: 刻意不含 PDF 手册与厂商说明文档：它们不是复现所必需的，且属于主办方材料。
ALLOW_OFFICIAL_FILES: tuple[str, ...] = (
    "data/official/MANIFEST.sha256",
    "data/official/student-1.0/01_竞赛版流程字典.json",
    "data/official/student-1.0/02_公开训练轨迹.jsonl",
    "data/official/student-1.0/03_公开训练参考标注.jsonl",
    "data/official/student-1.0/04_无标注验证轨迹.jsonl",
    "data/official/student-1.0/05_提交结果示例.json",
    "data/official/student-1.0/06_提交格式.schema.json",
    "data/official/student-1.0/07_数据字典与业务规则.md",
    "data/official/student-1.0/08_提交格式校验.py",
    "data/official/student-1.0/09_模拟客户与商品档案.json",
    "data/official/student-1.0/10_版本评测报告模板.md",
)

# ---------------------------------------------------------------------------
# 拒绝名单：无论白名单怎么写，命中这些的一律不复制（也会在打包后复扫一次）。
# ---------------------------------------------------------------------------

#: 目录名命中即整棵子树不进入包
DENY_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", ".github", ".idea", ".vscode",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "build", "dist", "out", ".tmp", "node_modules",
})

#: 文件名（小写后）匹配任一 fnmatch 模式即拒绝
DENY_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.log",
    "*.pyc",
    "*.pyo",
    ".coverage",
    "coverage.xml",
)

#: 文件名含这些子串（小写后）即拒绝。
#: 2026-11-04 补 `password` / `pwd`：此前只挡 `passwd`，`password_store.py`、
#: `db_pwd.txt` 这类命名会整条漏过去（C-13）。
DENY_NAME_SUBSTRINGS: tuple[str, ...] = (
    "credential", "secret", "token", "passwd", "password", "pwd", "apikey", "api_key",
)

#: 包内允许出现的"生成物"（不是复制进去的，而是脚本自己产出的）
GENERATED_NAMES = frozenset({"提交清单.md", "提交说明.md", "SHA256SUMS.txt", ".tracelab-bundle.json"})
GENERATED_DIRS = ("评测产出/",)

#: 构建标记：删除一个非空输出目录前，必须用它证明"这目录确实是本脚本产出的"。
#: 只凭目录里有没有 `submission/` 判断归属是**不安全**的（普通目录也可能有同名子目录）。
MARKER_FILENAME = ".tracelab-bundle.json"
GENERATOR_NAME = "build_submission_bundle"
GENERATOR_VERSION = "2.0.0"
PROJECT_ID = "yai-tracelab"

#: 包里必须有的文档（缺一即失败）
REQUIRED_DOCS = ("README.md", "docs/method.md", "docs/architecture.md", "LICENSE", "NOTICE", "pyproject.toml")

# ---------------------------------------------------------------------------
# 敏感串扫描规则。**标签里刻意不写原始标记**：本脚本自身会被打进包并回读扫描，
# 标签带原文会让「包内安全扫描」自我命中。GitHub 令牌前缀也用字符类/分片拼写。
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    # GitHub：经典令牌（"gh" + 一个前缀字母 p/o/u/s/r + 下划线）
    # 与 fine-grained 令牌（"github" + 下划线 + "pat" + 下划线）。**不写完整前缀**：
    # 本脚本会被打包回读扫描，注释里连写会自我命中。
    ("GitHub 令牌前缀", re.compile(rb"gh" rb"[pousr]_|github" rb"_pat_")),
    ("AWS 访问密钥 ID", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("私钥 PEM 头", re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("明文口令赋值", re.compile(rb"password\s*=", re.IGNORECASE)),
    # OpenAI 两代前缀：`sk-<base62>` 与 `sk-proj-<...>`；`ant-` 一并覆盖（Anthropic 那条仍保留，
    # 命中会重复报告一条，属预期）。分片拼写：连写会让本脚本被打包回读时自我命中。
    ("OpenAI 风格密钥", re.compile(rb"sk-" rb"(?:proj-|ant-)?" rb"[A-Za-z0-9_-]{20,}")),
    # 口令/密钥赋值：passwd / pwd / db_password / secret_key 后跟 : 或 =
    # 分片拼写：本脚本自身会被打包回读，连写会让「包内安全扫描」自我命中。
    ("口令/密钥赋值", re.compile(
        rb"(?:" rb"passwd|pwd|db_password|secret_key" rb")\s*[:=]", re.IGNORECASE
    )),
    # 下面几条同样按"字符类/分片"拼写：本脚本自己会被打进包并回读扫描，
    # 标签或正则里连写完整前缀会让「包内安全扫描」自我命中。
    ("Anthropic 风格密钥", re.compile(rb"sk-" rb"ant-[A-Za-z0-9_-]{20,}")),
    ("Google API Key", re.compile(rb"AI" rb"za[0-9A-Za-z_-]{35}")),
    ("GitLab 令牌前缀", re.compile(rb"gl" rb"pat-[A-Za-z0-9_-]{20,}")),
    ("Slack 令牌前缀", re.compile(rb"xo" rb"x[baprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe 密钥", re.compile(rb"(?:sk|rk)_" rb"live_[A-Za-z0-9]{20,}")),
    ("HuggingFace 令牌", re.compile(rb"hf" rb"_[A-Za-z0-9]{30,}")),
    # Authorization: Bearer <token>（只看头部，不尝试解析 JWT 语义）
    ("Bearer 令牌", re.compile(rb"[Bb]ea" rb"rer\s+[A-Za-z0-9._~+/=-]{20,}")),
    # JWT：三段 base64url，段间以 . 分隔
    ("JWT", re.compile(rb"e" rb"yJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
)

#: 同一批规则的**文本版**：UTF-16LE 文本里 ASCII 会被插进 \x00，
#: 字节正则匹配不到（C-20）。这里按"强制 UTF-16LE 再扫一次"补齐，
#: 只在内容看起来像文本时启用，避免二进制噪声误报。
SENSITIVE_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern.pattern.decode("utf-8"), pattern.flags))
    for label, pattern in SENSITIVE_PATTERNS
)

#: 官方校验器成功时打印的这句话（中文，正是编码问题的高发点）。
#: 与 `scripts/run_official_validator.py` 的 SUCCESS_MARKER 一致；包装层不可用时的兜底。
OFFICIAL_SUCCESS_MARKER = "格式检查通过"
ENCODING_FAILURE_MARKERS: tuple[str, ...] = ("UnicodeEncodeError", "charmap", "codec can't encode")

#: Windows 扩展长度路径前缀：`\\?\` 会让 `os.path` 的规范化比较失效
#: （项目根/主目录判定可能被绕开），一律拒绝而不是"规范化后再比"。
EXTENDED_PATH_PREFIXES: tuple[str, ...] = ("\\\\?\\", "//?/", "\\\\.\\", "//./")
#: Windows 8.3 短名（`RUNNER~1`）：短名与长名指向同一目录但字符串不同，
#: realpath 之后仍残留说明它没被解析成真实路径，同样拒绝。
SHORT_NAME_MARKER = re.compile(r"~\d")
#: ZIP 条目里的盘符前缀（`C:` 绝对或相对）与 UNC（`//server`）：解压时会落到盘符路径。
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")


class UnsafeOutputError(RuntimeError):
    """输出目录未通过安全校验。抛出前不会删除、也不会写入任何文件。"""


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CopyPlan:
    """白名单复制计划（只读计算，不做任何写入）。"""

    files: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ScanReport:
    """一次包内安全扫描的结果。"""

    scanned: int
    problems: list[str]

    @property
    def clean(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class BundleOptions:
    """一次打包的全部输入（便于测试直接构造）。"""

    root: Path
    out_root: Path
    allow_outside: bool = False
    include_official: bool = False
    #: 是否把 `regression/`（Case 级私有快照）打进包。默认不打，必须显式开关。
    include_regression: bool = False
    skip_tests: bool = False
    #: 只做「路径校验 + 白名单复制 + 安全扫描」，跳过评测类自检。
    #: 仅用于路径/白名单/扫描的自动化测试（评测类自检依赖真实仓库与资料包）。
    skip_checks: bool = False


# ---------------------------------------------------------------------------
# 路径工具（区分大小写差异只在 Windows 上生效；全部只读）
# ---------------------------------------------------------------------------


def _norm(path: Path) -> str:
    """规范化用于比较的字符串：Windows 上大小写不敏感、分隔符统一。"""
    return os.path.normcase(os.path.normpath(str(path)))


def _same_path(a: Path, b: Path) -> bool:
    return _norm(a) == _norm(b)


def _is_same_or_under(path: Path, parent: Path) -> bool:
    """path 等于 parent 或位于 parent 之下（按规范化后的路径比较）。"""
    key = _norm(parent).rstrip(os.sep)
    other = _norm(path).rstrip(os.sep)
    if other == key:
        return True
    return other.startswith(key + os.sep)


def _is_ancestor(path: Path, of: Path) -> bool:
    """path 是 of 的祖先（不含 path == of）。"""
    return not _same_path(path, of) and _is_same_or_under(of, path)


def _is_link(path: Path) -> bool:
    """symlink 或 Windows junction / 其它重解析点。"""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    try:
        stat_result = path.stat(follow_symlinks=False)
    except OSError:
        return False
    # FILE_ATTRIBUTE_REPARSE_POINT（Windows）；POSIX 上该字段不存在，恒为 0
    return bool(getattr(stat_result, "st_file_attributes", 0) & 0x400)


def find_path_link(path: Path, *, trusted_root: Path | None = None) -> Path | None:
    """返回 path 自身或其父链上第一个符号链接，没有则返回 None。

    ``trusted_root`` 及其祖先上的链接不计入：仓库本身可能就放在链接目录下
    （例如 Linux 上 /var 指向 /private/var），这层链接不影响输出目录的身份。
    真正危险的是"项目根以下"或"仓库之外"那一段被链接改道。
    """
    trusted: set[str] = set()
    if trusted_root is not None:
        trusted.add(_norm(trusted_root))
        trusted.update(_norm(parent) for parent in trusted_root.parents)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if _norm(current) in trusted:
            continue
        if _is_link(current):
            return current
    return None


def _reject_extended_prefix(raw: Path) -> None:
    """拒绝 Windows 扩展长度/设备路径前缀（`\\\\?\\`、`//?/`、`\\\\.\\`、`//./`）。

    这类前缀不能让 `os.path.realpath` 可靠地归一化（也不该被归一化后接受）：
    历史版本只把 `raw` 交给 `abspath`，`--out "\\\\?\\<项目根>"` 与项目根字符串不相等，
    于是"项目根本身"的判定被绕开（C-16，Lead 已复现）。
    """
    text = str(raw)
    for prefix in EXTENDED_PATH_PREFIXES:
        if text.startswith(prefix):
            raise UnsafeOutputError(
                f"拒绝使用 Windows 扩展长度/设备路径前缀（{prefix!r}）：{text}\n"
                "    这类路径会绕过路径规范化比较（项目根/主目录判定会失效），请改用普通绝对路径"
            )


def _reject_short_name_segments(raw: Path) -> None:
    """拒绝 `realpath` 之后仍残留 8.3 短名（`~` + 数字）的路径段。

    存在的短名路径会先被 `realpath` 展开成长名（GitHub Windows runner 的
    `C:\\Users\\RUNNER~1\\...` 因此仍然可用）；展开不掉说明这一段不存在或无法解析，
    此时长短名指向同一目标的断言不成立，一律拒绝。
    """
    for part in Path(str(raw)).parts:
        if SHORT_NAME_MARKER.search(part):
            raise UnsafeOutputError(
                f"拒绝含 8.3 短名的路径段（{part!r}）：{raw}\n"
                "    短名与长名可能指向同一目录但字符串不同，会绕过目录归属判定"
            )


def validate_output_dir(out: Path, root: Path, *, allow_outside: bool = False) -> Path:
    """校验并规范化输出目录；不安全时抛 `UnsafeOutputError`。

    判定顺序（先形式、再解析、再看链接/身份、最后看范围），全程只读：
    0. `\\\\?\\`/`//?/` 等扩展长度前缀、8.3 短名 → 拒绝（路径形式本身不可信）
    1. 路径上任意一级是符号链接/目录联接 → 拒绝（没有覆盖开关，链接会改道删除目标）
    2. 文件系统根（`C:\\`、`/`）→ 拒绝
    3. 项目根目录本身 → 拒绝
    4. 项目根的祖先目录（`--out ..`、`--out E:\\Competition`）→ 拒绝
    5. 用户主目录及其祖先 → 拒绝
    6. 不在 `<项目根>/dist` 及其子目录内 → 需要 allow_outside 显式许可

    返回的是 `realpath` 之后的绝对路径（`..` 已消解、链接已被拒绝）。
    """
    root = Path(os.path.realpath(str(root)))
    raw = out if out.is_absolute() else root / out
    # 形式检查必须在 realpath 之前：`\\?\` 前缀会被规范化掉，之后就"看不见"了
    _reject_extended_prefix(raw)
    # 链接检查也必须在 realpath 之前：realpath 会把链接解析掉，之后就看不出它存在过
    link = find_path_link(raw, trusted_root=root)
    if link is not None:
        raise UnsafeOutputError(
            f"输出路径上存在符号链接/目录联接：{link}\n"
            "    链接会让删除目标变成不确定的位置，请改用真实目录"
        )
    # 关键修复（C-16）：raw 也走 realpath，与 root/home 用同一套解析结果比较
    raw = Path(os.path.realpath(str(raw)))
    _reject_short_name_segments(raw)

    if _same_path(raw, Path(raw.anchor)):
        raise UnsafeOutputError(f"拒绝删除文件系统根目录：{raw}")
    if _same_path(raw, root):
        raise UnsafeOutputError(f"拒绝删除项目根目录本身：{raw}")
    if _is_ancestor(raw, root):
        raise UnsafeOutputError(f"拒绝删除项目根目录的祖先目录：{raw}\n    项目根是：{root}")

    home = Path(os.path.realpath(str(Path.home())))
    if _same_path(raw, home) or _is_ancestor(raw, home):
        raise UnsafeOutputError(f"拒绝删除用户主目录或其祖先：{raw}")

    default_out = root / DEFAULT_OUT_SUBDIR
    if _is_same_or_under(raw, default_out):
        return raw
    if not allow_outside:
        raise UnsafeOutputError(
            f"输出目录不在默认允许范围内：{raw}\n"
            f"    默认只允许 {default_out} 及其子目录；\n"
            "    确认该目录可以被清空后，再加 --allow-outside 重跑"
        )
    return raw


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _looks_like_bundle_output(directory: Path) -> bool:
    """**只读诊断**：目录内容是否像本脚本的产出。

    注意：这个函数**不能**作为删除许可。历史版本用它判断"能不能清空"，
    导致任何含 `submission/` 的普通目录都会被删除。删除许可一律走 `marker_mismatch()`。
    """
    return (directory / MARKER_FILENAME).is_file()


def _path_identity(path: Path) -> str:
    """规范化路径的指纹（Windows 大小写不敏感、反转义、去尾部分隔符）。"""
    return hashlib.sha256(_norm(path).encode("utf-8")).hexdigest()


def _output_manifest(out_root: Path) -> list[dict[str, object]]:
    """记录本次产出的**文件清单**（相对路径 + 大小 + sha256），不含标记文件本身。

    G6-0B：标记从此不只证明"这个目录是我的"，还能证明"里面正好是这些文件"。
    再次构建时据此判断「有没有人往里塞了东西」与「我记录的文件是否被改过/删掉」。
    """
    entries: list[dict[str, object]] = []
    for path in sorted(out_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out_root)
        if rel.name == MARKER_FILENAME and len(rel.parts) == 1:
            continue
        entries.append({
            "path": rel.as_posix(),
            "size": path.stat().st_size,
            "sha256": _file_sha256(path),
        })
    return entries


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_sums(bundle: Path) -> list[str]:
    """重写 SHA256SUMS（覆盖除自身外的全部文件，含结论页/提交说明）。"""
    sums = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            sums.append(f"{_file_sha256(path)}  {path.relative_to(bundle).as_posix()}")
    (bundle / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return sums


def write_marker(
    bundle: Path,
    root: Path,
    out_root: Path,
    *,
    include_official: bool,
    files: list[dict[str, object]] | None = None,
) -> Path:
    """在 bundle 根写构建标记：证明"这是本项目、在这个输出目录、由本脚本产出"。

    ``files`` 为本次产出的文件清单；构建开始时写空清单（表示"尚未完成"），
    构建结束时再写一次真实清单。中途崩溃的目录不会被下一次构建误当成"可清空"。
    """
    payload = {
        "project": PROJECT_ID,
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "source_root": _path_identity(root),
        "output_dir": _path_identity(out_root),
        "created_at": _now_iso(),
        "include_official_data": bool(include_official),
        "files": list(files) if files is not None else [],
    }
    path = bundle / MARKER_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_marker(directory: Path) -> dict[str, object] | None:
    path = directory / MARKER_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def marker_mismatch(directory: Path, root: Path, out_root: Path) -> str | None:
    """返回 None 表示"标记有效、身份匹配、内容与清单一致"，可以安全删除；否则返回拒绝原因。

    校验顺序：身份（谁产出的）→ 清单完整（有没有被塞东西）→ 内容一致（记录的文件是否原样）。
    任何一项不满足都**不改动目录**——拒绝重建而不是"先删了再说"。
    """
    marker = read_marker(directory)
    if marker is None:
        return f"缺少有效构建标记 {MARKER_FILENAME}（不认识的目录一律不清空）"
    if marker.get("project") != PROJECT_ID:
        return f"标记里的项目身份不匹配：{marker.get('project')!r} != {PROJECT_ID!r}"
    if marker.get("generator") != GENERATOR_NAME:
        return f"标记里的生成器不匹配：{marker.get('generator')!r}"
    if marker.get("source_root") != _path_identity(root):
        return "标记里的项目根指纹不匹配（这个目录不是由当前项目产出的）"
    if marker.get("output_dir") != _path_identity(out_root):
        return "标记里的输出目录指纹不匹配（目录被移动或复制过）"

    recorded = marker.get("files")
    if not isinstance(recorded, list) or not recorded:
        return (
            "标记里没有文件清单（旧版本生成，或上次构建未完成），无法证明目录内容属于本脚本；"
            "请换一个空目录重跑"
        )

    expected: dict[str, str] = {}
    for item in recorded:
        if not isinstance(item, dict) or "path" not in item or "sha256" not in item:
            return "标记里的文件清单格式无效"
        expected[str(item["path"])] = str(item["sha256"])

    actual = {str(entry["path"]): str(entry["sha256"]) for entry in _output_manifest(directory)}
    extra = sorted(set(actual) - set(expected))
    if extra:
        return f"目录里出现标记清单以外的文件（共 {len(extra)} 个，例如 {extra[0]}），保持目录原样"
    missing = sorted(set(expected) - set(actual))
    if missing:
        return f"标记记录的文件已不存在（共 {len(missing)} 个，例如 {missing[0]}），保持目录原样"
    changed = sorted(name for name in expected if expected[name] != actual[name])
    if changed:
        return f"标记记录的文件内容已变化（共 {len(changed)} 个，例如 {changed[0]}），停止重建"
    return None


def prepare_output_dir(out_root: Path, *, root: Path) -> None:
    """清空并重建输出目录。只能在 `validate_output_dir` 通过之后调用。

    删除非空目录的唯一许可：`marker_mismatch()` 返回 None（标记存在且双重身份匹配）。
    路径校验 —— 标记校验 —— 复查链接 —— rmtree，顺序固定，任何一步失败都不改动文件。
    """
    if out_root.exists():
        if not out_root.is_dir():
            raise UnsafeOutputError(f"输出路径已存在且不是目录：{out_root}")
        if any(out_root.iterdir()):
            reason = marker_mismatch(out_root, root, out_root)
            if reason is not None:
                raise UnsafeOutputError(
                    f"目标目录非空且未通过构建标记校验，拒绝清空：{out_root}\n"
                    f"    原因：{reason}\n"
                    "    请换一个空目录；确认可清空时请手工删除后重跑（脚本不会代替你做这个决定）"
                )
            # 复查：校验之后目录可能被替换成链接（TOCTOU 收敛点，做不到原子操作但尽量窄）
            if _is_link(out_root):
                raise UnsafeOutputError(f"输出目录在校验后变成了链接，拒绝清空：{out_root}")
            if _norm(Path(os.path.realpath(str(out_root)))) != _norm(out_root):
                raise UnsafeOutputError(f"输出目录在校验后发生路径解析变化，拒绝清空：{out_root}")
            if marker_mismatch(out_root, root, out_root) is not None:
                raise UnsafeOutputError(f"输出目录标记在复查时失效，拒绝清空：{out_root}")
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)


# ---------------------------------------------------------------------------
# 白名单复制
# ---------------------------------------------------------------------------


def denial_reason(
    rel: Path, *, include_official: bool, include_regression: bool = False
) -> str | None:
    """逐段判断相对路径是否被拒绝；None 表示允许。

    入口先把路径 `normpath`（消解 `.`/`..` 与重复分隔符）再逐段 `normcase`：
    ZIP 条目或人工构造的 `tracelab/../data/official/...` 若不先归一化，
    `parts[0] == "data"` 的前缀判断会漏掉它（C-13）。
    """
    normalized = os.path.normpath(str(rel))
    parts = Path(normalized).parts
    if not parts or parts == (os.curdir,):
        return "空路径"
    if len(parts) > 1 and parts[0] == "data" and parts[1] == "official" and not include_official:
        return "赛事资料包默认不打包（需要 --include-official-data 显式允许）"
    if parts[0] in OPTIONAL_DIRS and not include_regression:
        return "私有回归快照默认不打包（需要 --include-regression 显式允许）"
    for part in parts:
        # normcase 在 Windows 上等价于小写；POSIX 上是恒等。再叠加 lower 保证跨平台一致。
        low = os.path.normcase(part).lower()
        if low in DENY_DIR_NAMES:
            return f"命中拒绝名单目录：{part}"
        for pattern in DENY_NAME_PATTERNS:
            if fnmatch.fnmatch(low, pattern):
                return f"文件名匹配拒绝模式：{pattern}"
        for word in DENY_NAME_SUBSTRINGS:
            if word in low:
                return f"文件名含敏感词：{word}"
    return None


def plan_copy(
    root: Path, *, out_root: Path, include_official: bool, include_regression: bool = False
) -> CopyPlan:
    """按白名单算出要复制哪些文件（只读，不写盘）。"""
    candidates: list[Path] = []
    skipped_dirs: list[tuple[str, str]] = []

    for rel in ALLOW_FILES:
        candidate = root / rel
        if candidate.is_file():
            candidates.append(candidate)
    allowed_dirs = ALLOW_DIRS + (OPTIONAL_DIRS if include_regression else ())
    for rel in allowed_dirs:
        base = root / rel
        if base.is_file():
            candidates.append(base)
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            here = Path(dirpath)
            kept: list[str] = []
            for name in sorted(dirnames):
                child = here / name
                rel = child.relative_to(root)
                reason = "目录是符号链接" if _is_link(child) else denial_reason(
                    rel, include_official=include_official, include_regression=include_regression
                )
                if reason is not None:
                    # 整棵子树一起记一条，避免清单里堆满 __pycache__/*.pyc
                    skipped_dirs.append((rel.as_posix(), reason))
                    continue
                kept.append(name)
            dirnames[:] = kept
            candidates.extend(here / name for name in sorted(filenames))
    if include_official:
        for rel in ALLOW_OFFICIAL_FILES:
            candidate = root / rel
            if candidate.is_file():
                candidates.append(candidate)

    files: list[Path] = []
    skipped: list[tuple[str, str]] = sorted(skipped_dirs)
    seen: set[str] = set()
    for path in candidates:
        rel = path.relative_to(root)
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        reason = denial_reason(rel, include_official=include_official, include_regression=include_regression)
        if reason is None and _is_link(path):
            reason = "文件是符号链接（可能指向包外内容）"
        if reason is None and _is_same_or_under(path, out_root):
            reason = "位于输出目录内（避免产出把自己再扫进包）"
        if reason is not None:
            skipped.append((rel.as_posix(), reason))
            continue
        files.append(path)
    return CopyPlan(files=sorted(files), skipped=skipped)


def copy_plan(root: Path, bundle: Path, plan: CopyPlan) -> list[str]:
    """执行复制，返回包内相对路径列表。"""
    copied: list[str] = []
    for path in plan.files:
        rel = path.relative_to(root)
        target = bundle / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())
    return copied


# ---------------------------------------------------------------------------
# 打包后安全扫描
# ---------------------------------------------------------------------------


def _utf16le_text(data: bytes) -> str | None:
    """把疑似 UTF-16LE 文本解出来；不像文本（或不是 UTF-16LE）则返回 None。

    典型 UTF-16LE 文本的 NUL 占比很高；解码后若出现大量 C0 控制字符说明是二进制，
    直接放弃——宁可漏扫二进制（清单里写明不在覆盖范围），也不要噪声误报。
    """
    if b"\x00" not in data or len(data) % 2:
        return None
    try:
        text = data.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    control = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    if control > max(1, len(text) // 100):
        return None
    return text


def _content_hits(data: bytes) -> list[str]:
    """内容敏感串命中：先按原始字节扫，再把 UTF-16LE 文本解出来重扫一次。"""
    hits = [f"{label}" for label, pattern in SENSITIVE_PATTERNS if pattern.search(data)]
    text = _utf16le_text(data)
    if text is not None:
        hits += [
            f"{label}（UTF-16LE）"
            for label, pattern in SENSITIVE_TEXT_PATTERNS
            if pattern.search(text)
        ]
    return hits


def scan_paths(
    base: Path, *, include_official: bool, include_regression: bool = False
) -> ScanReport:
    """扫描展开后的包目录：文件名拒绝名单 + 文件内容敏感串。"""
    problems: list[str] = []
    scanned = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        rel = path.relative_to(base)
        reason = denial_reason(rel, include_official=include_official, include_regression=include_regression)
        if reason is not None:
            problems.append(f"文件名命中拒绝名单：{rel.as_posix()}（{reason}）")
        data = path.read_bytes()
        for label in _content_hits(data):
            problems.append(f"敏感内容命中：{rel.as_posix()}（{label}）")
    return ScanReport(scanned=scanned, problems=problems)


def _zip_entry_is_suspicious(name: str, inner: PurePosixPath) -> str | None:
    """ZIP 条目路径检查：绝对路径、反斜杠、`..`、盘符前缀、UNC。"""
    if name.startswith("/") or "\\" in name or ".." in inner.parts:
        return "ZIP 条目路径可疑"
    if DRIVE_PREFIX.match(name) or name.startswith("//"):
        # `C:/x` 是绝对；`C:x` 是**盘符相对**（解压会落到该盘的当前目录）——两者都不接受。
        return "ZIP 条目含盘符/UNC 前缀"
    return None


def scan_zip(
    zip_path: Path, *, include_official: bool, include_regression: bool = False
) -> ScanReport:
    """重新打开生成的 zip 扫描：条目名、条目内容、是否夹带 data/official。"""
    problems: list[str] = []
    scanned = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            scanned += 1
            inner = PurePosixPath(name)
            suspicious = _zip_entry_is_suspicious(name, inner)
            if suspicious is not None:
                problems.append(f"{suspicious}：{name}")
            rel = Path(*inner.parts[1:]) if len(inner.parts) > 1 else Path(inner.name)
            reason = denial_reason(
                rel, include_official=include_official, include_regression=include_regression
            )
            if reason is not None:
                problems.append(f"ZIP 内文件名命中拒绝名单：{name}（{reason}）")
            data = archive.read(info)
            for label in _content_hits(data):
                problems.append(f"ZIP 内敏感内容命中：{name}（{label}）")
    return ScanReport(scanned=scanned, problems=problems)


# ---------------------------------------------------------------------------
# 自检（评测类，依赖真实仓库与资料包）
# ---------------------------------------------------------------------------


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """执行自检子进程，并**强制 UTF-8 stdio**。

    审计 R4-12：CI 全局设了 PYTHONUTF8=1，但本地非 CJK Windows（cp1252）跑打包时，
    子进程打印中文会 UnicodeEncodeError → 所有评测检查失败。这里统一注入编码环境，
    让"本地能过/CI 能过"是同一件事。
    """
    return _run_with_utf8(cmd, cwd)


def _run_with_utf8(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONLEGACYWINDOWSSTDIO": "0"})
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""


def _load_sibling_module(filename: str, module_name: str) -> ModuleType | None:
    """从本脚本同目录载入一个兄弟模块（打包后 scripts/ 一起进包，路径稳定）。"""
    path = Path(__file__).resolve().parent / filename
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(module_name, module)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - 包装层坏掉时退回兜底，不让打包整体崩
        return None


def _run_official_validator(validator: Path, cases: Path, submission: Path) -> tuple[bool, str]:
    """调用官方校验器：**必须**走 `-X utf8` + 语义断言（C-36）。

    官方 `08_提交格式校验.py` 会直接 print 中文；在非 UTF-8 控制台下它会
    `UnicodeEncodeError`，看起来像"格式不合法"。历史版本在这里直接
    `subprocess.run([python, validator, ...])`，等于绕过了 `run_official_validator.py`
    已经做好的编码安全包装。这里复用兄弟模块的 `run_validation` / `classify`；
    模块缺失时才退回等价兜底（同样带 `-X utf8` + 成功标记断言）。
    """
    wrapper = _load_sibling_module("run_official_validator.py", "tracelab_official_validator")
    if wrapper is not None and hasattr(wrapper, "run_validation") and hasattr(wrapper, "classify"):
        returncode, output = wrapper.run_validation(validator, cases, submission)
        return wrapper.classify(returncode, output)
    return _run_official_validator_fallback(validator, cases, submission)


def _run_official_validator_fallback(
    validator: Path, cases: Path, submission: Path
) -> tuple[bool, str]:
    """包装模块不可用时的兜底：仍然强制 UTF-8，并且不只看退出码。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(validator),
         "--cases", str(cases), "--submission", str(submission)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if any(marker in output for marker in ENCODING_FAILURE_MARKERS):
        return False, "编码故障：官方脚本输出无法在当前环境编码（不是校验结论）"
    if proc.returncode == 0 and OFFICIAL_SUCCESS_MARKER in output:
        return True, "通过"
    if proc.returncode == 0:
        return False, "退出码为 0 但未出现成功标记，判定不可信"
    return False, f"校验失败（退出码 {proc.returncode}）"


def _evaluation_checks(root: Path, bundle: Path, *, skip_tests: bool) -> list[Check]:
    """跑真实仓库存量自检：评测、官方格式校验、报告、指标、回归、测试、数据哈希。"""
    data_dir = root / "data" / "official" / "student-1.0"
    train_cases = data_dir / "02_公开训练轨迹.jsonl"
    train_labels = data_dir / "03_公开训练参考标注.jsonl"
    valid_cases = data_dir / "04_无标注验证轨迹.jsonl"
    validator = data_dir / "08_提交格式校验.py"
    outputs = bundle / "评测产出"
    checks: list[Check] = []

    # 1) 训练集评测
    eval_train = run([sys.executable, "-m", "tracelab", "evaluate",
                      "--cases", str(train_cases), "--labels", str(train_labels),
                      "--out", str(outputs / "train")], root)
    checks.append(Check("训练集评测可运行", eval_train.returncode == 0,
                        _first_line(eval_train.stdout or eval_train.stderr)))

    # 2) 无标注验证集评测
    eval_valid = run([sys.executable, "-m", "tracelab", "evaluate",
                      "--cases", str(valid_cases), "--out", str(outputs / "valid")], root)
    checks.append(Check("验证集评测可运行", eval_valid.returncode == 0,
                        _first_line(eval_valid.stdout or eval_valid.stderr)))

    # 3) 官方格式校验（两套都必须通过；走编码安全包装层 + 语义断言）
    fmt_ok = True
    fmt_detail: list[str] = []
    for name, cases in (("train", train_cases), ("valid", valid_cases)):
        passed, reason = _run_official_validator(
            validator, cases, outputs / name / "submission.jsonl"
        )
        fmt_ok = fmt_ok and passed
        fmt_detail.append(f"{name}: {reason}")
    checks.append(Check("官方提交格式校验通过", fmt_ok, "；".join(fmt_detail)))

    # 4) 官方报告模板 7 节
    official = outputs / "train" / "report-official.md"
    heads = [line for line in official.read_text(encoding="utf-8").splitlines() if line.startswith("## ")] if official.exists() else []
    checks.append(Check("版本评测报告符合官方模板 7 节", len(heads) == 7, f"实际 {len(heads)} 节"))

    # 5) 方法指标
    metrics_path = outputs / "train" / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))["values"] if metrics_path.exists() else {}
    expected = {
        "agent_task_success_accuracy": 1.0,
        "business_invoice_created_accuracy": 1.0,
        "version_direction_accuracy": 1.0,
        "first_fault_event_exact_match": 1.0,
        "root_cause_label_macro_f1": 1.0,
        "evidence_id_validity": 1.0,
    }
    bad = {key: metrics.get(key) for key, value in expected.items() if metrics.get(key) != value}
    checks.append(Check("训练集六项方法指标达标", not bad,
                        f"未达标：{bad}" if bad else "六项均为 1.0（拟合值，已在报告中声明）"))

    # 6) 回归闭环复测。`regression/` 默认不进包（Case 级私有快照），
    # 此时用仓库里的快照复测，保证"回归证据"这项检查仍然是真的执行过。
    pack_in_bundle = bundle / "regression" / "invoice-pack" / "regression-pack.json"
    pack = pack_in_bundle if pack_in_bundle.exists() else root / "regression" / "invoice-pack" / "regression-pack.json"
    verify = run([sys.executable, "-m", "tracelab", "regression", "verify",
                  "--pack", str(pack), "--cases", str(train_cases),
                  "--out", str(outputs / "regression-verify")], root)
    pack_where = "包内快照" if pack_in_bundle.exists() else "仓库快照（未随包分发）"
    # 审计 A4：只看返回码会把"[WARN] 快照版本落后"记成通过。带 WARN 的复测不算零漂移，
    # 必须让提交包自检失败（否则评审看到的是 ✅ 后面挂一行 WARN）。
    combined = (verify.stdout or "") + (verify.stderr or "")
    checks.append(Check("回归闭环复测零漂移",
                        verify.returncode == 0 and "[WARN]" not in combined,
                        f"{pack_where}；{_first_line(verify.stdout)}"
                        + ("；存在 WARN（如快照版本落后），需重建快照" if "[WARN]" in combined else "")))

    # 7) 测试套件
    if skip_tests:
        checks.append(Check("测试套件", True, "已跳过（--skip-tests）"))
    else:
        pytest_proc = run([sys.executable, "-m", "pytest", "tests", "-q"], root)
        tail = (pytest_proc.stdout or "").strip().splitlines()[-1] if pytest_proc.stdout else ""
        checks.append(Check("测试套件全部通过", pytest_proc.returncode == 0, tail))

    # 8) 数据完整性
    data_proc = run([sys.executable, "scripts/ci_verify.py", "data"], root)
    checks.append(Check("原始数据哈希未变", data_proc.returncode == 0,
                        (data_proc.stdout or "").strip().splitlines()[-1] if data_proc.stdout else ""))
    return checks


def _leftovers_check(bundle: Path) -> Check:
    """包内不得出现缓存/产出目录（只看包内相对路径，别把 dist/ 这层父目录算进来）。"""
    leftovers = [
        str(path.relative_to(bundle))
        for path in bundle.rglob("*")
        if path.is_file() and any(part in DENY_DIR_NAMES for part in path.relative_to(bundle).parts[:-1])
    ]
    return Check("包内无缓存/产出残留", not leftovers, f"残留：{leftovers[:5]}" if leftovers else "干净")


def _allowlist_only_check(bundle: Path, copied: list[str]) -> Check:
    """包内每个文件都必须来自白名单，或是脚本自己生成的产出。"""
    allowed = set(copied)
    extra = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle).as_posix()
        if rel in allowed or rel in GENERATED_NAMES or rel.startswith(GENERATED_DIRS):
            continue
        extra.append(rel)
    return Check("包内仅含白名单文件", not extra,
                 f"白名单外文件：{extra[:5]}" if extra else f"{len(allowed)} 个复制文件 + 评测产出")


def _official_check(bundle: Path, *, include_official: bool, root: Path | None = None) -> Check:
    official = sorted(
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.relative_to(bundle).parts[:2] == ("data", "official")
    )
    if include_official:
        # 审计 R2-12 + R4-07：不只校验"非空"，而是"源目录里存在的白名单文件必须全在包里"——
        # "11 个里只静默复制 1 个"同样会让评审按包内说明无法复现。
        expected = [
            rel for rel in ALLOW_OFFICIAL_FILES
            if root is None or (root / rel).is_file()
        ]
        missing = [rel for rel in expected if rel not in official]
        ok = bool(official) and not missing
        detail = f"已显式允许（--include-official-data），打包 {len(official)}/{len(expected)} 个白名单文件"
        if not official:
            detail = "❌ 声明打包赛事资料，但包内 data/official 为空（目录不存在或全部被拒绝名单拦下）"
        elif missing:
            detail += f"；缺失 {len(missing)} 个：{missing[:5]}"
        return Check("赛事资料包处理", ok, detail)
    return Check("赛事资料包处理", not official,
                 "未打包（默认保守：赛事授权范围不确定）" if not official else f"不应出现：{official[:3]}")


def _regression_check(bundle: Path, *, include_regression: bool) -> Check:
    """回归快照是 Case 级私有材料：默认不得随包分发，显式允许时才可进入。"""
    present = sorted(
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.relative_to(bundle).parts[0] == "regression"
    )
    if include_regression:
        # 审计 R4-07：回归快照必须真的可用——pack 文件要在包里，否则 `regression verify` 无从复测。
        pack_rel = "regression/invoice-pack/regression-pack.json"
        missing = [] if pack_rel in present else [pack_rel]
        ok = bool(present) and not missing
        detail = f"已显式允许（--include-regression），打包 {len(present)} 个文件（Case 级私有快照）"
        if missing:
            detail += f"；缺少 {missing}"
        return Check("回归快照处理", ok, detail)
    return Check("回归快照处理", not present,
                 "未打包（默认保守：Case 级私有快照）" if not present else f"不应出现：{present[:3]}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _scan_section(
    report: ScanReport, *, include_official: bool, include_regression: bool = False
) -> list[str]:
    name_hits = [item for item in report.problems if "拒绝名单" in item]
    content_hits = [item for item in report.problems if "敏感内容" in item]
    lines = [
        "## 包内安全扫描",
        "",
        f"- 扫描文件数：{report.scanned}",
        f"- 拒绝名单（文件名/目录名）：{'无命中' if not name_hits else f'命中 {len(name_hits)} 项'}",
        f"- 敏感串（内容）：{'无命中' if not content_hits else f'命中 {len(content_hits)} 项'}",
        # 规则清单从 SENSITIVE_PATTERNS 现取，避免清单文案与真实扫描漂移
        "- 扫描规则（" + str(len(SENSITIVE_PATTERNS)) + " 类）："
        + " / ".join(label for label, _ in SENSITIVE_PATTERNS),
        "- 扫描方式：同一批规则先扫原始字节，检出 UTF-16LE 文本时**再扫一次**",
        "- 覆盖范围：**二进制/压缩内容不在覆盖范围**（base64、加密/压缩载荷、"
        "图片语音等不保证命中）；ZIP 条目另查盘符/UNC 前缀",
        f"- 赛事资料包 `data/official`：{'已显式允许打包' if include_official else '未打包（默认保守）'}",
        f"- 回归快照 `regression/`：{'已显式允许打包' if include_regression else '未打包（默认保守：Case 级私有快照）'}",
        "",
    ]
    for item in report.problems:
        lines.append(f"- ❌ {item}")
    if report.problems:
        lines.append("")
    return lines


def _source_section(
    bundle: Path, *, include_official: bool, include_regression: bool = False
) -> list[str]:
    """来源清单：包内评测产出**实际是用哪份数据算出来的**（不靠人填）。

    数据不完整或读不到 manifest 时如实写"未能确认"，不假装有来源。
    """
    rows: list[str] = []
    for manifest_path in sorted(bundle.glob("评测产出/*/results.json")):
        group = manifest_path.parent.name
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            dataset = payload.get("manifest", {}).get("dataset", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            rows.append(f"| `{group}` | 未能确认（manifest 不可读） | — | — | — |")
            continue
        rows.append(
            "| `{group}` | `{source}` | `{origin}` | `{version}` | `{sha}` |".format(
                group=group,
                source=dataset.get("source", "?"),
                origin=dataset.get("data_origin", "?"),
                version=dataset.get("dataset_version", "?"),
                sha=(dataset.get("sha256") or "?")[:16],
            )
        )
    lines = [
        "## 来源清单",
        "",
        "| 评测产出 | 数据文件 | 数据来源标记 | 数据集版本 | 数据 SHA-256（前 16 位） |",
        "|---|---|---|---|---|",
    ]
    lines += rows if rows else ["| （本包未含评测产出） | — | — | — | — |"]
    lines += [
        "",
        f"- 官方原始数据是否随包分发：{'是（--include-official-data）' if include_official else '否（默认不打包）'}",
        f"- Case 级回归快照是否随包分发：{'是（--include-regression）' if include_regression else '否（默认不打包）'}",
        "- `评测产出/` 是**由数据生成的结果**，不是原始数据；官方数据授权范围不确定，故默认只交付代码与自检结论。",
        "",
    ]
    return lines


def _write_checklist(
    bundle: Path,
    *,
    checks: list[Check],
    ok: bool,
    out_root: Path,
    plan: CopyPlan,
    copied: list[str],
    include_official: bool,
    include_regression: bool = False,
    report: ScanReport,
) -> None:
    lines = [
        "# 初赛提交自检清单",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 输出目录：`{out_root}`",
        f"- 结论：**{'全部通过' if ok else '存在未通过项，不允许提交'}**",
        "",
        "| # | 检查项 | 结果 | 说明 |",
        "|---|---|---|---|",
    ]
    for index, check in enumerate(checks, 1):
        lines.append(f"| {index} | {check.name} | {'✅' if check.passed else '❌'} | {check.detail} |")

    lines += [
        "",
        "## 对应赛题要求的提交物",
        "",
        "| 赛题要求 | 位置 |",
        "|---|---|",
        "| 可运行的离线评测程序 | `tracelab/`（`python -m tracelab evaluate`） |",
        "| 源代码、运行环境与启动说明 | 本包全部源码 + `README.md`「30 秒跑起来」 |",
        "| 评测方法说明（评估逻辑/评分方式/判断依据） | `docs/method.md` |",
        "| 基于批量 Case 的版本评测报告 | `评测产出/train/report-official.md`（官方模板 7 节） |",
        "| 按要求生成的评测结果 | `评测产出/train/submission.jsonl`、`评测产出/valid/submission.jsonl` |",
        "",
        "## 打包白名单",
        "",
        "- 白名单文件：" + "、".join(f"`{item}`" for item in ALLOW_FILES),
        "- 白名单目录：" + "、".join(f"`{item}/`" for item in ALLOW_DIRS),
        "- 需显式开关的目录：" + "、".join(f"`{item}/`" for item in OPTIONAL_DIRS),
        f"- 实际复制：{len(copied)} 个文件；被拒绝名单/排除规则拦下 {len(plan.skipped)} 项",
    ]
    if plan.skipped:
        grouped: dict[str, list[str]] = {}
        for rel, reason in plan.skipped:
            grouped.setdefault(reason, []).append(rel)
        lines += ["", "| 未打包原因 | 数量 | 示例 |", "|---|---|---|"]
        for reason, items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
            sample = "、".join(f"`{item}`" for item in items[:3])
            more = f" 等 {len(items)} 项" if len(items) > 3 else ""
            lines.append(f"| {reason} | {len(items)} | {sample}{more} |")

    lines += [""]
    lines += _scan_section(report, include_official=include_official, include_regression=include_regression)
    lines += [
        "## 赛事资料包说明",
        "",
        "赛事主办方提供的 `data/official/` 授权范围不确定，**默认不打包**（保守处理）。",
        "需要把资料包一并交付时显式重跑：`--include-official-data`，此时只打包 "
        "`ALLOW_OFFICIAL_FILES` 明确列出的复现必需文件（不含 PDF 手册与厂商说明文档）。",
        "",
        "## 复现方式",
        "",
        "```bash",
        "python -m tracelab evaluate --cases data/official/student-1.0/02_公开训练轨迹.jsonl \\",
        "  --labels data/official/student-1.0/03_公开训练参考标注.jsonl --out out/train",
        "```",
        "",
        "> 若本包未含 `data/official/`，请先把主办方资料包放回 `data/official/student-1.0/`。",
        "",
    ]
    lines += _source_section(
        bundle, include_official=include_official, include_regression=include_regression
    )
    lines += [
        "> 本清单由 `scripts/build_submission_bundle.py` 实际执行生成，不是手填勾选。",
        "",
    ]
    (bundle / "提交清单.md").write_text("\n".join(lines), encoding="utf-8")


def _write_submission_note(
    bundle: Path,
    *,
    include_official: bool,
    include_regression: bool,
) -> None:
    """在包根写 `提交说明.md`：赛题交付物 → 包内路径 → 复现命令 → 数据边界。

    只写静态说明（不含任何评测结论数字，避免与实跑结果不一致）；
    逐项自检结果在 `提交清单.md`，这里只做"东西在哪、怎么跑、包里没有什么"。
    """
    official_line = (
        "- 本包**包含**赛事资料包中的复现必需文件（`data/official/` 白名单子集）。"
        if include_official
        else "- 本包**默认不含** `data/official/`（主办方资料包授权范围不确定，保守处理）；"
             "需要一并交付时用 `--include-official-data` 重新构建。"
    )
    regression_line = (
        "- 本包**包含** `regression/`（Case 级私有快照）。"
        if include_regression
        else "- 本包**默认不含** `regression/`（由官方数据归纳的 Case 级快照，不随包分发）；"
             "需要交付回归证据时用 `--include-regression` 重新构建。"
    )
    lines = [
        "# 提交说明（赛题三：AI 开票 Agent 离线轨迹评测）",
        "",
        "本包由 `scripts/build_submission_bundle.py` 实际执行生成；逐项自检结果见同目录 `提交清单.md`。",
        "",
        "## 一、交付物对照（赛题第 05 节）",
        "",
        "| 交付物 | 在包内哪里 | 说明 |",
        "|---|---|---|",
        "| 可运行离线评测程序 | `tracelab/` | 纯 Python，无第三方运行时依赖 |",
        "| 源代码 / 运行环境 / 启动说明 | `README.md`（+ `pyproject.toml`） | Python 3.11+；命令见第二节 |",
        "| 评测方法说明（逻辑 / 评分 / 依据） | `docs/method.md`（+ `docs/architecture.md`） | 规则表、扣分口径、三态语义与数据完整性 |",
        "| 多 Case 版本评测报告 + 评测结果 | **由命令生成**（包内不含 `out/`） | 资料包就位后跑第二节第 1 条 |",
        "",
        "评审导向的导航（交付物 / 评价维度 / 加分项分别对应哪个文件）见 `docs/submission-index.md`；",
        "**单文件版作品说明**见 `docs/作品说明.md`（平台若只允许上传一个文件，可直接交这一份）。",
        "",
        "## 二、复现命令（三分钟）",
        "",
        "```bash",
        "# 1) 批量评测：产出 submission.jsonl / results.json / 四份报告",
        "python -m tracelab evaluate --cases data/official/student-1.0/02_公开训练轨迹.jsonl \\",
        "  --labels data/official/student-1.0/03_公开训练参考标注.jsonl --out out/train",
        "",
        "# 2) 回归闭环复测（0 漂移即退出码 0）",
        "python -m tracelab regression verify --pack regression/invoice-pack/regression-pack.json \\",
        "  --cases data/official/student-1.0/02_公开训练轨迹.jsonl --out out/regression-verify",
        "",
        "# 3) 变形实验（全部自造样本，**不需要资料包**；固定种子可复现）",
        "python -m tracelab experiments --out out/experiments",
        "```",
        "",
        "> 第 3 条在任何环境都能跑：样本由 `tracelab/samples.py` 现场构造，不读取官方资料包。",
        "",
        "## 三、评测器版本与判定变化（重要）",
        "",
        "- 本包评测器版本：`tracelab-evaluator/0.3.0`；规则集版本：`rulepack-1.3`。",
        "- 相对 0.1.0 的三类判定变化（完整说明与复现命令见 `docs/audit-2026-11-04-fixes.md`）：",
        "  1. 非法/不可解析证据（金额格式非法、未知步骤名、缺少任一题内要求字段）从\"不报违规\"改为 `UNKNOWN`；",
        "  2. 聚合发布建议更保守：0 条运行判对、存在两版同错、或逐 Case 建议为 investigate 时不得给出 `release`；",
        "  3. 依据记录复核更严格：规则-判据绑定 + 事件按 `(seq, event_id)` 定序 + ref 白名单。",
        "- 历史回归快照（`tracelab-regression/2`）不受影响：官方训练集 28 条运行的判定未变化。",
        "",
        "## 四、本包的数据边界",
        "",
        official_line,
        regression_line,
        "- 不含 `out/` 评测产出，也不含任何逐 Case 的私有结果文件。",
        "- 包内自动扫描拒绝名单与敏感串（结果见 `提交清单.md` 的「包内安全扫描」一节）。",
        "",
        "## 五、完整性核验",
        "",
        "- `SHA256SUMS.txt`：包内每个文件的摘要，可直接 `sha256sum -c`。",
        "- `.tracelab-bundle.json`：构建标记（含上一次产出文件清单）；在已有目录上重建会先核对身份与内容，",
        "  出现清单外文件或被改动的文件时**拒绝重建并保持目录原样**，避免误删。",
        "",
    ]
    (bundle / "提交说明.md").write_text("\n".join(lines), encoding="utf-8")


def build_bundle(options: BundleOptions) -> int:
    """构建提交包，返回退出码（0 通过 / 1 检查未过 / 3 安全扫描失败）。"""
    root = Path(os.path.realpath(str(options.root)))
    out_root = validate_output_dir(options.out_root, root, allow_outside=options.allow_outside)
    prepare_output_dir(out_root, root=root)
    bundle = out_root / BUNDLE_DIRNAME
    bundle.mkdir(parents=True)
    # 标记写在**被删除的那个输出目录**上（不是 bundle 内部）：
    #   prepare_output_dir 清空的是 out_root，所以身份必须挂在 out_root
    write_marker(out_root, root, out_root, include_official=options.include_official)

    explicit_outside = options.allow_outside and not _is_same_or_under(
        out_root, root / DEFAULT_OUT_SUBDIR
    )
    print(f"项目根：{root}")
    print(f"输出目录：{out_root}{'（--allow-outside 显式许可）' if explicit_outside else ''}")
    print(f"赛事资料包：{'打包（显式允许）' if options.include_official else '不打包（默认保守）'}")
    print(f"回归快照：{'打包（显式允许）' if options.include_regression else '不打包（默认保守：Case 级私有快照）'}")

    checks: list[Check] = [
        Check("输出目录安全校验通过", True,
              f"{out_root}{'（--allow-outside）' if explicit_outside else '（默认 dist 范围）'}"),
    ]

    # 1) 白名单复制
    plan = plan_copy(
        root,
        out_root=out_root,
        include_official=options.include_official,
        include_regression=options.include_regression,
    )
    copied = copy_plan(root, bundle, plan)
    missing = [item for item in REQUIRED_DOCS if not (bundle / item).exists()]
    checks.append(Check("白名单复制 + 必需文档齐全", bool(copied) and not missing,
                        f"{len(copied)} 个文件；拦截 {len(plan.skipped)} 项"
                        + (f"；缺失：{missing}" if missing else "")))

    # 2) 评测类自检
    if options.skip_checks:
        checks.append(Check("评测类自检", True, "已跳过（skip_checks，仅用于路径/白名单/扫描测试）"))
    else:
        checks.extend(_evaluation_checks(root, bundle, skip_tests=options.skip_tests))

    # 3) 包内文件构成
    checks.append(_allowlist_only_check(bundle, copied))
    checks.append(_leftovers_check(bundle))
    checks.append(_official_check(bundle, include_official=options.include_official, root=root))
    checks.append(_regression_check(bundle, include_regression=options.include_regression))

    # 4) 先生成结论页与哈希清单，再对**最终产物**做安全扫描（审计 R4-01）：
    #    旧顺序把清单/说明/SHA256SUMS 写在扫描之后，结论页成了扫描盲区；
    #    失败时还会留下"成功名 zip + 清单写全部通过"的矛盾产物。
    def _scan_detail(scan: ScanReport) -> str:
        return (f"{scan.scanned} 个文件；拒绝名单与敏感串均未命中" if scan.clean
                else f"{scan.scanned} 个文件；命中 {len(scan.problems)} 项")

    _write_submission_note(bundle, include_official=options.include_official,
                           include_regression=options.include_regression)
    _write_checklist(bundle, checks=checks, ok=False, out_root=out_root, plan=plan,
                     copied=copied, include_official=options.include_official,
                     include_regression=options.include_regression, report=ScanReport(0, []))
    _write_sums(bundle)

    report = scan_paths(bundle, include_official=options.include_official,
                        include_regression=options.include_regression)
    checks.append(Check("包内安全扫描通过", report.clean, _scan_detail(report)))
    ok = all(check.passed for check in checks)
    _write_checklist(bundle, checks=checks, ok=ok, out_root=out_root, plan=plan,
                     copied=copied, include_official=options.include_official,
                     include_regression=options.include_regression, report=report)
    _write_sums(bundle)

    # 结论页改写后再复扫一次：确保"清单/说明/哈希"本身也被扫描覆盖。
    report = scan_paths(bundle, include_official=options.include_official,
                        include_regression=options.include_regression)
    for index, check in enumerate(checks):
        if check.name == "包内安全扫描通过":
            checks[index] = Check(check.name, report.clean, _scan_detail(report))
    ok = all(check.passed for check in checks)
    _write_checklist(bundle, checks=checks, ok=ok, out_root=out_root, plan=plan,
                     copied=copied, include_official=options.include_official,
                     include_regression=options.include_regression, report=report)
    _write_sums(bundle)

    # 5) 打 zip：只有全部检查通过才用"可提交"命名；失败一律 .zip.incomplete。
    #    审计 F5-04：先打一次 zip 做回读扫描，再把 ZIP 检查行写进提交清单，
    #    然后重建 zip —— 保证"包内清单"与"展开目录清单"一致，且不遗漏 ZIP 检查。
    stamp = datetime.now().strftime("%Y%m%d")
    zip_name = f"yai-tracelab-submission-{stamp}.zip"

    def _write_zip(target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    archive.write(path, Path(ZIP_PREFIX) / path.relative_to(bundle))

    def _zip_detail(scan: ScanReport) -> str:
        return (f"{scan.scanned} 个条目；拒绝名单与敏感串均未命中" if scan.clean
                else f"{scan.scanned} 个条目；命中 {len(scan.problems)} 项")

    zip_path = out_root / (zip_name if ok else f"{zip_name}.incomplete")
    _write_zip(zip_path)
    zip_report = scan_zip(zip_path, include_official=options.include_official,
                          include_regression=options.include_regression)
    checks.append(Check("ZIP 回读安全扫描通过", zip_report.clean, _zip_detail(zip_report)))
    ok = all(check.passed for check in checks)
    _write_checklist(bundle, checks=checks, ok=ok, out_root=out_root, plan=plan,
                     copied=copied, include_official=options.include_official,
                     include_regression=options.include_regression, report=report)
    _write_sums(bundle)

    # 用最终清单重建 zip，再回读一次；失败则把产物打回 .zip.incomplete 并同步清单。
    final_path = out_root / (zip_name if ok else f"{zip_name}.incomplete")
    if final_path != zip_path:
        try:
            zip_path.unlink()
        except OSError:
            pass
        zip_path = final_path
    _write_zip(zip_path)
    zip_report = scan_zip(zip_path, include_official=options.include_official,
                          include_regression=options.include_regression)
    if not zip_report.clean:
        for index, check in enumerate(checks):
            if check.name == "ZIP 回读安全扫描通过":
                checks[index] = Check(check.name, False, _zip_detail(zip_report))
        ok = False
        target = out_root / f"{zip_name}.incomplete"
        if zip_path != target:
            try:
                os.replace(zip_path, target)
            except OSError:
                pass
            zip_path = target
        _write_checklist(bundle, checks=checks, ok=False, out_root=out_root, plan=plan,
                         copied=copied, include_official=options.include_official,
                         include_regression=options.include_regression, report=report)
        _write_sums(bundle)

    # 6) 无论成功失败都写**真实文件清单**的标记：失败目录必须可被下一次构建安全清理
    #    （审计 R4-01：旧实现在扫描失败时提前 return，marker.files 为空 → 目录永久不可重建）。
    write_marker(out_root, root, out_root, include_official=options.include_official,
                 files=_output_manifest(out_root))

    scan_failed = any(
        not check.passed and check.name.startswith(("包内安全扫描", "ZIP 回读安全扫描"))
        for check in checks
    )
    print(f"提交清单：{'全部通过' if ok else '存在未通过项'}")
    for check in checks:
        print(f"  {'✅' if check.passed else '❌'} {check.name}：{check.detail}")
    if scan_failed:
        print("❌ 安全扫描未通过，未产出可提交 zip：", file=sys.stderr)
        for problem in report.problems + ([] if zip_report.clean else zip_report.problems):
            print(f"  - {problem}", file=sys.stderr)
    print()
    print(f"安全扫描：{report.scanned} 个文件 / {zip_report.scanned} 个 ZIP 条目"
          + ("，全部干净" if report.clean and zip_report.clean else "，存在命中"))
    if ok:
        print(f"包：{zip_path}（{zip_path.stat().st_size / 1024:.0f} KB）")
    else:
        print(f"⚠️ 检查未通过，产物已标记为不可提交：{zip_path}", file=sys.stderr)
    print(f"展开：{bundle}")
    return 0 if ok else (3 if scan_failed else 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建初赛提交包（路径安全 + 白名单打包）")
    parser.add_argument("--out", default=DEFAULT_OUT_SUBDIR,
                        help="输出目录（相对路径按项目根解析），默认 <项目根>/dist")
    parser.add_argument("--allow-outside", action="store_true",
                        help="允许输出到 <项目根>/dist 之外（仍拒绝项目根/祖先/根路径/主目录/符号链接）")
    parser.add_argument("--include-official-data", action="store_true",
                        help="显式允许打包 data/official/ 下明确列出的复现必需文件（默认不打）")
    parser.add_argument("--include-regression", action="store_true",
                        help="显式允许打包 regression/ 下的 Case 级回归快照（默认不打，私有材料）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过测试（仅调试用）")
    args = parser.parse_args(argv)

    options = BundleOptions(
        root=ROOT,
        out_root=Path(args.out),
        allow_outside=args.allow_outside,
        include_official=args.include_official_data,
        include_regression=args.include_regression,
        skip_tests=args.skip_tests,
    )
    try:
        return build_bundle(options)
    except UnsafeOutputError as exc:
        print(f"❌ 输出目录安全检查未通过：{exc}", file=sys.stderr)
        print("   未删除、未写入任何文件。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


