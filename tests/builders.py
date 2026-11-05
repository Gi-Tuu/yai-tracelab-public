"""测试用轨迹构造器（薄再导出）。

真正的实现已移到 `tracelab/samples.py`：变形实验命令需要同一份自造样本，
放在产品侧可以让「评委一条命令复现实验数字」和「单元测试」共用同一事实来源，避免漂移。

本模块只做名字再导出，保持既有测试的 `from tests.builders import …` 不受影响。
"""

from __future__ import annotations

from tracelab.samples import (
    basic_flow,
    context,
    make_case,
    make_event,
    make_run,
    reference,
)

__all__ = [
    "basic_flow",
    "context",
    "make_case",
    "make_event",
    "make_run",
    "reference",
]
