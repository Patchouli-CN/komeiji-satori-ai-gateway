"""Checker 注册表：装饰器注册 + 包扫描自动发现（类 Spring @ComponentScan）。

用法：
    from .registry import register_checker

    @register_checker
    class MyChecker:
        name = "mine"

        @classmethod
        def from_config(cls, cfg: Config) -> "MyChecker | None":
            # 返回 None 表示该 checker 在当前配置下禁用
            return cls(cfg.mine) if cfg.mine.enabled else None

        async def check(self, client, upstream, model): ...

新增 checker 只要把模块放进 checkers/ 包并加装饰器，
serve 启动时 discover() 会自动导入并装配。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from .checkers import Checker
from .config import Config

log = logging.getLogger("satori")

# 注册表：checker 类列表（注册顺序即执行顺序）
_REGISTRY: list[type] = []
_discovered = False


def register_checker(cls: type) -> type:
    """装饰器：把 checker 类加入注册表。类必须提供 from_config()。"""
    if not hasattr(cls, "from_config"):
        raise TypeError(f"{cls.__name__} 缺少 from_config(cfg) 工厂方法")
    _REGISTRY.append(cls)
    return cls


def discover() -> None:
    """扫描 checkers 包下所有模块，触发其中的装饰器注册。"""
    global _discovered
    if _discovered:
        return
    from . import checkers

    for mod in pkgutil.iter_modules(checkers.__path__):
        importlib.import_module(f"{checkers.__name__}.{mod.name}")
    _discovered = True
    log.info("checker 自动发现完成：%d 个已注册", len(_REGISTRY))


def build_checkers(cfg: Config) -> list[Checker]:
    """按注册表装配本次启用的 checker。"""
    discover()
    built: list[Checker] = []
    for cls in _REGISTRY:
        checker = cls.from_config(cfg)
        if checker is not None:
            built.append(checker)
            log.info("checker 启用: %s", checker.name)
        else:
            log.info("checker 跳过（配置禁用）: %s", cls.__name__)
    return built
