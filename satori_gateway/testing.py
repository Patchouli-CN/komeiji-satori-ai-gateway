"""业务测试上报与裁决引擎（v4 Phase 5A）——质量锚点。

**两轴模型**（v4 Phase 5A 定位修正，本模块是第一轴的一半）：
    信任轴（Phase 6 TrustLevel）：谁的 PASS/FAIL 有分量——端点负责把关
    信号轴（本模块 + 账本拆分）：什么嫌疑能被 PASS 洗白——只有质量类

规则要点：
- L0 确定性断言 N=3 / L1 语义等价 N=5 / L2 复杂推理 N=7 / L3 开放式不计分
- 滑动窗口：最近 M=N×3 次里失败 ≥ N 次才触发（抗偶发 flaky）
- 触发 → 直接注入 DEGRADED 级嫌疑分，可跳过 WATCH 缓冲期；TRUSTED 凭据可立即熔断
- PASS → 衰减质量类嫌疑（L0 -10 / L1 -5 / L2 -2），受负分地板约束（端点把关）
- **身份类嫌疑（声纹 JS / 答案指纹）不可被任何测试结果洗白**——
  解冻只靠重新采集基线或 operator 显式裁决（维度隔离，账本的另一半）
- 幂等：相同 trace_id + test_name + attempt 不重复计分
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

from .state import StateStore


class TestLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


# N=触发阈值, score=注入分, decay=PASS 衰减量
LEVEL_SPEC: dict[str, dict[str, int]] = {
    "L0": {"threshold": 3, "score": 15, "decay": 10},
    "L1": {"threshold": 5, "score": 10, "decay": 5},
    "L2": {"threshold": 7, "score": 8, "decay": 2},
    "L3": {"threshold": 0, "score": 0, "decay": 0},
}
WINDOW_MULTIPLIER = 3
FLAKY_RATE = 0.05          # 历史失败率超过 5% → unreliable
FLAKY_MIN_SAMPLES = 20     # 样本不足不下 flaky 结论

_PASS_WORDS = {"pass", "passed", "success", "ok"}
_FAIL_WORDS = {"fail", "failed", "failure", "error"}


@dataclass(frozen=True)
class TestReport:
    trace_id: str
    test_suite: str
    test_name: str
    level: str            # L0/L1/L2/L3
    status: str           # 归一化后的 pass / fail
    attempt: int
    max_attempts: int
    failure_diff: str
    model_claimed: str
    upstream: str
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ts": self.ts or time.time(),
            "trace_id": self.trace_id, "test_suite": self.test_suite,
            "test_name": self.test_name, "level": self.level,
            "status": self.status, "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "failure_diff": self.failure_diff,
            "model_claimed": self.model_claimed, "upstream": self.upstream,
        }

    @staticmethod
    def from_payload(payload: dict) -> tuple["TestReport | None", str]:
        """解析 + 校验。返回 (report, 错误信息)；缺字段给中文错误文案。"""

        def need(field: str) -> str | None:
            v = payload.get(field)
            if isinstance(v, str) and v.strip():
                return v.strip()
            return None

        trace_id = need("trace_id")
        suite = need("test_suite")
        name = need("test_name")
        upstream = need("upstream")
        model = need("model_claimed")
        if not all([trace_id, suite, name, upstream, model]):
            return None, ("trace_id / test_suite / test_name / upstream / "
                          "model_claimed 均必填且非空")
        level = str(payload.get("level", "")).upper()
        if level not in LEVEL_SPEC:
            return None, f"level 必须是 {sorted(LEVEL_SPEC)} 之一"
        raw_status = str(payload.get("status", "")).lower()
        if raw_status in _PASS_WORDS:
            status = "pass"
        elif raw_status in _FAIL_WORDS:
            status = "fail"
        else:
            return None, "status 必须是 pass/fail 语义（pass/passed/ok/fail/error…）"
        try:
            attempt = int(payload.get("attempt", 1))
            max_attempts = int(payload.get("max_attempts", 1))
        except (TypeError, ValueError):
            return None, "attempt / max_attempts 必须是整数"
        if attempt < 1 or max_attempts < 1:
            return None, "attempt / max_attempts 必须 ≥ 1"
        return TestReport(
            trace_id=trace_id, test_suite=suite, test_name=name, level=level,
            status=status, attempt=attempt, max_attempts=max_attempts,
            failure_diff=str(payload.get("failure_diff", ""))[:2000],
            model_claimed=model, upstream=upstream,
            ts=float(payload.get("ts") or 0.0),
        ), ""


@dataclass
class Decision:
    accepted: bool               # False = 幂等去重
    action: str                  # recorded / deduped / pass-decay / fail-trigger / unreliable / ignored
    score_delta: float = 0.0     # >0 注入质量类嫌疑
    decay_amount: float = 0.0    # >0 衰减质量类嫌疑
    trigger_breaker: bool = False
    detail: str = ""


class TestAdjudicator:
    """滑窗 + 幂等 + flaky 标记。信任轴门控由端点按 TrustLevel 叠加。"""

    def __init__(self, state: StateStore | None = None) -> None:
        self._seen: set[tuple[str, str, int]] = set()
        self._windows: dict[tuple[str, ...], deque[bool]] = {}
        self._lifetime: dict[tuple[str, ...], list[int]] = {}  # [total, failed]
        self._unreliable: set[tuple[str, ...]] = set()
        self._state = state
        if state is not None:
            for rec in state.read_test_reports():
                report, _ = TestReport.from_payload(rec)
                if report is not None:
                    self._judge(report, replay=True)

    # ---- 内部 ----

    @staticmethod
    def _wkey(report: TestReport) -> tuple[str, ...]:
        return (report.upstream, report.model_claimed,
                report.test_suite, report.test_name)

    def _window(self, key: tuple[str, ...], level: str) -> deque[bool]:
        maxlen = LEVEL_SPEC.get(level, LEVEL_SPEC["L3"])["threshold"] \
            * WINDOW_MULTIPLIER or 1
        w = self._windows.get(key)
        if w is None or w.maxlen != maxlen:
            w = deque(maxlen=maxlen)
            self._windows[key] = w
        return w

    def _persist(self, report: TestReport) -> None:
        if self._state is not None:
            self._state.append_test_report(report.to_dict())

    def _judge(self, report: TestReport, replay: bool = False) -> Decision:
        key = self._wkey(report)
        spec = LEVEL_SPEC.get(report.level, LEVEL_SPEC["L3"])
        passed = report.status == "pass"

        idem = (report.trace_id, report.test_name, report.attempt)
        if not replay:
            if idem in self._seen:
                return Decision(False, "deduped",
                                detail="相同 trace_id + test_name + attempt 不重复计分")
        # replay 时也要重建幂等集——否则重启后旧报告会被当新的再裁决一遍
        self._seen.add(idem)

        window = self._window(key, report.level)
        window.append(passed)
        life = self._lifetime.setdefault(key, [0, 0])
        life[0] += 1
        if not passed:
            life[1] += 1

        def finish(decision: Decision) -> Decision:
            if not replay:
                self._persist(report)
            return decision

        # flaky：历史失败率 > 5% 且样本充足 → unreliable，此后仅记录不裁决。
        # 顺序要紧：已标记的先进"仅记录"分支，否则每次 deciding 都会重新命中
        # flaky 条件（生命周期还在涨），永远出不了 unreliable 状态
        if key in self._unreliable:
            return finish(Decision(True, "recorded",
                                   detail="unreliable 测试，仅记录不裁决"))
        if life[0] >= FLAKY_MIN_SAMPLES and life[1] / life[0] > FLAKY_RATE:
            self._unreliable.add(key)
            return finish(Decision(
                True, "unreliable",
                detail=f"历史失败率 {life[1] / life[0]:.1%} > {FLAKY_RATE:.0%}，"
                       f"标记 unreliable（不计分）"))

        if passed:
            if spec["decay"] > 0:
                return finish(Decision(
                    True, "pass-decay", decay_amount=float(spec["decay"]),
                    detail=f"PASS 衰减质量类嫌疑 -{spec['decay']}（受负分地板约束）"))
            return finish(Decision(True, "recorded", detail="PASS 记录"))

        # fail 分支
        if spec["threshold"] == 0:
            return finish(Decision(True, "ignored",
                                   detail="L3 开放式测试不计分"))
        fails = sum(1 for ok in window if not ok)
        if fails >= spec["threshold"]:
            window.clear()  # 触发后重新武装，避免刷屏
            return finish(Decision(
                True, "fail-trigger", score_delta=float(spec["score"]),
                trigger_breaker=True,
                detail=f"滑窗失败 {fails}/{window.maxlen} ≥ N={spec['threshold']}"
                       f" → 注入 DEGRADED 级嫌疑 +{spec['score']}"))
        return finish(Decision(
            True, "recorded",
            detail=f"滑窗失败 {fails}/{window.maxlen}，未达 N={spec['threshold']}"))

    # ---- 对外 ----

    def decide(self, report: TestReport) -> Decision:
        return self._judge(report, replay=False)

    def summary(self) -> list[dict]:
        """Dashboard 测试溯源面板的数据源（按 upstream × model × suite 聚合）。"""
        out = []
        for key, (total, failed) in sorted(self._lifetime.items()):
            upstream, model, suite, name = key
            out.append({
                "upstream": upstream, "model": model,
                "test_suite": suite, "test_name": name,
                "total": total, "failed": failed,
                "unreliable": key in self._unreliable,
            })
        return out
