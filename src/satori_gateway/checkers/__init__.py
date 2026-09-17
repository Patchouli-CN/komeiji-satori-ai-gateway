"""Checker 基类：所有“觉之瞳”检测手段的公共接口。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..config import Upstream


@dataclass(frozen=True)
class CheckResult:
    checker: str
    upstream: str
    model: str
    ok: bool
    score: float  # 语义由 checker 自定：金丝雀是通过率，指纹是 JS 散度
    detail: str
    checked_at: float = field(default_factory=time.time)


class Checker(Protocol):
    name: str

    async def check(
        self, client: httpx.AsyncClient, upstream: Upstream, model: str
    ) -> CheckResult: ...
