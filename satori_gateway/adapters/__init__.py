"""协议适配器注册表：客户端协议 ↔ 内部规范（OpenAI Chat Completions）。

适配器只做协议互转，检测核心（规则/侧信道/录制/账本）永远工作在规范格式上。
新增协议：在 adapters/ 里放一个模块，实现 Adapter 协议并加 @register_adapter，
包扫描会自动发现（与 checkers 注册表同款套路）。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Protocol, runtime_checkable

from ..logger import LoggerManager

log = LoggerManager.get_logger("ADAPTERS")


@runtime_checkable
class Adapter(Protocol):
    name: str
    path: str  # 客户端入口路径，如 /v1/chat/completions
    passthrough: bool  # 与规范同构时为 True，走透传快车道

    def to_canonical(self, body: dict) -> dict:
        """客户端请求体 → 规范（Chat Completions）请求体。"""
        ...

    def from_canonical(self, payload: dict) -> dict:
        """规范响应（非流式）→ 客户端协议响应。"""
        ...

    def translate_sse(self, chunk: dict, state: dict) -> list[dict]:
        """规范 SSE 块 → 客户端协议 SSE 事件列表 [{"event":..., "data":...}]。
        state 为每请求字典，适配器可存进度。"""
        ...

    def finish_sse(self, state: dict) -> list[dict]:
        """流结束时补发的收尾事件。"""
        ...


_ADAPTERS: list[Adapter] = []
_discovered = False


def register_adapter(cls: type) -> type:
    """装饰器：实例化并注册一个协议适配器。"""
    _ADAPTERS.append(cls())
    return cls


def discover_adapters() -> None:
    global _discovered
    if _discovered:
        return
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")
    _discovered = True
    log.info("adapter 自动发现完成：{} 个协议入口", len(_ADAPTERS))


def get_adapters() -> list[Adapter]:
    discover_adapters()
    return list(_ADAPTERS)
