"""兼容入口：实现已迁移到 `judge_metrics.py`，避免与初赛 `tracelab/metrics.py` 同名歧义。"""

from .judge_metrics import VERDICTS, compute_metrics, pair_runs, strict_bool

__all__ = ["VERDICTS", "compute_metrics", "pair_runs", "strict_bool"]
