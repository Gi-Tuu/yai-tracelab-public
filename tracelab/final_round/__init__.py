"""复赛（双业务 + 专属 Judge）核心：数据校验、输入白名单、四态结果、收据链与版本比较。

`ScriptedJudge` 用于链路验证；`OpenAICompatJudge` 走真实 OpenAI 兼容网关。
"""

from __future__ import annotations

from .controls import (
    ControlBatch,
    ControlFormatError,
    load_controls,
    run_control_file,
    run_controls,
    write_control_batch,
)
from .judge import JudgeClient, ScriptedJudge
from .judge_input import (
    JudgeInputContractError,
    build_judge_input,
    normalize_judge_input,
    sha256_json,
    validate_observable_trace,
)
from .llm_judge import JudgeOutputError, OpenAICompatJudge, build_messages, parse_judge_output
from .load import FinalRoundFormatError, load_cases, validate_case, validate_cases
from .provider import ChatResult, ProviderError, chat_completions, list_models, resolve_api_key
from .receipts import JudgeReceipt, ReceiptChain
from .results import build_case_result, compare_versions, normalize_assessment, validate_case_result
from .runner import BatchOutput, run_cases, run_file, write_batch

__all__ = [
    "BatchOutput",
    "FinalRoundFormatError",
    "ChatResult",
    "ControlBatch",
    "ControlFormatError",
    "JudgeClient",
    "JudgeInputContractError",
    "JudgeOutputError",
    "JudgeReceipt",
    "OpenAICompatJudge",
    "ProviderError",
    "ReceiptChain",
    "ScriptedJudge",
    "build_case_result",
    "build_judge_input",
    "build_messages",
    "chat_completions",
    "compare_versions",
    "list_models",
    "parse_judge_output",
    "resolve_api_key",
    "load_cases",
    "load_controls",
    "normalize_assessment",
    "normalize_judge_input",
    "run_cases",
    "run_control_file",
    "run_controls",
    "run_file",
    "sha256_json",
    "validate_case",
    "validate_case_result",
    "validate_cases",
    "validate_observable_trace",
    "write_batch",
    "write_control_batch",
]
