"""ProviderPressure 供应商压力感知（v4 Phase 0.2）。

**来源先于时机，压力是二阶修正**：采集时机只影响 W_pressure 权重
（LOW/MID 1.0 / HIGH 0.6 / EXTR 0.0），不再是强制闸门——操作员可以用
--force-pressure 覆盖，但覆盖事实记进 sidecar，信任度自己负责。

压力等级来自厂商本地时区的流量模式（通用默认表按本地小时划分：
夜间 LOW、工作日高峰 HIGH）。实时信号叠加（P99 延迟超基线 3 倍或
错误率 > 5% → 强制 EXTR）需要网关在程内的延迟画像，留在 Wave 3
与 LatencyWatch 联动——本模块预留 promote() 钩子。
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

# 厂商 → 本地时区（流量高峰按厂商的白天算，不按你的）
VENDOR_TZ: dict[str, str] = {
    "openai": "America/Los_Angeles",
    "anthropic": "America/Los_Angeles",
    "deepseek": "Asia/Shanghai",
    "kimi": "Asia/Shanghai",
    "moonshot": "Asia/Shanghai",
    "zhipu": "Asia/Shanghai",
    "qwen": "Asia/Shanghai",
    "dashscope": "Asia/Shanghai",
}
DEFAULT_TZ = "UTC"

# 通用流量模式：本地小时 → 压力等级。夜间黄金窗口，工作日高峰可疑
_HOUR_PATTERN: dict[int, str] = {
    **{h: "LOW" for h in range(0, 7)},  # 00-06 黄金窗口
    **{h: "MID" for h in (7, 8, 19, 20, 21)},  # 早晚过渡带
    **{h: "HIGH" for h in (9, 10, 11, 15, 16, 17, 18)},  # 工作高峰
    12: "MID",
    13: "MID",
    14: "MID",  # 午休回落
    22: "LOW",
    23: "LOW",
}

# 压力 → 信任权重（v4 Phase 0.2 基线质量权重映射）
PRESSURE_WEIGHT: dict[str, float] = {
    "LOW": 1.0,  # 不削弱
    "MID": 1.0,  # 可接受
    "HIGH": 0.6,  # 显著削弱，自动降级为 STANDARD 起点
    "EXTR": 0.0,  # 完全弃用，拒绝作为 STRICT 基线
}

_GOLDEN = ("LOW", "MID")


class ProviderPressure:
    """按厂商本地时间报压力等级。纯函数，不打网络、不看进程状态。"""

    def __init__(
        self, vendor: str, tz: str | None = None, pattern: dict[int, str] | None = None
    ) -> None:
        self.vendor = vendor
        self.tz_name = tz or VENDOR_TZ.get(vendor.lower(), DEFAULT_TZ)
        self.pattern = pattern or _HOUR_PATTERN

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except Exception:
            return ZoneInfo(DEFAULT_TZ)

    def level_at(self, now: float | None = None) -> str:
        """now 为 unix 时间戳；缺省取当前时刻。"""
        ts = now if now is not None else time.time()
        local_hour = datetime.fromtimestamp(ts, self._zone()).hour
        return self.pattern.get(local_hour, "MID")

    def weight_at(self, now: float | None = None, level: str | None = None) -> float:
        lv = level or self.level_at(now)
        return PRESSURE_WEIGHT.get(lv, 1.0)

    def next_golden_window(self, now: float | None = None) -> float:
        """下一个 LOW/MID 窗口开始的 unix 时间戳（--wait-for-low 用）。"""
        ts = now if now is not None else time.time()
        zone = self._zone()
        for step in range(0, 48 * 60, 15):  # 15 分钟粒度，最多看两天
            candidate = ts + step * 60
            hour = datetime.fromtimestamp(candidate, zone).hour
            if self.pattern.get(hour, "MID") in _GOLDEN:
                return candidate
        return ts  # 模式全 HIGH 的病态配置：不赌，立刻返回

    def describe(self, now: float | None = None) -> str:
        lv = self.level_at(now)
        zone = self._zone()
        local = datetime.fromtimestamp(now or time.time(), zone)
        return (
            f"Current pressure: {lv} (vendor {self.vendor} local "
            f"{local:%H:%M} {self.tz_name}). Weight: "
            f"{self.weight_at(level=lv):.1f}."
        )
