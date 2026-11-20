"""TraceLab 契约层：官方格式 → Canonical Trace IR。

设计原则
--------
1. **不丢原始数据**：每个 IR 对象保留 `raw`，规范化只做索引不删字段。
2. **只读语义**：契约层不修改输入，评测流程永远不会回写原始轨迹。
3. **完整性显式化**：解析过程中发现的问题进入 `IntegrityIssue`，不静默吞掉。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------- 版本常量

IR_VERSION = "tracelab-ir/1"
ADAPTER_OFFICIAL_VERSION = "official-student-1.0"


class Verdict(StrEnum):
    """内部三态判定。

    * PASS：有充分证据证明 Agent 正确处理
    * FAIL：有充分证据证明 Agent 违反业务规则
    * UNKNOWN：证据不足、数据不完整，或**评测器自身失败**

    官方提交 Schema 的 `agent_task_success` 只有布尔值，因此映射关系是
    PASS→true，FAIL/UNKNOWN→false；但 UNKNOWN 在内部与统计里必须单独计数，
    不允许被当作"Agent 做错了"参与版本比较。
    """

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"

    def to_success_flag(self) -> bool:
        """内部三态 → 官方 `agent_task_success` 字段。"""
        return self is Verdict.PASS

STEP_IDS = (
    "RECEIVE_REQUEST", "CREATE_TASK", "RESOLVE_ENTERPRISE", "PARSE_INTENT",
    "COLLECT_INVOICE_DATA", "WAIT_USER_SUPPLEMENT", "VALIDATE_DATA", "CONFIRM_INVOICE",
    "SUBMIT_INVOICE", "WAIT_INVOICE_CALLBACK", "COMPLETE_TASK", "FAIL_TASK",
    "TIMEOUT_TASK", "TRANSFER_TO_HUMAN", "CANCEL_TASK",
)

TERMINAL_STEPS = ("COMPLETE_TASK", "FAIL_TASK", "TIMEOUT_TASK", "TRANSFER_TO_HUMAN", "CANCEL_TASK")


@dataclass(frozen=True)
class IntegrityIssue:
    """数据完整性问题。

    三个维度必须能分开判断，不能合并成一个 complete 布尔值：
    * category="read"     —— 文件层面没读全（坏行、编码、截断）
    * category="schema"   —— 读到了但不符合数据契约（类型/必填/重复）
    * category="semantic" —— 结构合法但业务上可疑（如未配对运行）
    """

    scope: str            # 例如 "run:INV-T01-A"
    code: str             # 稳定错误码，便于测试断言
    detail: str
    severity: str = "warn"       # warn | error
    category: str = "schema"     # read | schema | semantic


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    result: Mapping[str, Any]
    attempt: int | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class Event:
    event_id: str
    seq: int
    step_id: str
    module: str
    action: str
    step_status: str
    result_code: str
    error_code: str | None
    error_message: str | None
    retry_count: int
    observed_input: Mapping[str, Any]
    observed_output: Mapping[str, Any]
    tool_calls: tuple[ToolCall, ...]
    started_at: str
    finished_at: str
    duration_ms: int
    raw: Mapping[str, Any]

    def tool(self, name: str) -> tuple[ToolCall, ...]:
        return tuple(t for t in self.tool_calls if t.name == name)


@dataclass(frozen=True)
class Run:
    run_id: str
    agent_version: str
    task_id: str
    trace_id: str
    events: tuple[Event, ...]
    final_state: str
    snapshot_state: str
    workflow_terminal: bool
    invoice_created: bool
    resource_usage: Mapping[str, Any]
    raw: Mapping[str, Any]

    def event_by_id(self) -> dict[str, Event]:
        return {e.event_id: e for e in self.events}

    def first(self, step_id: str) -> Event | None:
        for e in self.events:
            if e.step_id == step_id:
                return e
        return None

    def last(self, step_id: str) -> Event | None:
        found = None
        for e in self.events:
            if e.step_id == step_id:
                found = e
        return found

    def all(self, step_id: str) -> tuple[Event, ...]:
        return tuple(e for e in self.events if e.step_id == step_id)

    def max_seq(self) -> int:
        return max((e.seq for e in self.events), default=0)


@dataclass(frozen=True)
class Case:
    case_id: str
    scenario: str
    user_request: str
    context: Mapping[str, Any]
    reference_result: Mapping[str, Any]
    runs: tuple[Run, ...]
    dataset_version: str
    data_origin: str
    raw: Mapping[str, Any]

    def run(self, run_id: str) -> Run | None:
        for r in self.runs:
            if r.run_id == run_id:
                return r
        return None

    def baseline(self) -> Run:
        """基线版：官方约定 runs 顺序为 [基线, 候选]，此处按位置不按版本名。"""
        return self.runs[0]

    def candidate(self) -> Run:
        return self.runs[1] if len(self.runs) > 1 else self.runs[0]

    @property
    def is_paired(self) -> bool:
        return len(self.runs) == 2


@dataclass(frozen=True)
class EvalProfile:
    """评测变体（供消融实验使用）。

    `rule_ids=None` 表示启用全部规则；`terminal_only=True` 表示完全跳过规则，
    只用"终态是否等于题内可接受终态"判断，代表不做业务语义分析时的上界。
    """

    name: str
    description: str
    rule_ids: frozenset[str] | None = None
    terminal_only: bool = False


@dataclass
class Dataset:
    cases: list[Case]
    issues: list[IntegrityIssue] = field(default_factory=list)
    source: str = ""
    sha256: str = ""
    #: 显式完整性计数：total/parsed/skipped/truncated。truncated 恒为 0——
    #: 读取层不设任何上限，无法证明"没漏"就不允许报告"评测完成"。
    stats: dict[str, int] = field(default_factory=dict)
    #: 实际使用的适配器（由 adapter 在 load 时写入；manifest 从这里取，不再硬编码）
    adapter_name: str = ""
    adapter_version: str = ""

    def __iter__(self) -> Iterator[Case]:
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)

    @property
    def errors(self) -> list[IntegrityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    # ---- 三项完整性标志：必须分开报告，不能合并成一个 complete ----
    @property
    def read_complete(self) -> bool:
        """A. 文件是否被完整读取（无坏行/编码问题/截断）。

        `stats` 是读取层唯一的自证凭据：**没有计数就没有证据**。旧实现把
        `stats` 默认成 `{}`，于是"缺少任何计数"被静默当成"读全了"（真空成立）。
        现在关键计数（总行数/解析数/跳过数/截断数）只要缺一项就**不得**宣称读全。
        """
        required = ("lines_total", "cases_parsed", "skipped", "truncated")
        if any(key not in self.stats for key in required):
            return False
        if self.stats["truncated"] != 0 or self.stats["skipped"] != 0:
            return False
        return not any(i.category == "read" and i.severity == "error" for i in self.issues)

    @property
    def schema_valid(self) -> bool:
        """B. 输入是否符合数据契约（类型/必填/重复等）。"""
        return not any(i.category == "schema" and i.severity == "error" for i in self.issues)





