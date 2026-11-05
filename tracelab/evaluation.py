"""评测编排：批量运行、版本配对、实验清单与指标。

复现性要求（对应赛题"结果可复现"与工程约束）：
同一输入文件 + 同一规则集版本 + 同一评测器版本 + 同一配置，必须得到逐字节相同的结果哈希。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 审计 A1：版本号只在 `tracelab/__init__.py` 维护一处，评测器版本由它派生。
from . import __version__ as _PACKAGE_VERSION  # noqa: E402
from .attribution import (
    RunAssessment,
    assess_run,
    business_result_text,
    downstream_text,
    process_text,
    suggestions,
)
from .contracts import IR_VERSION, Case, Dataset, EvalProfile, Verdict
from .metrics import (
    LabelFormatError,
    Metrics,
    load_reference_labels,
    score_against_labels,
    validate_label_fields,
)
from .rules import RULE_VERSION, RULES
from .summary import summarise
from .version_compare import VersionComparison, compare_versions

EVALUATOR_VERSION = f"tracelab-evaluator/{_PACKAGE_VERSION}"



__all__ = [
    "EVALUATOR_VERSION", "CaseResult", "EvaluationResult",
    "VersionComparison", "compare_versions",
    "LabelFormatError", "Metrics", "load_reference_labels",
    "score_against_labels", "validate_label_fields", "summarise",
    "build_manifest", "evaluate_dataset",
    "write_submission", "write_results_json",
]

# --------------------------------------------------------------------- 结果模型

@dataclass
class CaseResult:
    case: Case
    assessments: list[RunAssessment]
    comparison: VersionComparison
    issues: list[str] = field(default_factory=list)

    @property
    def base(self) -> RunAssessment | None:
        """基线版判定；Case 没有任何运行（0-run）时为 None，而不是抛 IndexError。

        报告层必须能原样表达"该 Case 无可评估运行"——用异常中断整批评测
        等于把一条坏样本升级成整报告崩溃，这条路径不允许存在。
        """
        return self.assessments[0] if self.assessments else None

    @property
    def cand(self) -> RunAssessment | None:
        """候选版判定；0-run Case 返回 None（语义同 `base`）。"""
        return self.assessments[-1] if self.assessments else None

    @property
    def has_evaluable_runs(self) -> bool:
        return bool(self.assessments)

    def to_submission(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "run_assessments": [self._run_payload(a) for a in self.assessments],
            "version_comparison": {
                "direction": self.comparison.direction,
                "release_recommendation": self.comparison.release_recommendation,
                "reason": self.comparison.reason,
            },
        }

    def _run_payload(self, a: RunAssessment) -> dict[str, Any]:
        root_causes = []
        for v in a.violations:
            entry: dict[str, Any] = {
                "label": v.label,
                "first_fault_event_id": v.event_id,
                "evidence_event_ids": list(v.evidence_ids),
                "explanation": f"[{v.rule_id}@{v.rule_version}][{v.certainty}] {v.explanation}",
            }
            if v.label != "INSUFFICIENT_EVIDENCE":
                entry["confidence"] = round(v.confidence, 2)
            root_causes.append(entry)
        return {
            "run_id": a.run_id,
            "agent_task_success": a.agent_task_success,
            "business_invoice_created": a.business_invoice_created,
            "quality_score": a.quality_score,
            "business_result_assessment": business_result_text(self.case, a),
            "process_assessment": process_text(self.case, a),
            "penalty_items": a.penalty_items(),
            "root_causes": root_causes,
            "downstream_impact": downstream_text(a),
            "improvement_suggestions": suggestions(self.case, a),
            "evidence_event_ids": a.evidence_ids,
            "explanation": _explanation(a),
        }


def _explanation(a: RunAssessment) -> str:
    if a.is_unknown:
        return (
            "本运行判定为 UNKNOWN（证据不足或评测器未能完成判断），"
            "不构成 Agent 正确处理或失败的结论；已在报告中单独计数。"
        )
    if a.verdict is Verdict.PASS:
        guards = "、".join(g.event_id for g in a.guards)
        tail = f"；工具拒绝 {guards} 属于正确防护，不计为故障" if guards else ""
        return f"按题内业务规则逐项校验未发现可证实违规，Agent 处理正确{tail}。"
    first = a.first_error
    assert first is not None
    return (
        f"最早可证实错误出现在 {first.event_id}（{first.rule_id}/{first.label}）：{first.explanation}"
        + (f" 共识别 {len(a.violations)} 条根因。" if len(a.violations) > 1 else "")
    )


@dataclass
class EvaluationResult:
    dataset: Dataset
    results: list[CaseResult]
    manifest: Mapping[str, Any]
    elapsed_ms: int = 0

    def submission_rows(self) -> list[dict[str, Any]]:
        return [r.to_submission() for r in self.results]

    def result_hash(self) -> str:
        payload = json.dumps(self.submission_rows(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 审计 F6-03：metrics/summary 用 TYPE_CHECKING 引用 EvaluationResult（避免运行时循环导入）。
# 为了让 typing.get_type_hints()/文档生成工具也能解析注解，在类定义之后把类型补进
# 那两个模块的运行时命名空间（类型检查器仍走 TYPE_CHECKING 分支）。
def _expose_public_types() -> None:
    from typing import cast

    from . import metrics as metrics_module
    from . import summary as summary_module

    # mypy 会把模块属性当"类型"而禁止赋值；cast(Any) 只影响类型检查，运行时行为不变。
    cast(Any, metrics_module).EvaluationResult = EvaluationResult
    cast(Any, summary_module).EvaluationResult = EvaluationResult


_expose_public_types()


# ------------------------------------------------------------------ 实验清单

def _resolve_evaluated(extra: dict[str, Any], evaluated: bool | None) -> bool:
    """解析 evaluated 三态（审计可读性拆分，语义见 build_manifest docstring）。"""
    explicit = evaluated
    if explicit is None:
        explicit = extra.pop("evaluated", None)
    if explicit is None:
        explicit = any(
            key in extra for key in ("unknown_runs", "degraded_runs", "empty_cases", "evaluation_complete")
        )
    return bool(explicit)


def _resolve_integrity_counts(
    dataset: Dataset, extra: dict[str, Any], evaluated: bool
) -> tuple[int | None, int | None, int]:
    """解析 unknown_runs / degraded_runs / empty_cases；未评测时前两项为 None。"""
    if evaluated:
        unknown_runs: int | None = int(extra.get("unknown_runs", 0) or 0)
        degraded_runs: int | None = int(extra.get("degraded_runs", 0) or 0)
    else:
        unknown_runs = None
        degraded_runs = None
    # 0-run Case（例如官方格式里的 {"runs": []}）：没有可评估运行，整批不能算"评测完整"。
    # 未显式传入时直接从 dataset 派生——`manifest` 子命令不经过 evaluate_dataset，
    # 也必须能如实报告"存在无可评估运行的 Case"。
    explicit_empty = extra.get("empty_cases")
    if explicit_empty is None:
        empty_cases = sum(1 for case in dataset.cases if not case.runs)
    else:
        empty_cases = int(explicit_empty or 0)
    return unknown_runs, degraded_runs, empty_cases


def _build_integrity(
    dataset: Dataset,
    evaluated: bool,
    unknown_runs: int | None,
    degraded_runs: int | None,
    empty_cases: int,
    explicit_complete: bool | None,
) -> dict[str, Any]:
    """组装 integrity 四项口径（读全 / 合规 / 评测完整 / 综合判定）。"""
    if not evaluated:
        evaluation_complete: bool | None = None
    elif explicit_complete is None:
        # 有评测结果但调用方没显式声明：存在降级/未判出的运行就如实标记评测不完整。
        evaluation_complete = unknown_runs == 0 and degraded_runs == 0 and empty_cases == 0
    else:
        # 即使调用方声明完整，存在 0-run Case 也必须降级：没有运行就没有结论。
        evaluation_complete = bool(explicit_complete) and empty_cases == 0
    integrity: dict[str, Any] = {
        "issues": len(dataset.issues),
        "errors": len(dataset.errors),
        # A/B/C 三项必须分开：读全 / 合规 / 评测完整，不能合并成一个 complete
        "read_complete": dataset.read_complete,
        "schema_valid": dataset.schema_valid,
        "evaluation_complete": evaluation_complete,
        # 单项都在 integrity 里能读到，供报告层点名"是哪一项不完整"
        "unknown_runs": unknown_runs,
        "degraded_runs": degraded_runs,
        "empty_cases": empty_cases,
        "reading": dict(dataset.stats),
    }
    # 审计 F-13：UNKNOWN 不是"评测完成"的同义词留给报告的独立字段；
    # 只要存在未判出的运行，整批就不能宣称"结论完整"（与 :260 的默认口径一致）。
    # `complete` 三态：True=完整；False=有具体不完整项；None=未评测（不能断言）。
    integrity["complete"] = (
        None if not evaluated
        else (
            integrity["read_complete"]
            and integrity["schema_valid"]
            and bool(integrity["evaluation_complete"])
            and unknown_runs == 0
        )
    )
    integrity["evaluated"] = evaluated
    return integrity


def _dataset_block(dataset: Dataset) -> dict[str, Any]:
    """数据集描述块（manifest.dataset）。"""
    return {
        "source": Path(dataset.source).name,
        "sha256": dataset.sha256,
        "cases": len(dataset),
        "runs": sum(len(case.runs) for case in dataset),
        "dataset_version": next((case.dataset_version for case in dataset), ""),
        "data_origin": next((case.data_origin for case in dataset), ""),
    }


def build_manifest(
    dataset: Dataset,
    extra: Mapping[str, Any] | None = None,
    *,
    evaluated: bool | None = None,
) -> dict[str, Any]:
    """构建实验清单。

    调用约定（审计 R3-B-07 / R4-A-08）：

    * ``evaluated=False``（默认用于 `tracelab manifest`）：计数与 ``complete`` 一律
      ``None``，表示"**未评测，不能断言**"；
    * ``evaluated=True``：调用方声明已评测；未显式传入的计数按 0 处理，**由调用方负责**
      保证它们与真实评测一致（`evaluate_dataset` 会传全量计数）。

    历史说明：`tracelab manifest` 不经过评测，却把 unknown_runs 默认成 0、
    complete 默认成 True——同一份含 UNKNOWN 的数据，manifest 说"完整"、evaluate 说
    "不完整"。现在未评测时这些字段一律写 `null`（"没评测 ≠ 没有 UNKNOWN"），
    并显式标记 `evaluated: false`；只有 evaluate_dataset 传入评测结果时才给布尔值。
    """
    extra = dict(extra or {})
    evaluated = _resolve_evaluated(extra, evaluated)
    unknown_runs, degraded_runs, empty_cases = _resolve_integrity_counts(dataset, extra, evaluated)
    explicit_complete = extra.pop("evaluation_complete", None)
    integrity = _build_integrity(
        dataset, evaluated, unknown_runs, degraded_runs, empty_cases, explicit_complete
    )
    return {
        "tracelab_evaluator_version": EVALUATOR_VERSION,
        "trace_ir_version": IR_VERSION,
        # 从实际适配器取，不再硬编码 official（Generic 演示必须标成 generic）
        "adapter": {
            "name": dataset.adapter_name or "unknown",
            "version": dataset.adapter_version or "unknown",
        },
        "rulepack_version": RULE_VERSION,
        "rulepack_rules": [rule.rule_id for rule in RULES],
        "dataset": _dataset_block(dataset),
        "integrity": integrity,
        "agent_versions": sorted({run.agent_version for case in dataset for run in case.runs}),
        "case_ids": [case.case_id for case in dataset],
        **extra,
    }

# ---------------------------------------------------------------------- 主流程

def _degraded_assessment(case: Case, run, exc: BaseException) -> RunAssessment:
    """单 Case/单 Run 异常隔离：降级为"证据不足"，不中断整批，也不伪造结论。

    这是对 Scout「单 Case 失败隔离」教训的吸收：一个坏样本不能拖垮整批评测，
    但降级必须**显式可见**（rule_id=R901、confidence=0、写进 uncertainties 与 issues）。
    """
    from .rules import Violation

    detail = f"评测器内部异常，已隔离该运行并判定为 UNKNOWN：{type(exc).__name__}: {exc}"
    violation = Violation(
        rule_id="R901", rule_version=RULE_VERSION, label="INSUFFICIENT_EVIDENCE",
        event_id=None, seq=10**9, evidence_ids=(),
        explanation=detail + "；原始轨迹未被修改，结论按证据不足处理。",
        dimension="PROCESS", points=0.0, confidence=0.0,
    )
    return RunAssessment(
        case_id=case.case_id,
        run_id=getattr(run, "run_id", "<unknown>"),
        agent_version=getattr(run, "agent_version", "<unknown>"),
        # 评测器自身异常 → UNKNOWN，绝不能算成 Agent 失败/版本退化
        verdict=Verdict.UNKNOWN,
        business_invoice_created=bool(getattr(run, "invoice_created", False)),
        quality_score=0.0,
        violations=[violation],
        uncertainties=[detail],
    )


def _safe_assess(case: Case, run, profile: EvalProfile | None = None) -> tuple[RunAssessment, str | None]:
    try:
        if profile is None:
            return assess_run(case, run), None
        return assess_run(
            case, run,
            enabled_rule_ids=profile.rule_ids,
            terminal_only=profile.terminal_only,
        ), None
    except Exception as exc:  # noqa: BLE001 - 必须兜住任何样本级异常
        return _degraded_assessment(case, run, exc), f"{type(exc).__name__}: {exc}"


def evaluate_dataset(dataset: Dataset, profile: EvalProfile | None = None) -> EvaluationResult:
    started = time.perf_counter()
    results: list[CaseResult] = []
    # C. 评测完整性：任何降级（R901）或评测阶段失败都必须让本批"评测不完整"。
    # 这里显式收集失败类型，再传给 build_manifest——默认值不可信，必须由实际过程决定。
    evaluation_failures: list[str] = []
    degraded_runs = 0
    empty_cases = 0
    for case in dataset.cases:
        # 审计 F-21：case_id 互为前缀时（C1/C10），startswith 会把别人的 issue 挂到自己头上。
        # 结构化 scope 形如 `case:<id>` 或 `case:<id>/run:<rid>`，按分隔符精确匹配。
        own_prefix = f"case:{case.case_id}"
        issues = [
            f"{i.code}: {i.detail}" for i in dataset.issues
            if i.scope == own_prefix or i.scope.startswith(own_prefix + "/")
        ]
        if not case.runs:
            # 0-run Case（官方格式 {"runs": []}）：不参与指标与版本比较，但必须显式计数，
            # 不能让它静默消失，也不能让下游的 base/cand 取值把整批报告打崩。
            empty_cases += 1
            issues.append(
                "CASE_WITHOUT_RUNS: 该 Case 无可评估运行（runs 为空），不参与指标统计与版本对比。"
            )
            evaluation_failures.append("CASE_WITHOUT_RUNS")
        assessments: list[RunAssessment] = []
        comparison: VersionComparison
        try:
            for run in case.runs:
                assessment, failure = _safe_assess(case, run, profile)
                if failure:
                    issues.append(f"RUN_EVALUATION_FAILED {run.run_id}: {failure}")
                    evaluation_failures.append("RUN_EVALUATION_FAILED")
                if any(v.rule_id == "R901" for v in assessment.violations):
                    degraded_runs += 1
                assessments.append(assessment)
            if len(assessments) >= 2:
                try:
                    comparison = compare_versions(case, assessments[0], assessments[-1])
                except Exception as exc:  # noqa: BLE001
                    issues.append(f"COMPARISON_FAILED: {type(exc).__name__}: {exc}")
                    evaluation_failures.append("COMPARISON_FAILED")
                    comparison = VersionComparison(
                        "insufficient_evidence", "investigate",
                        f"版本比较阶段发生异常（{type(exc).__name__}），不下版本结论。",
                    )
            else:
                comparison = VersionComparison("insufficient_evidence", "investigate", "运行数量不足，无法比较。")
        except Exception as exc:  # noqa: BLE001 - Case 级兜底
            issues.append(f"CASE_EVALUATION_FAILED: {type(exc).__name__}: {exc}")
            evaluation_failures.append("CASE_EVALUATION_FAILED")
            assessments = [_degraded_assessment(case, run, exc) for run in case.runs]
            degraded_runs += len(assessments)
            comparison = VersionComparison("insufficient_evidence", "investigate", "Case 级评测异常，不下版本结论。")
        results.append(CaseResult(case=case, assessments=assessments, comparison=comparison, issues=issues))
    elapsed = int((time.perf_counter() - started) * 1000)
    unknown_runs = sum(1 for r in results for a in r.assessments if a.is_unknown)
    evaluation_complete = not evaluation_failures and degraded_runs == 0 and empty_cases == 0
    return EvaluationResult(
        dataset=dataset,
        results=results,
        manifest=build_manifest(
            dataset,
            extra={
                "degraded_runs": degraded_runs,
                "unknown_runs": unknown_runs,
                "empty_cases": empty_cases,
                "evaluation_complete": evaluation_complete,
            },
        ),
        elapsed_ms=elapsed,
    )



def write_submission(result: EvaluationResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in result.submission_rows()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_results_json(result: EvaluationResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest": {**result.manifest, "result_hash": result.result_hash(), "elapsed_ms": result.elapsed_ms},
        "cases": [
            {
                "case_id": cr.case.case_id,
                "scenario": cr.case.scenario,
                "comparison": {
                    "direction": cr.comparison.direction,
                    "release_recommendation": cr.comparison.release_recommendation,
                    "reason": cr.comparison.reason,
                    "quality_delta": cr.comparison.quality_delta,
                    "base_run_id": cr.comparison.base_run_id,
                    "cand_run_id": cr.comparison.cand_run_id,
                },
                "assessments": [
                    {
                        "run_id": a.run_id,
                        "agent_version": a.agent_version,
                        "agent_task_success": a.agent_task_success,
                        "business_invoice_created": a.business_invoice_created,
                        "quality_score": a.quality_score,
                        "root_causes": [
                            {"label": v.label, "rule_id": v.rule_id, "first_fault_event_id": v.event_id,
                             "evidence_event_ids": list(v.evidence_ids), "certainty": v.certainty,
                             "confidence": v.confidence, "explanation": v.explanation}
                            for v in a.violations
                        ],
                        "guards": [{"event_id": g.event_id, "code": g.code, "reason": g.reason} for g in a.guards],
                        "propagation": [{"from": e.from_event, "to": e.to_event, "why": e.why} for e in a.propagation],
                        "uncertainties": a.uncertainties,
                    }
                    for a in cr.assessments
                ],
                "issues": cr.issues,
            }
            for cr in result.results
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

