"""日志工具 桥接标准 logging 到 Loguru"""

# 改编自 GensokyoAI/utils/logging.py

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging as std_logging
import os
import sys
from pathlib import Path

import loguru
from loguru import logger

# 默认关闭完整堆栈，避免日志被异常 traceback 刷屏；可通过环境变量开启
_LOGURU_FULL_TRACEBACK = os.environ.get("LOGURU_FULL_TRACEBACK", "0").lower() in (
    "1",
    "true",
    "yes",
)

# 默认抑制部分底层库的低级别日志，避免污染终端/文件
_SUPPRESSED_LOW_LEVEL_LOGGERS = {"httpcore", "httpx", "asyncio", "aiohttp"}

# 第三方框架的命名空间：WARNING 以下一律丢弃
_SUPPRESSED_THIRD_PARTY_LOGGERS = {"nonebot", "uvicorn", "websockets"}


def _third_party_noise_filter(record) -> bool:
    """loguru sink 过滤器：第三方框架只保留 WARNING 及以上，自家日志不受限。"""
    name = (record["name"] or "").split(".")[0]
    if name in _SUPPRESSED_THIRD_PARTY_LOGGERS:
        return record["level"].no >= logger.level("WARNING").no
    return True


# 移除默认配置
logger.remove()

_handlers: dict[str, int | None] = {"console": None, "file": None}


class LoguruHandler(std_logging.Handler):
    def emit(self, record: std_logging.LogRecord):
        if (
            record.name.split(".")[0] in _SUPPRESSED_LOW_LEVEL_LOGGERS
            and record.levelno < std_logging.WARNING
        ):
            return

        # uvicorn 关停时的 KeyboardInterrupt/CancelledError 级联 traceback 不转发
        if record.name.split(".")[0] == "uvicorn":
            exc_type = record.exc_info[0] if record.exc_info else None
            if exc_type is not None and issubclass(
                exc_type, (KeyboardInterrupt, asyncio.CancelledError)
            ):
                return
            message = record.getMessage()
            if message.startswith("Traceback") and (
                "KeyboardInterrupt" in message
                or "asyncio.exceptions.CancelledError" in message
            ):
                return

        # 把其他库的 DEBUG 降级为我们的 TRACE
        level: str | int
        if record.levelno == std_logging.DEBUG:
            level = "TRACE"
        else:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame and (
            depth == 0 or frame.f_code.co_filename == std_logging.__file__
        ):
            frame = frame.f_back
            depth += 1

        exc = (
            record.exc_info
            if _LOGURU_FULL_TRACEBACK or record.levelno >= std_logging.ERROR
            else False
        )

        logger.opt(depth=depth, exception=exc, colors=False).bind(
            module=record.name.split(".")[0].upper()
        ).log(level, "{}", record.getMessage())


class LoggerManager:
    """带模块前缀的 logger 工厂：bind(module=...) 结果按名缓存，避免重复 bind。"""

    _cache: dict[str, loguru.Logger] = {}

    @staticmethod
    def get_logger(module_name: str):
        """返回绑定了 module 字段的 loguru logger（同名返回缓存实例）。"""
        if module_name not in LoggerManager._cache:
            LoggerManager._cache[module_name] = logger.bind(module=module_name)
        return LoggerManager._cache[module_name]


def setup_logging(
    log_level: str = "INFO",
    log_console: bool = True,
    log_file: str | Path | None = None,
    log_format: str | None = None,
    log_format_console: str | None = None,
    intercept_standard_logging: bool = True,
) -> None:
    """配置日志系统：loguru 控制台/文件双 sink，并把标准 logging 桥接进来。"""
    global _handlers

    # extra[module] 默认值：未经 bind 的 loguru 直接调用也不会缺键
    logger.configure(extra={"module": "SATORI"})

    if _handlers["console"] is not None:
        with contextlib.suppress(ValueError):
            logger.remove(_handlers["console"])
        _handlers["console"] = None
    if _handlers["file"] is not None:
        with contextlib.suppress(ValueError):
            logger.remove(_handlers["file"])
        _handlers["file"] = None

    if log_format is None:
        log_format = "[ {thread.name:^12} ] | {time:HH:mm:ss} | {extra[module]:<16} | {level:<8} | {message}"

    if log_format_console is None:
        log_format_console = (
            "<level>"
            "[ {thread.name:^12} ] | {time:HH:mm:ss} | "
            "{extra[module]:<16} | {level:<8} | {message}"
            "</level>"
        )

    if log_console:
        _handlers["console"] = logger.add(
            sys.stderr,
            format=log_format_console,
            level=log_level,
            colorize=True,
            filter=_third_party_noise_filter,
        )

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        _handlers["file"] = logger.add(
            str(log_file),
            format=log_format,
            level=log_level,
            rotation="10 MB",
            compression="zip",
            backtrace=_LOGURU_FULL_TRACEBACK,
            diagnose=_LOGURU_FULL_TRACEBACK,
            enqueue=True,
            filter=_third_party_noise_filter,
        )

    if intercept_standard_logging:
        root_logger = std_logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        root_logger.addHandler(LoguruHandler())
        root_logger.setLevel(std_logging.DEBUG)


__all__ = [
    "logger",
    "setup_logging",
    "LoguruHandler",
    "LoggerManager",
]
