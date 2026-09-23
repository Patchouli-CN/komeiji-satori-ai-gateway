"""基线判别等级与可信度评分（v4 Phase 1.3 状态机 + Phase 0.3 公式）。

三级判别（v4 Phase 1.3）：
    STRICT    基线有效，全通道开启
    STANDARD  基线老化/来源降权——声纹/答案指纹权重下调
    BASIC     无基线或已退役——仅启用不依赖参考的通道
              （身份探针、金丝雀、内置规则、usage/延迟/计费侧信道、命中率）

可信度（v4 Phase 0.3）：
    BaselineTrust(t) = W_source × W_pressure × max(0, 1 - (t-t0)/TTL)^γ
判别联动：≥0.8 STRICT | 0.4–0.8 STANDARD | 0.1–0.4 BASIC | <0.1 EXPIRED（等同 BASIC）

**来源先于时机**：W_source 来自 Phase 0 溯源 sidecar（缺失的存量参考一律按
secondhand 0.5 处理并提示补录）；W_pressure 来自采集时记录的压力等级
（Phase 0 压力感知落地前默认 MID 不削弱）；TTL 来自 Phase 2 动态引擎
（落地前默认 30 天）。

退役物理动作的现状说明：FingerprintChecker / AnswerPrintChecker 每次 check
都从磁盘现读参考文件，退役后天然走"无参考指纹"分支——不需要
BaselineExpiredError，也不需要内存卸载仪式；本模块负责归档留痕 + 等级切换。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .checkers.answerprint import answers_path
from .checkers.fingerprint import reference_path
from .config import FingerprintConfig, Upstream
from .state import StateStore

log = logging.getLogger("satori")

# Phase 0 溯源权重（sidecar 缺失的存量参考一律按 secondhand 处理）
W_SOURCE = {"official": 1.0, "secondhand": 0.5, "community": 0.3}
# Phase 0 压力权重
W_PRESSURE = {"LOW": 1.0, "MID": 1.0, "HIGH": 0.6, "EXTR": 0.0}

GAMMA = 1.5                      # 衰减指数：前期稳定，末期陡降
DEFAULT_TTL_DAYS = 30.0          # Phase 2 动态 TTL 落地前的保质期
TRUST_STRICT = 0.8
TRUST_STANDARD = 0.4
TRUST_BASIC = 0.1

# 身份类核验通道的满额记账权重（v4 Phase 5A 维度隔离的另一半）。
# 单一事实源在这——app.py 只消费，不再 duplicated
IDENTITY_CHECKER_WEIGHTS: dict[str, float] = {"fingerprint": 20.0,
                                              "answerprint": 10.0}
# STANDARD 档位的折损系数：基线老化/来源降权时，身份通道只配半额信任
STANDARD_WEIGHT_FACTOR = 0.5


class BaselineLevel(str, Enum):
    STRICT = "STRICT"
    STANDARD = "STANDARD"
    BASIC = "BASIC"


def level_for(trust: float) -> BaselineLevel:
    if trust >= TRUST_STRICT:
        return BaselineLevel.STRICT
    if trust >= TRUST_STANDARD:
        return BaselineLevel.STANDARD
    return BaselineLevel.BASIC


def trust_score(source_w: float, pressure_w: float,
                age_days: float, ttl_days: float) -> float:
    """W_source × W_pressure × max(0, 1 - age/TTL)^γ，钳制在 [0, 1]。

    age 为负（collected_at 在未来，比如时钟回拨/导入异地基线）时
    decay 会大于 1——那不是"超新鲜"，只是数据异常，按满分封顶。
    """
    if ttl_days <= 0:
        return 0.0
    decay = max(0.0, 1.0 - age_days / ttl_days)
    return min(1.0, max(0.0, source_w * pressure_w * (decay ** GAMMA)))


@dataclass
class BaselineState:
    upstream: str
    model: str
    level: BaselineLevel = BaselineLevel.BASIC
    trust: float = 0.0
    source: str = "secondhand"
    pressure: str = "MID"
    collected_at: float | None = None
    ttl_days: float = DEFAULT_TTL_DAYS
    expired: bool = False
    retired_at: float | None = None
    reference: str | None = None  # 'fingerprint' | 'answers' | None

    def to_dict(self) -> dict:
        return {
            "upstream": self.upstream, "model": self.model,
            "level": self.level.value, "trust": round(self.trust, 4),
            "source": self.source, "pressure": self.pressure,
            "collected_at": self.collected_at, "ttl_days": self.ttl_days,
            "expired": self.expired, "retired_at": self.retired_at,
            "reference": self.reference,
        }

    def identity_weight(self, checker_name: str) -> float:
        """档次动作（v4 Phase 1.3）：身份通道的记账权重随判别档次衰减。

        STRICT 全额——参考新鲜可信，对不上就是大罪；
        STANDARD 半价——基线老化/来源降权，别按满贯信任一份老参考；
        BASIC 归零——第三只眼对身份闭上，只留黑盒通道在岗。
        """
        base = IDENTITY_CHECKER_WEIGHTS.get(checker_name, 0.0)
        if self.level is BaselineLevel.STANDARD:
            return base * STANDARD_WEIGHT_FACTOR
        if self.level is BaselineLevel.BASIC:
            return 0.0
        return base


class BaselineManager:
    """每个 (upstream, model) 一本基线账：可信度、判别等级、退役归档。"""

    def __init__(self, fingerprint_cfg: FingerprintConfig,
                 upstreams: list[Upstream], archive_dir: Path,
                 state: StateStore,
                 ttl_engine=None) -> None:
        self.fingerprint_cfg = fingerprint_cfg
        self.upstreams = {u.name: u for u in upstreams}
        self.archive_dir = archive_dir
        self.state = state
        self.ttl_engine = ttl_engine  # Phase 2 动态 TTL；None 时用固定默认值
        self.states: dict[tuple[str, str], BaselineState] = {}

    # ---- 参考文件定位 ----

    def _reference_paths(self, key: tuple[str, str]) -> list[Path]:
        up = self.upstreams.get(key[0])
        if up is None:
            return []
        return [
            reference_path(self.fingerprint_cfg, up, key[1]),
            answers_path(self.fingerprint_cfg, up, key[1]),
        ]

    @staticmethod
    def _sidecar(ref: Path) -> Path:
        return ref.with_suffix(".meta.json")

    def _provenance(self, key: tuple[str, str]) -> tuple[str, str, float | None]:
        """(source, pressure_level, collected_at)。无 sidecar → secondhand + mtime 兜底。"""
        for ref in self._reference_paths(key):
            if not ref.exists():
                continue
            meta = self._sidecar(ref)
            if meta.exists():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    data = {}
                return (str(data.get("source", "secondhand")),
                        str(data.get("pressure_level", "MID")),
                        data.get("collected_at", ref.stat().st_mtime))
            return "secondhand", "MID", ref.stat().st_mtime
        return "secondhand", "MID", None

    def _archive_meta(self, key: tuple[str, str]) -> dict:
        meta = self.archive_dir / f"{key[0]}--{key[1]}" / "meta.json"
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    # ---- 评估 ----

    def recompute(self, upstream: str, model: str) -> BaselineState:
        key = (upstream, model)
        source, pressure, collected_at = self._provenance(key)
        fp_path, ans_path = self._reference_paths(key)
        refs = [p for p in (fp_path, ans_path) if p.exists()]
        ttl_days = (self.ttl_engine.ttl_for(upstream, model)[0]
                    if self.ttl_engine is not None else DEFAULT_TTL_DAYS)
        st = BaselineState(
            upstream=upstream, model=model,
            source=source, pressure=pressure, collected_at=collected_at,
            ttl_days=ttl_days,
            # 按实际存在的文件标注：answers-only 基线不是 fingerprint
            reference=("fingerprint" if fp_path.exists()
                       else "answers" if ans_path.exists() else None),
        )
        retired_at = self._archive_meta(key).get("retired_at")
        if retired_at and refs:
            # 退役后重采过（参考文件比退役标记新）→ 退役标记失效，
            # 别再挂着它让 startup_report 误报 BASIC
            newest_ref = max(p.stat().st_mtime for p in refs)
            if newest_ref > retired_at:
                retired_at = None
        if retired_at:
            st.retired_at = retired_at
        if not refs:
            # 无参考：从未采集或已退役——第三只眼闭合，BASIC
            st.level = BaselineLevel.BASIC
            st.trust = 0.0
        else:
            age_days = (time.time() - (collected_at or time.time())) / 86400
            st.trust = trust_score(W_SOURCE.get(source, 0.5),
                                   W_PRESSURE.get(pressure, 1.0),
                                   age_days, st.ttl_days)
            st.level = level_for(st.trust)
            st.expired = st.trust < TRUST_BASIC
        self.states[key] = st
        return st

    def recompute_all(self) -> None:
        for up in self.upstreams.values():
            for model in up.models:
                self.recompute(up.name, model)

    def get(self, upstream: str, model: str) -> BaselineState:
        return self.states.get((upstream, model)) or self.recompute(upstream, model)

    # ---- 退役 ----

    def retire(self, upstream: str, model: str, reason: str,
               reporter: str, last_js: float | None = None) -> BaselineState:
        """归档参考 + 留痕 + 记事件。返回退役后的状态（必然 BASIC）。"""
        key = (upstream, model)
        source, _, _ = self._provenance(key)  # 先取来源，搬家就取不到了
        arch = self.archive_dir / f"{upstream}--{model}"
        arch.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for ref in self._reference_paths(key):
            if ref.exists():
                shutil.move(str(ref), str(arch / ref.name))
                moved.append(ref.name)
                sidecar = self._sidecar(ref)
                if sidecar.exists():
                    shutil.move(str(sidecar), str(arch / sidecar.name))
        meta = {
            "retired_at": time.time(), "reason": reason, "reporter": reporter,
            "moved": moved, "last_js": last_js, "source": source,
        }
        (arch / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state.append_baseline_event({
            "ts": meta["retired_at"], "upstream": upstream, "model": model,
            "event": "retired", "reason": reason, "reporter": reporter,
            "source": source if moved else "none",
        })
        log.warning("[baseline] %s/%s 基线已退役归档（reason=%s）——BASIC 模式，"
                    "仅黑盒通道在岗", upstream, model, reason)
        st = self.recompute(upstream, model)
        st.retired_at = meta["retired_at"]
        return st

    # ---- 冷启动与反馈计量（v4 Phase 0.5） ----

    def is_cold_start(self, upstream: str, model: str) -> bool:
        """该 上游×模型 的有效基线事件 < 3 条即冷启动期。"""
        return len(self.state.read_baseline_events(
            model=model, upstream=upstream)) < 3

    def official_update_confirms(self, upstream: str, model: str,
                                 since: float | None) -> int:
        since = since or 0.0
        return sum(
            1 for r in self.state.read_feedback()
            if r.get("upstream") == upstream and r.get("model") == model
            and r.get("reason") == "official_update" and r.get("confirm")
            # 重放去重拦下的 confirm 留痕但不计数——不然刷计数照样能退役
            and r.get("action") != "confirm-deduped"
            and r.get("ts", 0) > since
        )

    def startup_report(self) -> list[str]:
        """启动自检（v4 Phase 1.4）：让系统知道自己是主动闭眼还是没睁开过。"""
        out = []
        for (up, model), st in sorted(self.states.items()):
            if st.retired_at is not None:
                out.append(f"Baseline expired for {model} "
                           f"(retired at {st.retired_at:.0f}). Running in BASIC mode.")
            elif st.reference is None:
                out.append(f"No baseline collected for {model}. "
                           f"Running in BASIC mode.")
        return out
