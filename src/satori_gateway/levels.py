"""报警等级：SAFETY / WATCH / DEGRADED。

账本分数到等级的映射：
  score < watch_threshold              → SAFETY（安全）
  watch_threshold ≤ score < 熔断阈值   → WATCH（可能降级，提高关注）
  score ≥ 熔断阈值                     → DEGRADED（质量降级，熔断）
"""

from __future__ import annotations

from enum import Enum


class AlertLevel(str, Enum):
    SAFETY = "SAFETY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"

    @property
    def rank(self) -> int:
        return {"SAFETY": 0, "WATCH": 1, "DEGRADED": 2}[self.value]


def level_of(score: int, watch_threshold: int, break_threshold: int) -> AlertLevel:
    if score >= break_threshold:
        return AlertLevel.DEGRADED
    if score >= watch_threshold:
        return AlertLevel.WATCH
    return AlertLevel.SAFETY
