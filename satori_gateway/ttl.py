"""动态 TTL 引擎（v4 Phase 2 最小可用版）——基线保质期的数据驱动预测。

两个概念必须拆开（v4 定位修正）：

- **变质检测**（模型是否被偷偷换掉）是账本 + 各检测通道的**日常工作**，
  半衰期衰减 + 命中率通道已经在管；
- **基线相关性**（参考文件是否还对得上当前官方模型）才是 TTL 的地盘——
  它是"要不要重新采集"的提示器，**不是变质探测器**。

学习数据源：基线事件流水（`state/baseline_events.jsonl` 的
retired/refreshed/confirmed）。不空等"刷新历史"——按官方数月一次的迭代节奏，
10 条记录要几年才攒得够；**用户每次确认 official_update 就是一次实锤的
过期事件**，这才是 TTL 该学习的地面真值（v4 修正）。

**预测不误杀**：TTL 到期只发预警并点灯（70% STANDARD / 90% 橙 / >100% 红），
**不自动退役基线**——退役与否必须经过 Phase 1 反馈通道的地面真值。
自动退役 = 用预测杀人：误报形态从"狼来了"变成"真狼来了却闭嘴"。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .state import StateStore

log = logging.getLogger("satori")

MIN_TTL_DAYS = 7.0
MAX_TTL_DAYS = 180.0
COLD_START_TTL_DAYS = 30.0     # 事件 < 3 条：固定保守值，不学
SAFETY_3_EVENTS = 0.7          # 3–4 条事件：首次启用中位数，安全系数 0.7
SAFETY_LEARNED = 0.9           # ≥ 5 条事件：安全系数恢复 0.9
LEARNING_MIN_EVENTS = 5

# 老化预警阈值（TTL 消耗比例）
AGING_AT = 0.7                 # → STANDARD / 黄灯
CRITICAL_AT = 0.9              # → 推送告警 / 橙灯
EXPIRED_AT = 1.0               # → 红灯 + 建议重采（不退役）


def median(values: list[float]) -> float:
    """中位数：抵抗偶发噪声，捕捉真实周期（v4：中位数优于平均值）。"""
    if not values:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def reject_outliers(values: list[float], sigmas: float = 3.0) -> list[float]:
    """剔除超出历史范围 3σ 的间隔。样本 < 2 时原样返回（无从估计离散度）。"""
    if len(values) < 2:
        return list(values)
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = var ** 0.5
    if std <= 0:
        return list(values)
    return [v for v in values if abs(v - mean) <= sigmas * std]


@dataclass(frozen=True)
class TtlVerdict:
    upstream: str
    model: str
    ttl_days: float
    age_days: float
    consumed: float            # age/ttl，可 > 1
    state: str                 # fresh / aging / critical / expired
    source: str                # cold-start / median / override
    events: int

    def to_dict(self) -> dict:
        return {
            "upstream": self.upstream, "model": self.model,
            "ttl_days": round(self.ttl_days, 1),
            "age_days": round(self.age_days, 1),
            "consumed": round(self.consumed, 3),
            "state": self.state, "source": self.source, "events": self.events,
        }


def _okey(upstream: str, model: str) -> str:
    """override 持久化键。上游名不含 |（与 state.py 账本键同一约定）。"""
    return f"{upstream}|{model}"


class TtlEngine:
    """每个 上游×模型 一个保质期。override 可临时锁定（操作员工具，落盘持久）。

    键是 (upstream, model) 不是裸 model：官方与中转跑同名模型时，
    事件/锁定/预警各走各的账，互不串味。
    """

    def __init__(self, state: StateStore | None = None,
                 overrides_file: Path | None = None) -> None:
        self.state = state
        self.overrides_file = overrides_file or (
            state.directory / "ttl_overrides.json" if state else None)
        self._overrides: dict[str, dict] = {}
        self._emitted: dict[tuple[str, str], str] = {}  # (up, model) -> 已点灯状态
        self._load_overrides()

    # ---- override 持久化 ----

    def _load_overrides(self) -> None:
        if self.overrides_file is None or not self.overrides_file.exists():
            return
        try:
            self._overrides = json.loads(
                self.overrides_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._overrides = {}

    def _save_overrides(self) -> None:
        if self.overrides_file is None:
            return
        self.overrides_file.parent.mkdir(parents=True, exist_ok=True)
        self.overrides_file.write_text(
            json.dumps(self._overrides, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def override(self, upstream: str, model: str,
                 ttl_days: float | None = None,
                 expires_at: float | None = None, reason: str = "") -> dict:
        """锁定 TTL。ttl_days 固定天数；expires_at 绝对时间点（先到先得）。"""
        record = {"upstream": upstream, "model": model, "reason": reason,
                  "set_at": time.time(), "ttl_days": ttl_days,
                  "expires_at": expires_at}
        self._overrides[_okey(upstream, model)] = record
        self._save_overrides()
        log.info("[ttl] %s/%s 手动锁定保质期：ttl_days=%s expires_at=%s（%s）",
                 upstream, model, ttl_days, expires_at, reason or "未注明")
        return record

    def clear_override(self, upstream: str, model: str) -> bool:
        key = _okey(upstream, model)
        if key in self._overrides or model in self._overrides:
            self._overrides.pop(key, None)
            self._overrides.pop(model, None)  # 旧格式（裸 model 键）一并清
            self._save_overrides()
            return True
        return False

    def override_of(self, upstream: str, model: str) -> dict | None:
        """当前生效的手动锁定记录（status 透出用）。

        旧格式文件里的裸 model 键按通配匹配（升级兼容：当时的锁定语义
        就是"这个模型"，读出来后首次覆盖写会迁移成复合键）。
        """
        return self._overrides.get(_okey(upstream, model),
                                   self._overrides.get(model))

    def _override_ttl(self, upstream: str, model: str,
                      now: float) -> float | None:
        rec = self.override_of(upstream, model)
        if not rec:
            return None
        if rec.get("expires_at") and now > rec["expires_at"]:
            self.clear_override(upstream, model)  # 过期自动解锁，回归学习值
            return None
        return float(rec["ttl_days"]) if rec.get("ttl_days") else None

    # ---- 学习 ----

    def intervals(self, upstream: str, model: str) -> list[float]:
        """相邻基线事件的天数间隔。事件太少学不到东西——这是常态，不硬学。"""
        if self.state is None:
            return []
        events = self.state.read_baseline_events(model=model, upstream=upstream)
        if len(events) < 2:
            return []
        ts = sorted(e.get("ts", 0.0) for e in events)
        return [(b - a) / 86400 for a, b in zip(ts, ts[1:]) if b > a]

    def ttl_for(self, upstream: str, model: str,
                now: float | None = None) -> tuple[float, str, int]:
        """(ttl_days, source, events)。override > 学习中位数 > 冷启动固定值。"""
        now = now if now is not None else time.time()
        override = self._override_ttl(upstream, model, now)
        if override is not None:
            return override, "override", len(self.intervals(upstream, model))
        raw = self.intervals(upstream, model)
        n = len(raw)
        if n < 3:
            return COLD_START_TTL_DAYS, "cold-start", n
        factor = SAFETY_3_EVENTS if n < LEARNING_MIN_EVENTS else SAFETY_LEARNED
        med = median(reject_outliers(raw))
        if med <= 0:
            return COLD_START_TTL_DAYS, "cold-start", n
        ttl = min(max(med * factor, MIN_TTL_DAYS), MAX_TTL_DAYS)
        return ttl, "median", n

    # ---- 判定与预警 ----

    @staticmethod
    def _state_of(consumed: float) -> str:
        if consumed >= EXPIRED_AT:
            return "expired"
        if consumed >= CRITICAL_AT:
            return "critical"
        if consumed >= AGING_AT:
            return "aging"
        return "fresh"

    def verdict(self, upstream: str, model: str, collected_at: float | None,
                now: float | None = None) -> TtlVerdict | None:
        """无采集时间的基线（无参考/存量无 sidecar）不判定——它本来就 BASIC。"""
        if not collected_at:
            return None
        now = now if now is not None else time.time()
        ttl_days, source, events = self.ttl_for(upstream, model, now)
        age_days = max(0.0, (now - collected_at) / 86400)
        consumed = age_days / ttl_days if ttl_days > 0 else 0.0
        return TtlVerdict(upstream, model, ttl_days, age_days, consumed,
                          self._state_of(consumed), source, events)

    def due_warnings(self, verdict: TtlVerdict) -> str | None:
        """状态翻转才点灯（去重）。返回该点的灯，None 表示无需广播。"""
        key = (verdict.upstream, verdict.model)
        prev = self._emitted.get(key)
        if verdict.state == prev:
            return None
        self._emitted[key] = verdict.state
        if verdict.state == "aging":
            return (f"TTL 消耗 {verdict.consumed:.0%}——基线步入老化，"
                    f"建议近期重新 collect（还剩 {verdict.ttl_days - verdict.age_days:.0f} 天）")
        if verdict.state == "critical":
            return (f"TTL 消耗 {verdict.consumed:.0%}——保质期所剩无几，"
                    f"请尽快重新采集或反馈 official_update")
        if verdict.state == "expired":
            return (f"TTL 已超限 {verdict.consumed:.0%}——基线可能过期。"
                    f"请 satori collect 重采，或反馈 official_update 确认退役"
                    f"（系统不会自动退役：预测不误杀）")
        return None
