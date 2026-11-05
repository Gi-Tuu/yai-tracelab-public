"""回归闭环：把评测结论固化成可复测的期望，支持「评测—归因—优化—复测」。

为什么要做这一步
----------------
评测跑一次出的结论，下一次代码改动后没人知道有没有漂移。
把结论写成**机器可比的期望快照**（regression pack），之后每次都能回答：

1. 哪些判断没变（安全）
2. 哪些从"错"变"对"（可能是修复，需要人确认）
3. 哪些从"对"变"错"（回归，必须拦住）
4. 哪些结论变成"证据不足"（既不能算通过，也不能算退化）

注意：pack 记录的是**当前评测器的判断**，不是绝对真理。它的价值在于"变化可见"，
而不是"把某次输出当成标准答案"。

三态与旧格式
------------
G5 之后内部判定是三态（``PASS`` / ``FAIL`` / ``UNKNOWN``，见
``contracts.Verdict``）。快照 schema 升级到 ``tracelab-regression/2``，每条期望
存 ``verdict`` 字符串：

* ``PASS``    —— 当时有充分证据证明 Agent 处理正确
* ``FAIL``    —— 当时有可证实的规则违规（Agent 做错了）
* ``UNKNOWN`` —— 当时证据不足 / 评测器未能完成判断（**不是 Agent 做错了**）

旧格式 ``tracelab-regression/1`` 只有布尔 ``agent_task_success``，而 false 同时
覆盖 FAIL 与 UNKNOWN，**无法反推**。所以旧快照默认被**拒绝读取**（提示重建）；
只有显式 ``allow_legacy`` / ``--allow-legacy`` 时才按"保守模式"读取：旧
``false`` 记为 ``UNKNOWN``，并在报告里标注"旧快照降级读取"。
**任何路径都不会把旧 false 自动解释成 FAIL。**

历史快照保护
------------
``write_pack`` 默认拒绝覆盖已有快照（回归基线必须可追溯）。显式
``allow_overwrite`` / CLI ``--force`` 覆盖时，先把旧快照原样归档到
``archive/regression-pack.<schema tag>.json``（内容冲突时追加内容哈希，绝不丢历史）。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import Dataset, Verdict
from .evaluation import EVALUATOR_VERSION, EvaluationResult, evaluate_dataset
from .rules import RULE_VERSION

PACK_SCHEMA = "tracelab-regression/2"
LEGACY_PACK_SCHEMAS = frozenset({"tracelab-regression/1"})
PACK_FILENAME = "regression-pack.json"
PACK_MD_FILENAME = "regression-pack.md"
ARCHIVE_DIRNAME = "archive"

_REBUILD_HINT = (
    "请用 `python -m tracelab regression build --cases <cases.jsonl> --out <dir> --force` "
    "重建三态快照（旧文件会自动归档到 archive/）"
)


class PackFormatError(ValueError):
    """回归快照格式不可用：未知 schema 版本、缺少三态信息、字段非法。"""


class LegacyPackError(PackFormatError):
    """旧格式（``/1``）缺少三态信息，默认拒绝读取。

    把旧 ``agent_task_success=false`` 当成 FAIL，会把"证据不足"误判成 Agent 退化，
    所以这里宁可报错也不猜。
    """


class PackExistsError(FileExistsError):
    """目标位置已有回归快照，而调用方没有显式允许覆盖。"""


# ------------------------------------------------------------------ 期望条目

@dataclass(frozen=True)
class Expectation:
    """一条运行的回归期望：三态判定 + 根因 + 首因事件 + 质量分。"""

    case_id: str
    run_id: str
    agent_version: str
    verdict: Verdict
    labels: tuple[str, ...]
    first_fault_event_id: str | None
    quality_score: float
    #: 由旧格式降级读取（false → UNKNOWN）时置 True；新格式恒为 False，不写盘。
    legacy_downgraded: bool = False

    @property
    def agent_task_success(self) -> bool:
        """官方 Schema 布尔映射（只有 PASS 为 true），仅供展示/兼容，不用于比较。"""
        return self.verdict.to_success_flag()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "agent_version": self.agent_version,
            "verdict": self.verdict.value,
            "labels": list(self.labels),
            "first_fault_event_id": self.first_fault_event_id,
            "quality_score": self.quality_score,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Expectation:
        """读取新格式条目：必须带 ``verdict``，绝不从布尔值反推三态。

        C-37：坏条目（不是 object / 缺主键 / 非法 verdict / 非数组 labels）
        一律抛 :class:`PackFormatError`，由 CLI 统一兜住并返回 2，
        不允许把 ``AttributeError`` / ``KeyError`` / ``TypeError`` 漏给用户。
        """
        if not isinstance(raw, Mapping):
            raise PackFormatError(
                f"快照条目必须是 object，实际 {type(raw).__name__}。{_REBUILD_HINT}。"
            )
        if "verdict" not in raw:
            if "agent_task_success" in raw:
                raise PackFormatError(
                    f"快照条目 {raw.get('run_id')!r} 只有旧布尔字段 agent_task_success，"
                    f"缺少三态 verdict（无法区分 FAIL 与 UNKNOWN）。{_REBUILD_HINT}。"
                )
            raise PackFormatError(f"快照条目 {raw.get('run_id')!r} 缺少 verdict 字段。{_REBUILD_HINT}。")
        for field_name in ("case_id", "run_id"):
            if field_name not in raw:
                raise PackFormatError(
                    f"快照条目缺少 {field_name} 字段。{_REBUILD_HINT}。"
                )
        labels = raw.get("labels") or ()
        if not isinstance(labels, (list, tuple)):
            raise PackFormatError(
                f"快照条目 {raw.get('run_id')!r} 的 labels 必须是数组，实际 {type(labels).__name__}。"
            )
        try:
            quality = float(raw.get("quality_score", 0.0))
        except (TypeError, ValueError) as exc:
            raise PackFormatError(
                f"快照条目 {raw.get('run_id')!r} 的 quality_score 非法：{raw.get('quality_score')!r}。"
            ) from exc
        return cls(
            case_id=str(raw["case_id"]),
            run_id=str(raw["run_id"]),
            agent_version=str(raw.get("agent_version", "")),
            verdict=_parse_verdict(raw["verdict"], str(raw.get("run_id", "?"))),
            labels=tuple(labels),
            first_fault_event_id=raw.get("first_fault_event_id"),
            quality_score=quality,
        )

    @classmethod
    def from_legacy_dict(cls, raw: Mapping[str, Any]) -> Expectation:
        """保守读取旧格式：``true`` → PASS，``false`` → UNKNOWN（**不是** FAIL）。

        旧快照里 false 同时代表"确证失败"和"证据不足"，无法区分，因此保守记为
        UNKNOWN；这样最坏结果是"证据不足"，绝不会凭空报出一次回归。
        """
        if not isinstance(raw, Mapping):
            raise PackFormatError(
                f"旧快照条目必须是 object，实际 {type(raw).__name__}。{_REBUILD_HINT}。"
            )
        if not isinstance(raw.get("agent_task_success"), bool):
            raise PackFormatError(
                f"旧快照条目 {raw.get('run_id')!r} 缺少布尔字段 agent_task_success，无法降级读取。"
            )
        for field_name in ("case_id", "run_id"):
            if field_name not in raw:
                raise PackFormatError(
                    f"旧快照条目缺少 {field_name} 字段，无法降级读取。{_REBUILD_HINT}。"
                )
        success = bool(raw["agent_task_success"])
        labels = raw.get("labels") or ()
        if not isinstance(labels, (list, tuple)):
            raise PackFormatError(
                f"旧快照条目 {raw.get('run_id')!r} 的 labels 必须是数组，实际 {type(labels).__name__}。"
            )
        try:
            quality = float(raw.get("quality_score", 0.0))
        except (TypeError, ValueError) as exc:
            raise PackFormatError(
                f"旧快照条目 {raw.get('run_id')!r} 的 quality_score 非法：{raw.get('quality_score')!r}。"
            ) from exc
        return cls(
            case_id=str(raw["case_id"]),
            run_id=str(raw["run_id"]),
            agent_version=str(raw.get("agent_version", "")),
            verdict=Verdict.PASS if success else Verdict.UNKNOWN,
            labels=tuple(labels),
            first_fault_event_id=raw.get("first_fault_event_id"),
            quality_score=quality,
            legacy_downgraded=not success,
        )


def _parse_verdict(raw: Any, run_id: str) -> Verdict:
    try:
        return Verdict(str(raw).strip().upper())
    except ValueError as exc:
        raise PackFormatError(
            f"快照条目 {run_id!r} 的 verdict 非法：{raw!r}（只允许 PASS/FAIL/UNKNOWN）。"
        ) from exc


# ------------------------------------------------------------------ 兼容性

@dataclass(frozen=True)
class CompatibilityIssue:
    """快照与当前运行环境的兼容性问题。"""

    level: str        # error | warn
    field: str
    expected: str
    actual: str
    hint: str


# ------------------------------------------------------------------ 复测报告

#: 期望 → 实际 的全部合法判定：字段名 → 结论分类
#: 三态比较表（这是本模块的核心约定）：
#:
#:   PASS    → FAIL        回归（必须拦住）
#:   FAIL    → PASS        改善（需人工确认是修复而非绕过）
#:   PASS    → UNKNOWN     证据不足：**不是**回归
#:   FAIL    → UNKNOWN     证据不足：**不是**改善
#:   UNKNOWN → PASS        证据恢复：单独分类，**不得**直接认定 Agent 改善
#:   UNKNOWN → FAIL        证据丢失：单独分类，**不得**直接认定 Agent 退化
#:   UNKNOWN → UNKNOWN     无效结论：无法形成有效回归结论
_VERDICT_CHANGES: dict[tuple[Verdict, Verdict], tuple[str, str]] = {
    (Verdict.PASS, Verdict.FAIL): ("regressed", "regression"),
    (Verdict.FAIL, Verdict.PASS): ("improved", "improvement"),
    (Verdict.PASS, Verdict.UNKNOWN): ("insufficient_evidence", "pass_to_unknown"),
    (Verdict.FAIL, Verdict.UNKNOWN): ("insufficient_evidence", "fail_to_unknown"),
    (Verdict.UNKNOWN, Verdict.PASS): ("evidence_recovered", "unknown_to_pass"),
    (Verdict.UNKNOWN, Verdict.FAIL): ("evidence_lost", "unknown_to_fail"),
    (Verdict.UNKNOWN, Verdict.UNKNOWN): ("insufficient_evidence", "unknown_to_unknown"),
}

_CHANGE_TITLES = {
    "regression": "❌ 回归（期望 PASS，实际 FAIL）",
    "improvement": "✅ 改善（期望 FAIL，实际 PASS）——需人工确认是修复而非绕过",
    "pass_to_unknown": "❓ 证据不足（期望 PASS，实际 UNKNOWN）——不构成回归，但也不能算通过",
    "fail_to_unknown": "❓ 证据不足（期望 FAIL，实际 UNKNOWN）——不构成改善，也不能算通过",
    "unknown_to_pass": "🔎 证据恢复（期望 UNKNOWN，实际 PASS）——不得直接认定为 Agent 改善",
    "unknown_to_fail": "🔎 证据丢失（期望 UNKNOWN，实际 FAIL）——不得直接认定为 Agent 退化",
    "unknown_to_unknown": "❓ 无效结论（期望 UNKNOWN，实际 UNKNOWN）——无法形成有效回归结论",
}


@dataclass
class VerifyReport:
    """复测报告。

    三态变化分成五个独立统计，语义必须能分开读：

    * ``regressed``            PASS → FAIL：回归，必须拦住
    * ``improved``             FAIL → PASS：改善，需人工确认
    * ``insufficient_evidence`` PASS/FAIL → UNKNOWN 与 UNKNOWN → UNKNOWN：证据不足/无效结论
    * ``evidence_recovered``   UNKNOWN → PASS：证据恢复，**不得**算 Agent 改善
    * ``evidence_lost``        UNKNOWN → FAIL：证据丢失，**不得**算 Agent 退化

    ``has_problem`` 的语义是"**不能宣称复测通过**"：任何回归、缺失、兼容性
    error、证据不足、证据恢复/丢失、旧快照降级读取都会让它为 True。
    改善（FAIL → PASS）本身不是问题，仍需人工确认。

    C-14：``uncovered`` 记录"数据里存在、但快照没有对应期望"的运行。
    旧实现只遍历 expectations，于是"用 2 条运行的快照去验 8 条运行的数据"
    会得到 is_clean=True 并对外宣称"复测通过"——6 条运行从未被验证。
    现在只要存在未覆盖运行，就不允许宣称通过。
    """

    #: 数据完整性问题（审计 R3-05）：加载/评测期的 error、不可读、schema 无效等。
    #: 旧实现完全不看 manifest.integrity，于是数据已判 error 也能"复测通过 exit 0"。
    integrity: list[dict[str, Any]] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)
    improved: list[dict[str, Any]] = field(default_factory=list)              # FAIL → PASS
    regressed: list[dict[str, Any]] = field(default_factory=list)             # PASS → FAIL
    insufficient_evidence: list[dict[str, Any]] = field(default_factory=list)  # ↔ UNKNOWN
    evidence_recovered: list[dict[str, Any]] = field(default_factory=list)    # UNKNOWN → PASS
    evidence_lost: list[dict[str, Any]] = field(default_factory=list)         # UNKNOWN → FAIL
    changed: list[dict[str, Any]] = field(default_factory=list)               # 标签/首因变了
    missing: list[str] = field(default_factory=list)                          # 期望里的 Case/Run 不见了
    #: 数据里存在但快照没有的 `case_id/run_id`（C-14：不能静默跳过）
    uncovered: list[str] = field(default_factory=list)
    #: 数据实际评测的运行数 / 其中被快照覆盖的数（用于"覆盖 2/8"这类结论）
    data_runs: int = 0
    covered_runs: int = 0
    compatibility: list[CompatibilityIssue] = field(default_factory=list)
    #: 读取的是旧 schema（降级模式）；报告必须显式标注
    legacy_pack: bool = False
    #: 旧 false 被保守记为 UNKNOWN 的 run_id 列表
    legacy_downgraded: list[str] = field(default_factory=list)

    @property
    def compatibility_errors(self) -> list[CompatibilityIssue]:
        return [i for i in self.compatibility if i.level == "error"]

    @property
    def has_unknown_changes(self) -> bool:
        return bool(self.insufficient_evidence or self.evidence_recovered or self.evidence_lost)

    @property
    def has_problem(self) -> bool:
        """True = 本次复测**不能**被宣称"通过"。"""
        return bool(
            self.integrity
            or self.regressed
            or self.missing
            or self.uncovered
            or self.compatibility_errors
            or self.insufficient_evidence
            or self.evidence_lost
            or self.evidence_recovered
            or self.legacy_pack
        )

    @property
    def is_clean(self) -> bool:
        return not self.has_problem

    @property
    def exit_code(self) -> int:
        """CLI 退出码。

        * ``4`` 环境/数据不兼容（compatibility error）：复测前提不成立
          （审计 R3-A-08：2 已被"输入/路径错误"占用，两种语义必须能区分）
        * ``1`` 存在回归、缺失、证据不足等**不能宣称通过**的情况
        * ``0`` 只有一致 / 改善 / 判据变化
        """
        if self.compatibility_errors:
            return 4
        return 1 if self.has_problem else 0

    def change_rows(self) -> list[dict[str, Any]]:
        """三态变化明细（渲染用）：按回归→丢失→恢复→改善→证据不足 的严重度排序。"""
        groups = (self.regressed, self.evidence_lost, self.evidence_recovered,
                  self.improved, self.insufficient_evidence)
        return [row for group in groups for row in group]

    def to_dict(self) -> dict[str, Any]:
        summary = {
            "integrity": self.integrity,
            "match": len(self.matches),
            "improved": len(self.improved),
            "regressed": len(self.regressed),
            "insufficient_evidence": len(self.insufficient_evidence),
            "evidence_recovered": len(self.evidence_recovered),
            "evidence_lost": len(self.evidence_lost),
            "changed": len(self.changed),
            "missing": len(self.missing),
            "uncovered": len(self.uncovered),
            "data_runs": self.data_runs,
            "covered_runs": self.covered_runs,
            "legacy_pack": self.legacy_pack,
            "legacy_downgraded": len(self.legacy_downgraded),
            "has_problem": self.has_problem,
            "exit_code": self.exit_code,
        }
        return {
            "summary": summary,
            "integrity": self.integrity,
            # 兼容旧字段名（旧消费方仍能读到计数）
            "match": summary["match"],
            "improved": self.improved,
            "regressed": self.regressed,
            "changed": self.changed,
            "missing": self.missing,
            "uncovered": self.uncovered,
            "compatibility": [
                {"level": i.level, "field": i.field, "expected": i.expected,
                 "actual": i.actual, "hint": i.hint}
                for i in self.compatibility
            ],
            "insufficient_evidence": self.insufficient_evidence,
            "evidence_recovered": self.evidence_recovered,
            "evidence_lost": self.evidence_lost,
            "legacy_downgraded": self.legacy_downgraded,
            "has_problem": self.has_problem,
            "exit_code": self.exit_code,
        }


# ------------------------------------------------------------------ 生成快照

def build_expectations(result: EvaluationResult) -> list[Expectation]:
    out: list[Expectation] = []
    for cr in result.results:
        for a in cr.assessments:
            first = a.first_error
            out.append(Expectation(
                case_id=cr.case.case_id,
                run_id=a.run_id,
                agent_version=a.agent_version,
                verdict=a.verdict,
                labels=tuple(a.labels),
                first_fault_event_id=first.event_id if first else None,
                quality_score=a.quality_score,
            ))
    return out


def _schema_tag(schema: str) -> str:
    """`tracelab-regression/1` → `v1`；未知格式 → `unknown`。"""
    tail = schema.rsplit("/", 1)[-1]
    return f"v{tail}" if tail.isdigit() else "unknown"


def _archive_existing(out_dir: Path, json_path: Path, md_path: Path) -> None:
    """覆盖前把旧快照原样归档，保证历史永不丢失。"""
    schema = ""
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        # 审计 R2-14：旧实现对"合法但非 object 的 JSON"直接 .get → AttributeError，
        # 覆盖归档时打穿 CLI。这里只接受 object，其余按 unknown 归档。
        schema = str(payload.get("schema") or "") if isinstance(payload, Mapping) else ""
    tag = _schema_tag(schema)
    archive = out_dir / ARCHIVE_DIRNAME
    archive.mkdir(parents=True, exist_ok=True)
    for src, stem in ((json_path, f"regression-pack.{tag}.json"), (md_path, f"regression-pack.{tag}.md")):
        if not src.exists():
            continue
        content = src.read_bytes()
        dst = archive / stem
        if dst.exists():
            if dst.read_bytes() == content:
                continue  # 幂等：同一份历史不重复归档
            # 同名归档已存在且内容不同：追加内容哈希，绝不覆盖历史
            digest = hashlib.sha256(content).hexdigest()[:8]
            dst = archive / f"{Path(stem).stem}-{digest}{Path(stem).suffix}"
            if dst.exists():
                continue
        dst.write_bytes(content)


def _atomic_write_text(path: Path, text: str) -> None:
    """原子落盘；显式用 LF，避免 Windows 上产出 CRLF 让快照字节不可复现。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_pack(
    expectations: list[Expectation],
    out_dir: Path,
    dataset: Dataset,
    *,
    allow_overwrite: bool = False,
) -> Path:
    """写回归快照。

    默认**拒绝覆盖**已有快照（历史基线必须可追溯）：同名快照存在时抛
    :class:`PackExistsError`。显式 ``allow_overwrite=True`` 时先把旧文件归档到
    ``archive/``，再写新文件——任何情况下都不丢历史。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / PACK_FILENAME
    md_path = out_dir / PACK_MD_FILENAME
    if path.exists() or md_path.exists():
        if not allow_overwrite:
            raise PackExistsError(
                f"已存在回归快照：{path}。历史快照默认不被覆盖（回归基线必须可追溯）；"
                f"如需重建请显式允许覆盖（CLI 加 --force，旧文件会自动归档到 "
                f"{ARCHIVE_DIRNAME}/），或改用新的 --out 目录。"
            )
        _archive_existing(out_dir, path, md_path)

    payload = {
        "schema": PACK_SCHEMA,
        "created_from": {
            "dataset": Path(dataset.source).name,
            "dataset_sha256": dataset.sha256,
            "dataset_version": next((c.dataset_version for c in dataset), ""),
            "evaluator_version": EVALUATOR_VERSION,
            "rulepack_version": RULE_VERSION,
        },
        "note": "记录的是当前评测器判断（三态 PASS/FAIL/UNKNOWN），不是绝对真理；价值在于变化可见。",
        "expectations": [e.to_dict() for e in expectations],
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    _atomic_write_text(md_path, _render_pack(payload))
    return path


def _render_pack(payload: Mapping[str, Any]) -> str:
    meta = payload["created_from"]
    exp: list[dict[str, Any]] = payload["expectations"]
    failing = [e for e in exp if e["verdict"] == Verdict.FAIL.value]
    unknown = [e for e in exp if e["verdict"] == Verdict.UNKNOWN.value]
    passing = [e for e in exp if e["verdict"] == Verdict.PASS.value]
    lines = [
        "# 回归用例包",
        "",
        f"- 数据：`{meta['dataset']}`（SHA-256 `{meta['dataset_sha256'][:16]}…`）",
        f"- 评测器：`{meta['evaluator_version']}`　规则集：`{meta['rulepack_version']}`",
        f"- 快照格式：`{payload['schema']}`（三态 PASS/FAIL/UNKNOWN）",
        f"- 期望快照：{len(exp)} 条运行 —— PASS {len(passing)}、"
        f"FAIL {len(failing)}、UNKNOWN {len(unknown)}",
        "",
        "> 这份快照记录的是**当前评测器的判断**，不是绝对真理。",
        "> 它的用途是让「判断漂移」可见：修复后应当出现 improved，回归应当出现 regressed。",
        "> UNKNOWN 表示「证据不足/评测器未完成判断」，**不是** Agent 做错了；"
        "复测时它既不算回归也不算改善。",
        "",
        "## 处理错误的运行 FAIL（可用于复测）",
        "",
        "| run | 根因 | 首因事件 | 质量分 |",
        "|---|---|---|---|",
    ]
    for e in failing:
        lines.append(
            f"| `{e['run_id']}` | {', '.join(e['labels']) or '—'} | "
            f"`{e['first_fault_event_id']}` | {e['quality_score']} |"
        )
    lines.append("")
    lines.append("## 处理正确的运行 PASS（回归守门）")
    lines.append("")
    lines.append("| run | 质量分 |")
    lines.append("|---|---|")
    for e in passing:
        lines.append(f"| `{e['run_id']}` | {e['quality_score']} |")
    lines.append("")
    lines.append("## 证据不足的运行 UNKNOWN（不参与回归判定）")
    lines.append("")
    lines.append("| run | 根因 | 质量分 |")
    lines.append("|---|---|---|")
    for e in unknown:
        lines.append(f"| `{e['run_id']}` | {', '.join(e['labels']) or '—'} | {e['quality_score']} |")
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- 复测

def _legacy_message(path: str, schema: str) -> str:
    return (
        f"旧快照 {path} 的格式是 {schema}：只有布尔 agent_task_success，没有三态信息。"
        f"把旧 false 当成 FAIL，会把「证据不足 UNKNOWN」误判成 Agent 退化，因此默认拒绝读取。"
        f"{_REBUILD_HINT}；若只想核对历史，可加 --allow-legacy 进入保守模式"
        f"（旧 false 记为 UNKNOWN，报告中标注「旧快照降级读取」）。"
    )


def load_pack(path: Path, *, allow_legacy: bool = False) -> dict[str, Any]:
    """读取回归快照。

    新格式（``/2``）直接返回；旧格式（``/1``）默认抛 :class:`LegacyPackError`，
    只有 ``allow_legacy=True`` 才降级返回（读取阶段不改写，三态降级在
    :func:`verify_pack` 里做）。

    C-37：坏 JSON / 非 object 顶层 / 读不出来一律转成 :class:`PackFormatError`。
    CLI 只 catch 这一个异常类型，旧实现会让 ``JSONDecodeError`` /
    ``AttributeError`` 直接冒泡成崩溃。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PackFormatError(
            f"回归包无法读取：{path}（{type(exc).__name__}: {exc}）。{_REBUILD_HINT}。"
        ) from exc
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError 是 ValueError 子类
        raise PackFormatError(
            f"回归包不是合法 JSON：{path}（{type(exc).__name__}: {exc}）。{_REBUILD_HINT}。"
        ) from exc
    if not isinstance(payload, dict):
        raise PackFormatError(
            f"回归包顶层必须是 object，实际 {type(payload).__name__}。{_REBUILD_HINT}。"
        )
    schema = payload.get("schema")
    if schema == PACK_SCHEMA or schema in LEGACY_PACK_SCHEMAS:
        if schema in LEGACY_PACK_SCHEMAS and not allow_legacy:
            raise LegacyPackError(_legacy_message(str(path), str(schema)))
        return payload
    raise PackFormatError(f"未知的回归包格式：{schema!r}（期望 {PACK_SCHEMA}）")


def expectations_from_pack(
    pack: Mapping[str, Any], *, allow_legacy: bool = False, source: str = "<pack>"
) -> tuple[list[Expectation], list[str]]:
    """从快照数据里取出期望，并返回「被降级读取的 run_id」列表。"""
    schema = pack.get("schema")
    if schema not in ({PACK_SCHEMA} | LEGACY_PACK_SCHEMAS):
        raise PackFormatError(f"未知的回归包格式：{schema!r}（期望 {PACK_SCHEMA}）")
    entries = pack.get("expectations")
    if not isinstance(entries, list):
        raise PackFormatError(f"回归包缺少 expectations 数组（schema={schema!r}）。{_REBUILD_HINT}。")
    if schema == PACK_SCHEMA:
        return [Expectation.from_dict(x) for x in entries], []
    if not allow_legacy:
        raise LegacyPackError(_legacy_message(source, str(schema)))
    expectations = [Expectation.from_legacy_dict(x) for x in entries]
    return expectations, [e.run_id for e in expectations if e.legacy_downgraded]


def check_compatibility(
    pack: Mapping[str, Any], dataset: Dataset, *, allow_data_evolution: bool = False
) -> list[CompatibilityIssue]:
    """校验快照与当前数据/规则/评测器是否同一环境。

    数据哈希不一致默认是**错误**：不能把"换了数据"静默说成"原始回归测试通过"。
    数据演进需要显式开关，且仍以 warn 保留可见性。
    """
    meta = dict(pack.get("created_from") or {})
    issues: list[CompatibilityIssue] = []

    if meta.get("dataset_sha256") and meta["dataset_sha256"] != dataset.sha256:
        issues.append(CompatibilityIssue(
            "warn" if allow_data_evolution else "error",
            "dataset_sha256", str(meta.get("dataset_sha256")), dataset.sha256,
            "数据已变化：默认不可直接比较；确认是数据演进请加 --allow-data-evolution，并保留旧快照。",
        ))
    if meta.get("rulepack_version") and meta["rulepack_version"] != RULE_VERSION:
        issues.append(CompatibilityIssue(
            "error", "rulepack_version", str(meta.get("rulepack_version")), RULE_VERSION,
            "规则集版本不同，旧快照不能证明新规则的回归结果；请用新规则重建快照。",
        ))
    if meta.get("evaluator_version") and meta["evaluator_version"] != EVALUATOR_VERSION:
        issues.append(CompatibilityIssue(
            "warn", "evaluator_version", str(meta.get("evaluator_version")), EVALUATOR_VERSION,
            "评测器版本不同，判定口径可能变化，结论需人工确认。",
        ))
    current_version = next((c.dataset_version for c in dataset), "")
    if meta.get("dataset_version") and current_version and meta["dataset_version"] != current_version:
        issues.append(CompatibilityIssue(
            "warn", "dataset_version", str(meta["dataset_version"]), current_version,
            "数据包版本不同，可能是不同批次的数据。",
        ))
    return issues


def verify_pack(
    pack: Mapping[str, Any],
    dataset: Dataset,
    *,
    allow_data_evolution: bool = False,
    allow_legacy: bool = False,
    source: str = "<pack>",
) -> VerifyReport:
    """按三态比较快照与当前评测结果，产出可审计的复测报告。"""
    expectations, legacy_downgraded = expectations_from_pack(
        pack, allow_legacy=allow_legacy, source=source
    )
    result = evaluate_dataset(dataset)
    actual: dict[tuple[str, str], Any] = {
        (cr.case.case_id, a.run_id): a for cr in result.results for a in cr.assessments
    }

    report = VerifyReport(
        legacy_pack=pack.get("schema") in LEGACY_PACK_SCHEMAS,
        legacy_downgraded=legacy_downgraded,
    )
    report.compatibility = check_compatibility(pack, dataset, allow_data_evolution=allow_data_evolution)
    # 审计 R3-05：数据完整性是"能否宣称复测通过"的前提，必须进 has_problem 与报告。
    integrity = result.manifest.get("integrity") or {}
    reading = integrity.get("reading") or {}
    errors = int(integrity.get("errors") or 0) or len(dataset.errors)
    problems: list[dict[str, Any]] = []
    if errors:
        problems.append({"field": "errors", "value": errors,
                         "detail": f"加载期存在 {errors} 项 error，结论只对可解析部分成立"})
    skipped = int(reading.get("skipped") or 0)
    truncated = int(reading.get("truncated") or 0)
    if skipped or truncated:
        problems.append({"field": "reading", "value": {"skipped": skipped, "truncated": truncated},
                         "detail": "读取不完整（坏行/截断），复测只覆盖可解析部分"})
    if integrity.get("evaluation_complete") is False:
        problems.append({"field": "evaluation_complete", "value": False,
                         "detail": "评测未完成（存在降级/未完成判定的运行）"})
    report.integrity = problems
    if report.legacy_pack:
        report.compatibility.append(CompatibilityIssue(
            "warn", "schema", str(pack.get("schema")), PACK_SCHEMA,
            "旧快照降级读取：旧 agent_task_success=false 已按 UNKNOWN 保守处理，"
            "本次结论不等同于三态快照复测，请尽快用新代码重建。",
        ))

    for expect in expectations:
        key = (expect.case_id, expect.run_id)
        got = actual.get(key)
        if got is None:
            report.missing.append(f"{expect.case_id}/{expect.run_id}")
            continue

        entry: dict[str, Any] = {
            "case_id": expect.case_id,
            "run_id": expect.run_id,
            "expected_verdict": expect.verdict.value,
            "actual_verdict": got.verdict.value,
            "actual_labels": got.labels,
        }
        change = _VERDICT_CHANGES.get((expect.verdict, got.verdict))
        if change is not None:
            bucket, classification = change
            getattr(report, bucket).append({**entry, "classification": classification})
            continue

        # 结论同向（PASS/PASS 或 FAIL/FAIL）：根因/首因变了仍值得人看一眼
        first = got.first_error.event_id if got.first_error else None
        if tuple(got.labels) != expect.labels or first != expect.first_fault_event_id:
            report.changed.append({
                **entry,
                "expected_labels": list(expect.labels),
                "actual_labels": got.labels,
                "expected_first_fault": expect.first_fault_event_id,
                "actual_first_fault": first,
            })
            continue

        report.matches.append(f"{expect.case_id}/{expect.run_id}")

    # C-14：反向覆盖检查。只遍历 expectations 会让"数据比快照多"完全不可见。
    expected_keys = {(e.case_id, e.run_id) for e in expectations}
    report.data_runs = len(actual)
    report.covered_runs = sum(1 for key in actual if key in expected_keys)
    report.uncovered = sorted(
        f"{case_id}/{run_id}" for (case_id, run_id) in actual if (case_id, run_id) not in expected_keys
    )
    return report


def render_verify(report: VerifyReport, pack_meta: Mapping[str, Any]) -> str:
    """渲染复测报告（编排层：表头 → 完整性 → 兼容性 → 三态明细 → 结论）。"""
    lines = _verify_header(report, pack_meta)
    lines += _verify_integrity(report)
    lines += _verify_compatibility(report)
    lines += _verify_changes(report)
    lines += _verify_conclusion(report)
    return "\n".join(lines)


def _verify_header(report: VerifyReport, pack_meta: Mapping[str, Any]) -> list[str]:
    lines = [
        "# 回归复测报告",
        "",
        f"- 快照来源：`{pack_meta.get('dataset', '?')}`　评测器 `{pack_meta.get('evaluator_version', '?')}`"
        f"　规则集 `{pack_meta.get('rulepack_version', '?')}`",
        f"- 一致 {len(report.matches)}　改善 {len(report.improved)}　**回归 {len(report.regressed)}**"
        f"　证据不足 {len(report.insufficient_evidence)}　证据恢复 {len(report.evidence_recovered)}"
        f"　证据丢失 {len(report.evidence_lost)}　判据变化 {len(report.changed)}"
        f"　缺失 {len(report.missing)}　未覆盖 {len(report.uncovered)}",
        "",
    ]
    if report.legacy_pack:
        lines += [
            "> ⚠ **旧快照降级读取**：快照是 `tracelab-regression/1`（只有布尔判定），"
            f"其中 {len(report.legacy_downgraded)} 条 `agent_task_success=false` 已按 **UNKNOWN** "
            "保守处理，不代表 Agent 失败。本次结论不能替代三态快照复测，请重建快照。",
            "",
        ]
    return lines


def _verify_integrity(report: VerifyReport) -> list[str]:
    lines: list[str] = []
    lines += ["## 数据完整性", ""]
    if not report.integrity:
        lines += ["本批数据完整性检查通过（读取完整、schema 有效、无加载期 error）。", ""]
    else:
        lines += ["| 项目 | 值 | 说明 |", "|---|---|---|"]
        for item in report.integrity:
            lines.append(f"| `{item.get('field')}` | {item.get('value')} | {item.get('detail', '')} |")
        lines += ["", "> ⚠ 数据完整性问题会让「复测通过」不成立：结论只对可解析部分有效。", ""]
    return lines


def _verify_compatibility(report: VerifyReport) -> list[str]:
    lines: list[str] = []
    if report.compatibility:
        lines += ["## 环境兼容性", "", "| 级别 | 项目 | 快照值 | 当前值 | 说明 |", "|---|---|---|---|---|"]
        for issue in report.compatibility:
            mark = "❌ error" if issue.level == "error" else "⚠ warn"
            lines.append(
                f"| {mark} | `{issue.field}` | `{issue.expected}` | `{issue.actual}` | {issue.hint} |"
            )
        lines.append("")
        if report.compatibility_errors:
            lines.append("> **快照与当前环境不一致：本次复测不能声称为「原始回归测试通过」。**")
            lines.append("")
    return lines


def _verify_changes(report: VerifyReport) -> list[str]:
    lines: list[str] = []
    if report.change_rows():
        lines += [
            "## 三态变化明细",
            "",
            "| run | 期望 verdict | 实际 verdict | 结论分类 |",
            "|---|---|---|---|",
        ]
        for row in report.change_rows():
            mark = _CHANGE_TITLES.get(row["classification"], row["classification"])
            lines.append(
                f"| `{row['run_id']}` | {row['expected_verdict']} | {row['actual_verdict']} | {mark} |"
            )
        lines.append("")

    if report.regressed:
        lines += ["## ❌ 回归（期望 PASS，实际 FAIL）", "", "| run | 现在给出的根因 |", "|---|---|"]
        lines += [f"| `{x['run_id']}` | {', '.join(x['actual_labels']) or '—'} |" for x in report.regressed]
        lines.append("")
    if report.evidence_lost:
        lines += ["## 🔎 证据丢失（期望 UNKNOWN，实际 FAIL）——不得直接认定为 Agent 退化", "",
                  "| run | 现在的根因 |", "|---|---|"]
        lines += [f"| `{x['run_id']}` | {', '.join(x['actual_labels']) or '—'} |" for x in report.evidence_lost]
        lines.append("")
    if report.evidence_recovered:
        lines += ["## 🔎 证据恢复（期望 UNKNOWN，实际 PASS）——不得直接认定为 Agent 改善", ""]
        lines += [f"- `{x['run_id']}`" for x in report.evidence_recovered]
        lines.append("")
    if report.improved:
        lines += ["## ✅ 改善（期望 FAIL，实际 PASS）——需人工确认是修复而非绕过", "",
                  "| run | 现在的根因 |", "|---|---|"]
        lines += [f"| `{x['run_id']}` | {', '.join(x['actual_labels']) or '—'} |" for x in report.improved]
        lines.append("")
    if report.insufficient_evidence:
        lines += ["## ❓ 证据不足（涉及 UNKNOWN）——既不是回归也不是改善", "",
                  "| run | 期望 | 实际 | 说明 |", "|---|---|---|---|"]
        for x in report.insufficient_evidence:
            lines.append(
                f"| `{x['run_id']}` | {x['expected_verdict']} | {x['actual_verdict']} | "
                f"{_CHANGE_TITLES.get(x['classification'], x['classification'])} |"
            )
        lines.append("")
    if report.changed:
        lines += ["## ⚠ 判据变化（结论同向，但根因或首因事件变了）", "",
                  "| run | 期望根因 | 实际根因 | 期望首因 | 实际首因 |", "|---|---|---|---|---|"]
        lines += [
            f"| `{x['run_id']}` | {', '.join(x['expected_labels']) or '—'} | "
            f"{', '.join(x['actual_labels']) or '—'} | `{x['expected_first_fault']}` | "
            f"`{x['actual_first_fault']}` |"
            for x in report.changed
        ]
        lines.append("")
    if report.missing:
        lines += ["## ❓ 缺失（快照里的运行在新数据中不存在）", ""]
        lines += [f"- `{m}`" for m in report.missing]
        lines.append("")
    if report.uncovered:
        lines += [
            "## ❓ 未覆盖（数据里存在、但快照没有任何期望的运行）",
            "",
            "> 这些运行**从未被复测**：即使它们全部出错，也不会出现在回归里。"
            "不能把「快照覆盖的那部分一致」当成「整批数据都合格」。",
            "",
        ]
        lines += [f"- `{u}`" for u in report.uncovered]
        lines.append("")
    return lines


def _verify_conclusion(report: VerifyReport) -> list[str]:
    lines: list[str] = []
    lines.append("## 结论")
    lines.append("")
    coverage = (
        f"数据覆盖 {report.covered_runs}/{report.data_runs}，"
        + (f"{len(report.uncovered)} 条未覆盖。" if report.uncovered else "无未覆盖运行。")
    )
    lines.append(coverage)
    lines.append("")
    if report.is_clean:
        lines.append("复测通过：没有回归、缺失、未覆盖、兼容性错误，也没有涉及 UNKNOWN 的三态变化。")
    else:
        reasons: list[str] = []
        if report.regressed:
            reasons.append(f"{len(report.regressed)} 条回归")
        if report.evidence_lost:
            reasons.append(f"{len(report.evidence_lost)} 条证据丢失（UNKNOWN → FAIL）")
        if report.evidence_recovered:
            reasons.append(f"{len(report.evidence_recovered)} 条证据恢复（UNKNOWN → PASS）")
        if report.insufficient_evidence:
            reasons.append(f"{len(report.insufficient_evidence)} 条证据不足/无效结论（涉及 UNKNOWN）")
        if report.missing:
            reasons.append(f"{len(report.missing)} 条缺失")
        if report.uncovered:
            reasons.append(f"{len(report.uncovered)} 条未覆盖（数据里有、快照没有）")
        if report.legacy_pack:
            reasons.append("旧快照降级读取")
        if report.compatibility_errors:
            reasons.append(f"{len(report.compatibility_errors)} 项环境不兼容")
        lines.append(
            "**复测未通过**："
            + "、".join(reasons)
            + "。证据不足或涉及 UNKNOWN 的变化不能算作「通过」，需补齐证据/重建快照后复测。"
        )
    lines.append("")
    return lines


def write_verify(report: VerifyReport, out_dir: Path, meta: Mapping[str, Any]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "regression-verify.md"
    js = out_dir / "regression-verify.json"
    _atomic_write_text(md, render_verify(report, meta))
    _atomic_write_text(js, json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return md, js
