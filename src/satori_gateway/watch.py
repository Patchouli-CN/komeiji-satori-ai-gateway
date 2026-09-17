"""通用漂移检测：EMA 自基线 + 最小样本数 + 连续越限才告警。

分词比例、首字节延迟、计费一致性共用一个套路，只是度量和阈值不同。
策略一律保守：宁可慢不误报。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _State:
    ema: float = 0.0
    samples: int = 0
    breaches: int = 0


class DriftWatch:
    def __init__(
        self,
        label: str,
        verdict: str,
        min_samples: int = 20,
        tolerance: float = 0.25,
        breach_limit: int = 3,
        alpha: float = 0.1,
    ) -> None:
        self.label = label
        self.verdict = verdict
        self.min_samples = min_samples
        self.tolerance = tolerance
        self.breach_limit = breach_limit
        self.alpha = alpha
        self._states: dict[tuple[str, str], _State] = {}

    def observe(self, key: tuple[str, str], value: float) -> str | None:
        if value <= 0:
            return None
        st = self._states.setdefault(key, _State())

        alert = None
        if st.samples >= self.min_samples and st.ema > 0:
            dev = abs(value - st.ema) / st.ema
            if dev > self.tolerance:
                st.breaches += 1
                if st.breaches >= self.breach_limit:
                    alert = (
                        f"{self.label} 持续偏移: {value:.2f} vs 基线 {st.ema:.2f}"
                        f"（偏差 {dev:.0%}，连续 {st.breaches} 次）{self.verdict}"
                    )
                    st.breaches = 0  # 告警后重新武装，避免刷屏
            else:
                st.breaches = 0
                # 只在正常样本上更新基线，防止被污染
                st.ema = (1 - self.alpha) * st.ema + self.alpha * value
        else:
            st.breaches = 0
            st.ema = value if st.samples == 0 else (1 - self.alpha) * st.ema + self.alpha * value

        st.samples += 1
        return alert


class LatencyWatch:
    """首字节延迟画像：基础设施签名，换链路/换模型会漂移。网络噪声大，阈值放宽。"""

    def __init__(self) -> None:
        self._drift = DriftWatch(
            "首字节延迟", "——疑似上游链路或模型更换",
            tolerance=0.5, breach_limit=5,
        )

    def observe(self, upstream: str, model: str, first_byte_ms: float) -> str | None:
        return self._drift.observe((upstream, model), first_byte_ms)


class BillingWatch:
    """计费一致性（GatewayBench L1 思路）：completion_tokens 与实收文本的比例。

    token 虚报（账单注水）或模型更换都会让比例漂移。
    """

    def __init__(self) -> None:
        self._drift = DriftWatch(
            "chars/completion_token 比例", "——计费 token 虚报或模型更换",
            tolerance=0.3,
        )

    def observe(
        self, upstream: str, model: str, content_chars: int, completion_tokens: int
    ) -> str | None:
        if content_chars <= 0 or completion_tokens <= 0:
            return None
        return self._drift.observe((upstream, model), content_chars / completion_tokens)


class HitRateWatch:
    """规则命中率通道：抗掺水专用。

    统计严重规则（score≥25：厂商自报/伪装泄漏/代理自曝）的命中率。
    孤立误报被 EMA 稀释，持续掺水（比如 90% 真 10% 假）会让命中率
    稳定越过阈值——掺多少抓多少。
    """

    def __init__(
        self,
        threshold: float = 0.05,
        min_samples: int = 50,
        alpha: float = 0.05,
    ) -> None:
        self.threshold = threshold
        self.min_samples = min_samples
        self.alpha = alpha
        self._states: dict[tuple[str, str], list[float]] = {}  # key -> [ema, samples]

    def observe(self, upstream: str, model: str, serious_hit: bool) -> str | None:
        key = (upstream, model)
        ema, samples = self._states.get(key, [0.0, 0])
        samples += 1
        ema = (1 - self.alpha) * ema + self.alpha * (1.0 if serious_hit else 0.0)
        self._states[key] = [ema, samples]

        if samples >= self.min_samples and ema > self.threshold:
            # 告警后重置，重新武装
            self._states[key] = [0.0, 0]
            return (
                f"严重规则命中率 {ema:.1%} 超阈值 {self.threshold:.1%}"
                f"（{samples} 个样本）——疑似掺水流量"
            )
        return None
