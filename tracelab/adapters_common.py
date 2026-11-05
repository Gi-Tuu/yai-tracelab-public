"""适配层公共工具：Trace IR 基础类型、JSONL 读取与字段校验（审计「大型模块拆分」）。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .contracts import Case, Dataset, IntegrityIssue

STEP_ALIASES: dict[str, str] = {
    "RECEIVE": "RECEIVE_REQUEST",
    "RECEIVE_REQUEST": "RECEIVE_REQUEST",
    "CREATE_TASK": "CREATE_TASK",
    "RESOLVE": "RESOLVE_ENTERPRISE",
    "PARSE": "PARSE_INTENT",
    "PARSE_INTENT": "PARSE_INTENT",
    "INTENT": "PARSE_INTENT",
    "COLLECT": "COLLECT_INVOICE_DATA",
    "COLLECT_DATA": "COLLECT_INVOICE_DATA",
    "COLLECT_FIELDS": "COLLECT_INVOICE_DATA",
    "ASK_USER": "WAIT_USER_SUPPLEMENT",
    "WAIT_SUPPLEMENT": "WAIT_USER_SUPPLEMENT",
    "VALIDATE": "VALIDATE_DATA",
    "CHECK": "VALIDATE_DATA",
    "CONFIRM": "CONFIRM_INVOICE",
    "CONFIRM_INVOICE": "CONFIRM_INVOICE",
    "SUBMIT": "SUBMIT_INVOICE",
    "SUBMIT_INVOICE": "SUBMIT_INVOICE",
    "PREVIEW": "CONFIRM_INVOICE",
    "CALLBACK": "WAIT_INVOICE_CALLBACK",
    "WAIT_CALLBACK": "WAIT_INVOICE_CALLBACK",
    "COMPLETE": "COMPLETE_TASK",
    "DONE": "COMPLETE_TASK",
    "FAIL": "FAIL_TASK",
    "TIMEOUT": "TIMEOUT_TASK",
    "TRANSFER": "TRANSFER_TO_HUMAN",
    "ESCALATE": "TRANSFER_TO_HUMAN",
    "CANCEL": "CANCEL_TASK",
}

_BUSINESS_STEP_HINTS = (
    "PARSE", "INTENT", "COLLECT", "VALIDATE", "CONFIRM", "SUBMIT", "PREVIEW",
    "CANCEL", "COMPLETE", "FAIL", "TIMEOUT", "TRANSFER", "RECEIVE", "CREATE",
    "RESOLVE", "CALLBACK", "INVOICE", "WAIT",
)

def _looks_like_business_step(step_id: str) -> bool:
    upper = step_id.upper()
    return any(hint in upper for hint in _BUSINESS_STEP_HINTS)

REQUIRED_EVENT_FIELDS = (
    "event_id", "seq", "step_id", "module", "action", "step_status",
    "result_code", "error_code", "error_message", "retry_count",
    "observed_input", "observed_output", "tool_calls",
)

class Adapter(Protocol):
    """适配器契约：实现 load() 即可把任意轨迹格式接入评测流水线。"""

    name: str
    version: str

    def load(self, path: Path) -> Dataset: ...

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _decode_lines(path: Path, issues: list[IntegrityIssue]) -> list[tuple[int, str | None]]:
    """按字节读文件、逐行解码（C-25）。

    * 文件先整体按 bytes 读入，再**逐行** `decode("utf-8")`：
      坏字节只影响这一行，记 `BAD_ENCODING`（read 级 error）后跳过，
      不会像旧实现那样让 `read_text()` 直接抛 UnicodeDecodeError 打穿整批；
    * 返回 `(物理行号, 文本)` 列表；`None` 表示"这一行存在但解不出来"，
      仍计入 `lines_total`，所以 `skipped`/`PARTIAL_PARSE` 能如实反映"少读了一行"。
    * 行号一律是**文件物理行号**：空行/注释行被跳过时不会让后续行的编号前移
      （旧实现用过滤后下标，坏 JSON 的定位会指向错误行）。
    """
    data = path.read_bytes()
    entries: list[tuple[int, str | None]] = []
    for lineno, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            issues.append(IntegrityIssue(
                f"line:{lineno}", "BAD_ENCODING",
                f"第 {lineno} 行不是合法 UTF-8：{exc}", "error", "read",
            ))
            entries.append((lineno, None))
            continue
        if lineno == 1:
            text = text.lstrip("\ufeff")  # 容忍首行 BOM，不让它污染 case_id
        if not text.strip():  # 例如"只有 BOM"的首行：等价于空行
            continue
        entries.append((lineno, text))
    return entries

def _finalize_dataset(
    path: Path,
    cases: list[Case],
    issues: list[IntegrityIssue],
    *,
    lines_total: int,
    adapter_name: str,
    adapter_version: str,
) -> Dataset:
    """官方/异构适配器**共用**的收尾逻辑（C-23）。

    收尾必须回答同一组问题：行数是否等于解析数、有没有重复主键、是否空文件、
    是否混了多个数据版本。以前只有 official 做了这些检查，generic 缺
    EMPTY_DATASET / DUPLICATE_CASE_ID / DUPLICATE_RUN_ID，换个格式就漏检。
    """
    parsed = len(cases)
    skipped = lines_total - parsed
    stats = {
        "lines_total": lines_total,
        "cases_parsed": parsed,
        "skipped": skipped,
        # 读取层不做任何上限截断：truncated 必须为 0，否则整体标记为部分评测。
        "truncated": 0,
        "runs_total": sum(len(c.runs) for c in cases),
        "events_total": sum(len(r.events) for c in cases for r in c.runs),
        # 被丢弃的非 object tool_calls 元素：计数保留下来，便于审计"丢了多少"
        "bad_tool_calls": sum(1 for i in issues if i.code == "BAD_TOOL_CALL_TYPE"),
    }

    if parsed != lines_total:
        issues.append(IntegrityIssue(
            "dataset", "CASE_COUNT_MISMATCH",
            f"文件 {lines_total} 行但仅解析出 {parsed} 个 Case", "error", "read",
        ))
    if skipped:
        issues.append(IntegrityIssue(
            "dataset", "PARTIAL_PARSE",
            f"{lines_total} 行中仅解析 {parsed} 个 Case，{skipped} 行被跳过", "error", "read",
        ))
    if not lines_total:
        issues.append(IntegrityIssue("dataset", "EMPTY_DATASET", "文件没有任何 JSON 行", "error", "read"))

    for case_id, count in sorted(Counter(c.case_id for c in cases).items()):
        if count > 1:
            issues.append(IntegrityIssue(
                f"case:{case_id}", "DUPLICATE_CASE_ID", f"case_id 重复 {count} 次", "error", "schema",
            ))
    for case in cases:
        run_ids = [r.run_id for r in case.runs]
        if len(set(run_ids)) != len(run_ids):
            issues.append(IntegrityIssue(
                f"case:{case.case_id}", "DUPLICATE_RUN_ID", "run_id 重复", "error", "schema",
            ))

    for key in ("dataset_version", "data_origin"):
        values = {c.raw.get(key) for c in cases}
        if len(values) > 1:
            issues.append(IntegrityIssue(
                "dataset", "MIXED_DATASET", f"{key} 不一致：{sorted(map(str, values))}", "warn",
            ))

    return Dataset(
        cases=cases, issues=issues, source=str(path), sha256=sha256_file(path),
        stats=stats, adapter_name=adapter_name, adapter_version=adapter_version,
    )

def _load_jsonl(
    path: Path,
    parse_record: Callable[[dict[str, Any], int, list[IntegrityIssue]], Case | None],
    *,
    adapter_name: str,
    adapter_version: str,
) -> Dataset:
    """JSONL 公共读取循环：逐行解码 → `json.loads` → 交给适配器解析。

    单行坏数据（坏字节、坏 JSON、递归爆栈、非 object）一律只丢该行，
    **不允许**中止整批（C-25）。
    """
    issues: list[IntegrityIssue] = []
    entries = _decode_lines(path, issues)
    cases: list[Case] = []

    for lineno, line in entries:
        if line is None:  # 坏字节：BAD_ENCODING 已记录
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError) as exc:
            # JSONDecodeError 是 ValueError 子类；超深嵌套/异常输入可能抛 RecursionError
            issues.append(IntegrityIssue(f"line:{lineno}", "BAD_JSON", str(exc), "error", "read"))
            continue
        if not isinstance(obj, dict):
            issues.append(IntegrityIssue(
                f"line:{lineno}", "NOT_OBJECT",
                f"记录必须是 JSON 对象，实际 {type(obj).__name__}", "error", "schema",
            ))
            continue
        case = parse_record(obj, lineno, issues)
        if case is not None:
            cases.append(case)

    return _finalize_dataset(
        path, cases, issues,
        lines_total=len(entries), adapter_name=adapter_name, adapter_version=adapter_version,
    )

def _as_mapping(
    value: Any, scope: str, code: str, issues: list[IntegrityIssue], *, category: str = "schema"
) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    issues.append(IntegrityIssue(
        scope, code, f"期望 object，实际 {type(value).__name__}", "error", category
    ))
    return {}

def _safe_int(
    value: Any,
    default: int,
    *,
    field: str,
    scope: str,
    issues: list[IntegrityIssue],
    missing: bool = False,
    minimum: int | None = None,
) -> int:
    """把字段安全转成 int：**只接受真正的 int**。

    `bool` 是 `int` 的子类，`True` 会被悄悄当成 1；数字字符串、浮点数、null
    同样不符合数据字典里的 "integer"，一律记 schema 错误并落默认值，
    绝不抛异常崩掉整批。`missing=True` 表示字段本身缺失（已由缺失字段检查
    记录过），此时只落默认值，避免重复报错。
    `minimum` 用于有序字段（seq/retry_count/duration_ms）拒绝负值——
    负 seq 会让该事件在"最早错误"排序里抢到首位，属于静默篡改事实。
    """
    if isinstance(value, bool):
        issues.append(IntegrityIssue(
            scope, "BAD_FIELD_TYPE", f"{field} 期望 integer，实际 bool", "error", "schema"
        ))
        return default
    if isinstance(value, int):
        if minimum is not None and value < minimum:
            issues.append(IntegrityIssue(
                scope, "BAD_FIELD_VALUE",
                f"{field} 必须 >= {minimum}，实际 {value}", "error", "schema",
            ))
            return default
        return value
    if missing:
        return default
    issues.append(IntegrityIssue(
        scope, "BAD_FIELD_TYPE", f"{field} 期望 integer，实际 {type(value).__name__}", "error", "schema"
    ))
    return default

def _optional_str(
    raw: Mapping[str, Any], field: str, *, scope: str, issues: list[IntegrityIssue]
) -> str | None:
    """可空字符串字段：`null` 与字符串都合法，其它类型记错并落 None。

    旧实现直接把原值塞进 `Event.error_code`，于是 `500` 这样的数字一路漏到规则层，
    触发 `AttributeError` 并被隔离成"评测器内部异常"，掩盖真实的数据问题。
    """
    value = raw.get(field)
    if value is None or isinstance(value, str):
        return value
    issues.append(IntegrityIssue(
        scope, "BAD_FIELD_TYPE", f"{field} 期望 string 或 null，实际 {type(value).__name__}",
        "error", "schema",
    ))
    return None

def _required_str(raw: Mapping[str, Any], field: str, *, scope: str, issues: list[IntegrityIssue]) -> str:
    """必需字符串字段：缺失或类型不符都记 schema 错误，返回安全默认空串。

    不用 `str(...)` 强转，避免 `str({"a": 1})` 这种"看似有值"的假数据。
    """
    if field not in raw:
        issues.append(IntegrityIssue(scope, "MISSING_FIELD", f"缺少必需字段 {field}", "error", "schema"))
        return ""
    value = raw[field]
    if isinstance(value, str):
        return value
    issues.append(IntegrityIssue(
        scope, "BAD_FIELD_TYPE", f"{field} 期望 string，实际 {type(value).__name__}", "error", "schema"
    ))
    return ""

def _required_bool(raw: Mapping[str, Any], field: str, *, scope: str, issues: list[IntegrityIssue]) -> bool:
    """业务布尔字段：**只接受真正的 bool**。

    `bool("false")` 与 `bool("0")` 都会得到 True，这正是"非法输入被伪装成正常
    业务状态"的根因；因此字符串/数字/null 一律记 schema 错误并落安全默认值 False。
    """
    if field not in raw:
        issues.append(IntegrityIssue(scope, "MISSING_FIELD", f"缺少必需字段 {field}", "error", "schema"))
        return False
    value = raw[field]
    if isinstance(value, bool):
        return value
    issues.append(IntegrityIssue(
        scope, "BAD_FIELD_TYPE", f"{field} 期望 boolean，实际 {type(value).__name__}", "error", "schema"
    ))
    return False

def _required_object(
    raw: Mapping[str, Any], field: str, *, scope: str, code: str, issues: list[IntegrityIssue]
) -> dict[str, Any]:
    """object 字段：字段存在但不是 object（含 null）时记 schema 错误；缺失时落空 dict。"""
    if field not in raw:
        return {}
    return _as_mapping(raw[field], scope, code, issues)

