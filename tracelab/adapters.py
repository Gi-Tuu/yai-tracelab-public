"""数据适配层入口：注册表与 load_dataset。

具体实现按 adapter 拆到 `adapters_official.py` / `adapters_generic.py`，
公共工具在 `adapters_common.py`；本模块只保留注册与分发（公开导入名称不变）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .adapters_common import STEP_ALIASES, Adapter, sha256_file
from .adapters_generic import GenericJSONLAdapter
from .adapters_official import OfficialJSONLAdapter
from .contracts import Dataset

__all__ = [
    "Adapter", "STEP_ALIASES", "sha256_file",
    "OfficialJSONLAdapter", "GenericJSONLAdapter",
    "REGISTRY", "register_adapter", "get_adapter", "load_dataset",
]

REGISTRY: dict[str, Callable[[], Adapter]] = {
    "official": OfficialJSONLAdapter,
    "generic": GenericJSONLAdapter,
}


def register_adapter(name: str, factory: Callable[[], Adapter]) -> None:
    if name in REGISTRY:
        raise ValueError(f"adapter {name!r} 已存在，禁止静默覆盖")
    REGISTRY[name] = factory


def get_adapter(name: str = "official") -> Adapter:
    if name not in REGISTRY:
        raise KeyError(f"未知 adapter {name!r}；可用：{sorted(REGISTRY)}")
    return REGISTRY[name]()


def load_dataset(path: Path, adapter: str = "official") -> Dataset:
    return get_adapter(adapter).load(path)
