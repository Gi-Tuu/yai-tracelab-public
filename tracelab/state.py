"""业务状态重建：把事件序列还原成"Agent 当时认为的业务事实"。

这一层是 TraceLab 与通用 Trace 工具的分界线：通用工具看到的是事件流，
我们看到的是购方、商品、金额口径、确认门禁、提交参数、回调结果这些**业务事实**，
以及它们在事件之间的流动。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .contracts import STEP_IDS, Case, Event, Run

TWO_PLACES = Decimal("0.01")


# ----------------------------------------------------------------- 工具函数

#: 金额解析状态。**不允许**把无法解析的值统一折叠成 None 后继续比较，
#: 否则 "abc" 与 "xyz" 会被当成"两个缺失值相等"。
MONEY_OK = "ok"
MONEY_MISSING = "missing"
MONEY_INVALID = "invalid"
MONEY_UNSUPPORTED = "unsupported"

#: 允许的最大小数位数（超过视为非法金额，而不是静默四舍五入）
MAX_MONEY_SCALE = 6


#: 允许的最大金额数量级（绝对值）。超过它视为非法金额，而不是让下游 quantize 崩。
MAX_MONEY_ABS = Decimal("1e15")


def _effective_exponent(value: Decimal) -> int | None:
    """去掉**数学等价尾零**之后的有效小数位指数（无法判定时返回 None）。

    `"1000.0000000"` 与 `"1000"` 是同一个金额，不能因为写了 7 个尾零就判非法；
    而 `"1.2345678"` 没有尾零可去，最小单位就是 1e-7，仍然应判非法。
    这里只做指数平移，不改写 `value` 本身（保留原始精度供审计）。

    零是特例：`Decimal("0.00000000")` 的尾数只有一个 0，逐位裁剪后指数仍是 -8，
    会被误判非法；因此零直接返回 0（审计 F-24）。
    """
    if value.is_zero():
        return 0
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    if exponent >= -MAX_MONEY_SCALE:
        return exponent
    trimmed = 0
    for digit in reversed(value.as_tuple().digits):
        if digit != 0:
            break
        trimmed += 1
    if trimmed == 0:
        return exponent
    return exponent + trimmed


@dataclass(frozen=True)
class MoneyParse:
    """金额解析结果：把"能不能判断"和"值是多少"分开表达。"""

    status: str
    value: Decimal | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == MONEY_OK

    def __bool__(self) -> bool:  # 便于 `if parse:` 使用
        return self.ok


def parse_money(value: Any) -> MoneyParse:
    """把任意输入解析成金额，并明确区分 缺失 / 非法 / 类型不支持。"""
    if value is None:
        return MoneyParse(MONEY_MISSING, None, "值为空")
    if isinstance(value, bool):
        # bool 是 int 的子类，但业务上绝不是金额
        return MoneyParse(MONEY_UNSUPPORTED, None, "布尔值不能作为金额")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return MoneyParse(MONEY_INVALID, None, "NaN/Inf 不是合法金额")
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return MoneyParse(MONEY_INVALID, None, "空字符串不是金额")
        try:
            candidate = Decimal(text)
        except (InvalidOperation, ValueError):
            return MoneyParse(MONEY_INVALID, None, f"无法解析为数字：{value!r}")
    else:
        return MoneyParse(MONEY_UNSUPPORTED, None, f"不支持的类型：{type(value).__name__}")

    if not candidate.is_finite():
        return MoneyParse(MONEY_INVALID, None, "NaN/Inf 不是合法金额")
    if abs(candidate) > MAX_MONEY_ABS:
        # 天文数字不是业务金额：放行会让下游 quantize 抛 InvalidOperation，
        # 最终被隔离成"评测器内部异常"，把数据缺陷伪装成程序 bug（审计 F-07）。
        return MoneyParse(MONEY_INVALID, None, "金额数量级超出允许范围（>1e15）")
    effective = _effective_exponent(candidate)
    if isinstance(effective, int) and effective < -MAX_MONEY_SCALE:
        return MoneyParse(MONEY_INVALID, None, f"小数位超过 {MAX_MONEY_SCALE} 位")
    return MoneyParse(MONEY_OK, candidate)


def as_decimal(value: Any) -> Decimal | None:
    """兼容旧接口：解析成功返回 Decimal，否则 None。判断逻辑请用 `parse_money`。"""
    parsed = parse_money(value)
    return parsed.value if parsed.ok else None


#: 金额比较结果：只有 EQUAL 才算相等，UNKNOWN 不允许被当作通过
MONEY_EQUAL = "equal"
MONEY_UNEQUAL = "unequal"
MONEY_UNKNOWN = "unknown"


def money_compare(left: Any, right: Any, tolerance: str = "0.005") -> str:
    """三态金额比较。任一侧缺失/非法 → UNKNOWN（不是相等，也不是不等）。"""
    a, b = parse_money(left), parse_money(right)
    if not (a.ok and b.ok):
        return MONEY_UNKNOWN
    assert a.value is not None and b.value is not None
    return MONEY_EQUAL if abs(a.value - b.value) <= Decimal(tolerance) else MONEY_UNEQUAL


def money_eq(left: Any, right: Any, tolerance: str = "0.005") -> bool:
    """金额相等判定：**只有两侧都是合法金额且差值在容差内才为 True**。

    **只可用于肯定判断**：True 一定意味着"确定相等"；
    False 既可能是"确定不等"，也可能是"无法判断"（缺失/非法/类型不支持）。
    任何要判"不等"的地方必须写 `money_compare(...) == MONEY_UNEQUAL`，
    否则会把"无法判断"当成"确定不等"，凭空造出违规。
    """
    return money_compare(left, right, tolerance) == MONEY_EQUAL


def money_signature(value: Any) -> tuple[str, str]:
    """明细金额的规范化签名（唯一实现）：合法金额 → 分位量化后的字符串。

    审计 R3-A-06：规则层原先内联了一份 `quantize(ROUND_HALF_UP)`，与 `money_identical`
    各写一遍、改一处不同步。现在 R006 的签名也走这里，两处语义必然一致。
    """
    from decimal import ROUND_HALF_UP, InvalidOperation

    parsed = parse_money(value)
    if parsed.ok and parsed.value is not None:
        try:
            normalized = parsed.value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return ("unparsed", "overflow")
        # 审计 R4-04：`-0.0` 与 `0.0` 数值相同，签名必须归一（否则 R006 会把同一金额判成违规）。
        if normalized == 0:
            normalized = normalized.copy_abs()
        return ("amount", str(normalized))
    return ("unparsed", parsed.status)


def money_identical(left: Any, right: Any) -> bool | None:
    """分位精确相等（用于**逐条明细**比较；与 `money_compare` 的 ±0.005 容差不同）。

    审计 P4 / F-23 / R4-04：明细语义现在**只由 `money_signature` 决定**——
    两处不再各写一份 quantize，符号零（`-0.0` vs `0.0`）也由签名统一归一。
    返回 `None` 表示至少一侧不是合法金额（不可判定），调用方不得当成"不相等"。
    """
    left_sig = money_signature(left)
    right_sig = money_signature(right)
    if left_sig[0] != "amount" or right_sig[0] != "amount":
        return None
    return left_sig == right_sig


def round_money(value: Any) -> Decimal | None:
    parsed = parse_money(value)
    if not parsed.ok or parsed.value is None:
        return None
    try:
        return parsed.value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        # 即便 parse_money 未来放宽了数量级，quantize 也不能把整条运行打崩
        return None


def dig(obj: Any, *path: str) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, Mapping) or key not in cur:
            return None
        cur = cur[key]
    return cur


def as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# --------------------------------------------------------------- 状态对象

@dataclass(frozen=True)
class AmountPropagation:
    """金额传播探测结果：传播边 + 未建立传播边的目标字段与原因。

    `rejected` 里每个字段保留完整的 `MoneyParse`：
    * 用 `.status`（MONEY_MISSING / MONEY_INVALID / MONEY_UNSUPPORTED）
      区分"缺失 / 非法 / 类型不支持"三种情况；
    * 用 `.reason` 说明具体原因（例如无法解析的原始值）。

    只要 `rejected` 非空，就说明目标金额组不完整，`edges` 必然为空——
    绝不允许退化成"只比较部分合法字段"后仍建立传播链。
    """

    edges: tuple[tuple[Event, dict[str, str]], ...] = ()
    rejected: dict[str, MoneyParse] = field(default_factory=dict)

@dataclass(frozen=True)
class BusinessState:
    """某次运行的可观测业务状态快照（全部来自 observed_input/output 与 tool_calls）。"""

    run: Run
    case: Case

    # 各阶段事件
    analyzes: tuple[Event, ...] = ()
    collects: tuple[Event, ...] = ()
    validates: tuple[Event, ...] = ()
    confirms: tuple[Event, ...] = ()
    submits: tuple[Event, ...] = ()
    callbacks: tuple[Event, ...] = ()
    terminals: tuple[Event, ...] = ()

    # ---- 阶段选择器 ----
    @property
    def ordered_events(self) -> tuple[Event, ...]:
        """按 **(seq, 原始顺序)** 稳定排序后的事件序列。

        `sorted` 是稳定排序：seq 相同时保持 `run.events` 里的原始先后，
        因此乱序 trace 里"同一 seq 的两条事件"仍然可复现，不会被随机打乱。
        """
        return tuple(sorted(self.run.events, key=lambda e: e.seq))

    @property
    def has_recognizable_evidence(self) -> bool:
        """运行里是否存在**任何可识别阶段**（step_id 属于 STEP_IDS）。

        事件为空、或全部 step_id 都不在 STEP_IDS 内时返回 False：
        这种运行所有业务规则都会"无事可判"，必须显式拒判，不能默认 PASS。
        """
        return any(e.step_id in STEP_IDS for e in self.run.events)

    def first_of(self, step_id: str) -> Event | None:
        for e in self.ordered_events:
            if e.step_id == step_id:
                return e
        return None

    def last_of(self, step_id: str) -> Event | None:
        found = None
        for e in self.ordered_events:
            if e.step_id == step_id:
                found = e
        return found

    # ---- 派生业务事实 ----
    @property
    def parse_event(self) -> Event | None:
        """取**seq 最小**的意图解析事件（稳定排序后的首条）。

        这是有意为之的契约（测试 C-27 锁定）：任务类型代表"用户最初提出的意图"，
        用首条解析冻结它；多轮修正属于上下文问题，由 R015 单独处理。当存在多条
        PARSE_INTENT 且结论不一致时，归因层会显式给出不确定说明，而不是静默选一条。
        """
        return self.first_of("PARSE_INTENT")

    @property
    def task_type(self) -> str | None:
        e = self.parse_event
        return dig(e.observed_output, "invoice_task_type") if e else None

    @property
    def intent(self) -> str | None:
        e = self.parse_event
        return dig(e.observed_output, "intent") if e else None

    @property
    def collect_event(self) -> Event | None:
        return self.collects[-1] if self.collects else None

    @property
    def buyer_id(self) -> str | None:
        """购方：优先采集回显；采集没回显时退回 context 里**已解析的标量**。

        审计 R2-05：购方可以由客户档案/上下文直接给出，不要求每条轨迹都在
        COLLECT 里回显；但 `buyer_candidates`（多候选）不算"已解析"，仍需消歧。
        """
        e = self.collect_event
        value = dig(e.observed_output, "buyer_id") if e else None
        if value is not None:
            return value
        context_value = (self.case.context or {}).get("buyer_id")
        return context_value if isinstance(context_value, str) else None

    @property
    def item_id(self) -> str | None:
        """商品：优先采集回显；采集没回显时退回 context 里**已解析的标量**（R2-05）。"""
        e = self.collect_event
        value = dig(e.observed_output, "item_id") if e else None
        if value is not None:
            return value
        context_value = (self.case.context or {}).get("item_id")
        return context_value if isinstance(context_value, str) else None

    @property
    def amount_basis(self) -> str | None:
        e = self.collect_event
        return dig(e.observed_output, "amount_basis") if e else None

    @property
    def amount_value(self) -> Any:
        """金额：优先采集回显；采集没回显时退回 context 里给出的金额（R2-05）。"""
        e = self.collect_event
        value = dig(e.observed_output, "amount_value") if e else None
        if value is not None:
            return value
        return (self.case.context or {}).get("amount_value")

    @property
    def computed_amounts(self) -> dict[str, Any]:
        e = self.collect_event
        return as_dict(dig(e.observed_output, "computed_amounts")) if e else {}

    @property
    def line_items(self) -> tuple[dict[str, Any], ...]:
        e = self.collect_event
        items = dig(e.observed_output, "line_items") if e else None
        if not isinstance(items, list):
            return ()
        return tuple(dict(x) for x in items if isinstance(x, Mapping))

    @property
    def original_invoice_id(self) -> str | None:
        e = self.collect_event
        value = dig(e.observed_output, "original_invoice_id") if e else None
        return value

    @property
    def missing_fields(self) -> tuple[str, ...]:
        e = self.collect_event
        value = dig(e.observed_output, "missing_fields") if e else None
        return tuple(value) if isinstance(value, list) else ()

    @property
    def validate_event(self) -> Event | None:
        return self.validates[-1] if self.validates else None

    @property
    def validation_passed(self) -> bool | None:
        e = self.validate_event
        v = dig(e.observed_output, "validation_passed") if e else None
        return v if isinstance(v, bool) else None

    @property
    def create_allowed(self) -> bool | None:
        e = self.validate_event
        v = dig(e.observed_output, "create_allowed") if e else None
        return v if isinstance(v, bool) else None

    @property
    def confirmation_required(self) -> bool | None:
        e = self.validate_event
        v = dig(e.observed_output, "confirmation_required") if e else None
        return v if isinstance(v, bool) else None

    @property
    def risk_flags(self) -> tuple[str, ...]:
        """上下文风险与规则工具返回的风险合并。"""
        flags: list[str] = []
        ctx = self.case.context.get("risk_flags")
        if isinstance(ctx, list):
            flags += [str(x) for x in ctx]
        e = self.validate_event
        tool_flags = dig(e.observed_output, "risk_flags") if e else None
        if isinstance(tool_flags, list):
            flags += [str(x) for x in tool_flags]
        return tuple(dict.fromkeys(flags))

    @property
    def confirm_event(self) -> Event | None:
        return self.confirms[-1] if self.confirms else None

    @property
    def invoice_confirmed(self) -> bool | None:
        e = self.confirm_event
        v = dig(e.observed_output, "invoice_confirmed") if e else None
        return v if isinstance(v, bool) else None

    @property
    def preview(self) -> dict[str, Any]:
        e = self.confirm_event
        return as_dict(dig(e.observed_output, "preview")) if e else {}

    @property
    def preview_amounts(self) -> dict[str, Any]:
        return as_dict(self.preview.get("amounts"))

    @property
    def submit_event(self) -> Event | None:
        return self.submits[-1] if self.submits else None

    @property
    def submit_attempts(self) -> int:
        """正式提交**调用次数**。

        只数 `invoice.submit*` 工具调用：提交步骤里夹带的其它工具（查询/校验）
        不是一次提交尝试；一次调用都没有但提交步骤存在时，按 1 次尝试计。
        没有提交步骤时为 0（不能凭空造出"发起过提交"的事实）。
        """
        if not self.submits:
            return 0
        return len(self.submit_tool_calls()) or 1

    def submit_tool_calls(self) -> tuple[Any, ...]:
        calls = []
        for e in self.submits:
            calls += [t for t in e.tool_calls if t.name.startswith("invoice.submit")]
        return tuple(calls)

    @property
    def submit_amounts(self) -> dict[str, Any]:
        """提交参数里的金额：优先 tool_calls.arguments，其次 observed_input。"""
        calls = self.submit_tool_calls()
        if calls:
            amounts = dig(calls[-1].arguments, "amounts")
            if isinstance(amounts, Mapping):
                return dict(amounts)
        e = self.submit_event
        amounts = dig(e.observed_input, "amounts") if e else None
        return as_dict(amounts)

    @property
    def submit_args(self) -> dict[str, Any]:
        calls = self.submit_tool_calls()
        if calls:
            return as_dict(calls[-1].arguments)
        e = self.submit_event
        return as_dict(e.observed_input) if e else {}

    @property
    def submit_status(self) -> str | None:
        e = self.submit_event
        status = dig(e.observed_output, "submit_status") if e else None
        return status if isinstance(status, str) else None

    @property
    def submit_error_code(self) -> str | None:
        e = self.submit_event
        return e.error_code if e else None

    @property
    def callback_event(self) -> Event | None:
        return self.callbacks[-1] if self.callbacks else None

    @property
    def callback_status(self) -> str | None:
        e = self.callback_event
        if not e:
            return None
        status = dig(e.observed_output, "callback_status")
        if isinstance(status, str):
            return status
        call = e.tool("invoice.callback")
        if call:
            status = dig(call[0].result, "status")
            if isinstance(status, str):
                return status
        return None

    @property
    def callback_error(self) -> str | None:
        e = self.callback_event
        return e.error_code if e else None

    @property
    def canceled(self) -> bool:
        return any(e.step_id == "CANCEL_TASK" for e in self.run.events)

    @property
    def transferred(self) -> bool:
        return any(e.step_id == "TRANSFER_TO_HUMAN" for e in self.run.events)

    @property
    def completed(self) -> bool:
        return any(e.step_id == "COMPLETE_TASK" for e in self.run.events)

    @property
    def final_state(self) -> str:
        return self.run.final_state

    @property
    def invoice_created(self) -> bool:
        return self.run.invoice_created

    # ---- 因果可达性辅助 ----
    def events_after(self, event: Event) -> tuple[Event, ...]:
        return tuple(e for e in self.ordered_events if e.seq > event.seq)

    def carries_amounts_probe(self, amounts: Mapping[str, Any]) -> AmountPropagation:
        """探测哪些事件携带了**同一组**金额，并给出未建立传播边的原因。

        规则（对应 P1-5.2）：
        1. 目标金额里只要**有一个字段不是合法金额**（缺失 / 非法 / 类型不支持），
           就**不建立任何传播边**——绝不能退化成"只比较剩下的合法字段"，
           否则非法金额会借其它字段的匹配伪造出虚假传播链；
        2. 目标金额里必须至少有**一个合法字段**，否则空集合不能算传播证据；
        3. 必须比较**全部**目标字段，候选缺字段即不匹配；
        4. 候选里的字段必须也能解析成合法金额；
        5. 返回匹配明细（字段名 + 规范化后的值）与 `rejected`
           （字段名 + 解析原因），让"为什么没建立边"可查询、可记录。
        """
        if not amounts:
            return AmountPropagation()

        targets: dict[str, Decimal] = {}
        rejected: dict[str, MoneyParse] = {}
        for key, value in amounts.items():
            parsed = parse_money(value)
            if parsed.ok and parsed.value is not None:
                targets[key] = parsed.value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            else:
                # 显式记录拒绝原因，而不是静默丢弃这个字段
                rejected[key] = parsed
        if rejected or not targets:
            return AmountPropagation(rejected=rejected)

        hits: list[tuple[Event, dict[str, str]]] = []
        for e in self.ordered_events:
            candidates: list[Any] = [e.observed_input, e.observed_output]
            candidates += [t.arguments for t in e.tool_calls]
            # 预览金额嵌在 observed_output.preview.amounts 下，单独展开
            preview = dig(e.observed_output, "preview", "amounts")
            if isinstance(preview, Mapping):
                candidates.append({"amounts": dict(preview)})
            for cand in candidates:
                got = dig(cand, "amounts") if isinstance(cand, Mapping) else None
                if not isinstance(got, Mapping):
                    continue
                matched, ok = {}, True
                for key, expected in targets.items():
                    if key not in got:          # 缺字段 → 不算匹配（不自动忽略）
                        ok = False
                        break
                    parsed = parse_money(got.get(key))
                    if not parsed.ok or parsed.value is None:   # 非法/缺失 → 不算匹配
                        ok = False
                        break
                    actual = parsed.value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                    if actual != expected:
                        ok = False
                        break
                    matched[key] = str(actual)
                if ok and matched and not any(hit_event is e for hit_event, _ in hits):
                    hits.append((e, matched))
        hits.sort(key=lambda item: item[0].seq)
        return AmountPropagation(edges=tuple(hits))

    def carries_amounts_detailed(self, amounts: Mapping[str, Any]) -> tuple[tuple[Event, dict[str, str]], ...]:
        """兼容接口：只返回传播边；拒绝原因请用 `carries_amounts_probe`。

        任一目标字段不是合法金额时返回空 tuple（明细见 probe 的 `rejected`）。
        """
        return self.carries_amounts_probe(amounts).edges

    def carries_amounts(self, amounts: Mapping[str, Any]) -> tuple[Event, ...]:
        """兼容接口：只返回携带同一组金额的事件。"""
        return tuple(e for e, _ in self.carries_amounts_detailed(amounts))


def reconstruct(case: Case, run: Run) -> BusinessState:
    """从一条运行重建业务状态。

    事件按 **(seq, 原始顺序)** 稳定排序后再分阶段：乱序 trace（seq 与写入顺序不一致）
    不能让"第一个/最后一个事件"随文件顺序漂移，同 seq 的事件则保留原始先后。
    原始 `run` 不被改写，只读。
    """
    ordered = tuple(sorted(run.events, key=lambda e: e.seq))

    def take(*step_ids: str) -> tuple[Event, ...]:
        wanted = set(step_ids)
        return tuple(e for e in ordered if e.step_id in wanted)

    return BusinessState(
        run=run,
        case=case,
        analyzes=take("PARSE_INTENT"),
        collects=take("COLLECT_INVOICE_DATA"),
        validates=take("VALIDATE_DATA"),
        confirms=take("CONFIRM_INVOICE"),
        submits=take("SUBMIT_INVOICE"),
        callbacks=take("WAIT_INVOICE_CALLBACK"),
        terminals=take("COMPLETE_TASK", "FAIL_TASK", "TIMEOUT_TASK", "TRANSFER_TO_HUMAN", "CANCEL_TASK"),
    )


def iter_events(events: Iterable[Event]) -> Iterable[Event]:
    return sorted(events, key=lambda e: e.seq)


# ------------------------------------------------------------------ 单一判定来源
# 审计 R3-01/02/03："题内事实是否可核对"、"金额是否非法"、"关键业务阶段是否缺失"
# 这三条判据此前在 rules / evidence_record / evidence_verify 各写一份，已经漂移。
# 现在只在这里实现，三端全部调用；复核端独立的是"取值路径"，不是"判定口径"。


def _money_key(key: Any) -> bool:
    return isinstance(key, str) and "amount" in key.lower()


def _money_issues(label: str, mapping: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(mapping, Mapping):
        return problems
    for key, value in mapping.items():
        if value is None or not _money_key(key):
            continue
        if isinstance(value, (Mapping, list, tuple)):
            problems.append(f"{label}.{key}=<非标量金额>")
            continue
        if not parse_money(value).ok:
            problems.append(f"{label}.{key}={value!r}")
    return problems


def _line_money_issues(label: str, items: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(items, list):
        return problems
    for index, item in enumerate(items):
        if isinstance(item, Mapping):
            problems += _money_issues(f"{label}[{index}]", item)
    return problems


def money_field_issues(state: BusinessState, case: Case) -> list[str]:
    """收集"声称是金额但无法解析"的字段（含 preview/submit/明细）。"""
    problems: list[str] = []
    collect = state.collect_event
    if collect is not None:
        output = collect.observed_output or {}
        value = output.get("amount_value")
        if value is not None and not parse_money(value).ok:
            problems.append(f"COLLECT.amount_value={value!r}")
        problems += _money_issues("computed_amounts", output.get("computed_amounts"))
        problems += _line_money_issues("line_items", output.get("line_items"))
    preview = state.preview or {}
    problems += _money_issues("preview.amounts", state.preview_amounts)
    problems += _line_money_issues("preview.line_items", preview.get("line_items"))
    problems += _money_issues("submit.amounts", state.submit_amounts)
    submit_event = state.submit_event
    if submit_event is not None:
        problems += _line_money_issues("submit.line_items", (submit_event.observed_input or {}).get("line_items"))
    calls = state.submit_tool_calls()
    if calls:
        problems += _line_money_issues("submit.arguments.line_items", (calls[-1].arguments or {}).get("line_items"))
    reference = case.reference_result or {}
    value = reference.get("amount_value")
    if value is not None and not parse_money(value).ok:
        problems.append(f"reference.amount_value={value!r}")
    problems += _money_issues("reference.expected_amounts", reference.get("expected_amounts"))
    problems += _line_money_issues("reference.expected_line_items", reference.get("expected_line_items"))
    return problems


def money_fields_present(state: BusinessState, case: Case) -> bool:
    """是否存在任何"声称是金额"的字段（用于决定 R902 是否展示金额条件）。"""
    collect = state.collect_event
    if collect is not None:
        output = collect.observed_output or {}
        if output.get("amount_value") is not None or output.get("computed_amounts") or output.get("line_items"):
            return True
    if state.preview_amounts or (state.preview or {}).get("line_items"):
        return True
    if state.submit_amounts:
        return True
    submit_event = state.submit_event
    if submit_event is not None and (submit_event.observed_input or {}).get("line_items"):
        return True
    reference = case.reference_result or {}
    return bool(
        reference.get("amount_value") is not None
        or reference.get("expected_amounts")
        or reference.get("expected_line_items")
    )


def fact_available(state: BusinessState, case: Case, key: str) -> bool:
    """题内要求的业务事实是否可在任一权威来源核对（采集/context/金额参数）。"""
    if key == "amount_value":
        return (
            state.amount_value is not None
            or bool(state.computed_amounts)
            or bool(state.preview_amounts)
            or bool(state.submit_amounts)
        )
    if key == "buyer_id":
        return state.buyer_id is not None
    if key == "item_id":
        return state.item_id is not None
    collect = state.collect_event
    return collect is not None and (collect.observed_output or {}).get(key) is not None


def missing_critical_stages(state: BusinessState, case: Case) -> list[str]:
    """关键业务阶段缺失清单（未知步骤名只有在"顶替了某个缺失阶段"时才构成拒判理由）。"""
    missing: list[str] = []
    if not state.collects:
        missing.append("COLLECT_INVOICE_DATA")
    if not state.submits and case.reference_result.get("invoice_should_exist") is True:
        missing.append("SUBMIT_INVOICE")
    if not state.confirms and state.submits:
        missing.append("CONFIRM_INVOICE")
    return missing
