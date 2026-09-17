"""usage 分词侧信道：chars/token 比例漂移检测。

原理：同一个模型+分词器，对同类文本的 字符数/prompt_tokens 比例是稳定的；
模型被偷偷路由（分词器换人）会引起比例的系统性偏移。
完全自基线——不需要官方参考，厂商不配合也能用，只需它没伪造 usage 字段。

策略保守：EMA 基线 + 最小样本数 + 连续越限才告警，宁可慢不误报。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _State:
    ema: float = 0.0
    samples: int = 0
    breaches: int = 0


class TokenizerWatch:
    def __init__(
        self,
        min_samples: int = 20,
        tolerance: float = 0.25,
        breach_limit: int = 3,
        alpha: float = 0.1,
    ) -> None:
        self.min_samples = min_samples
        self.tolerance = tolerance
        self.breach_limit = breach_limit
        self.alpha = alpha
        self._states: dict[tuple[str, str], _State] = {}

    def observe(
        self, upstream: str, model: str, request_chars: int, prompt_tokens: int
    ) -> str | None:
        """观察一条请求的 usage。返回 None 无事，返回 str 为告警描述。"""
        if request_chars <= 0 or prompt_tokens <= 0:
            return None
        ratio = request_chars / prompt_tokens
        st = self._states.setdefault((upstream, model), _State())

        alert = None
        if st.samples >= self.min_samples and st.ema > 0:
            dev = abs(ratio - st.ema) / st.ema
            if dev > self.tolerance:
                st.breaches += 1
                if st.breaches >= self.breach_limit:
                    alert = (
                        f"chars/token 比例持续偏移: {ratio:.2f} vs 基线 {st.ema:.2f}"
                        f"（偏差 {dev:.0%}，连续 {st.breaches} 次）——疑似分词器换人"
                    )
                    st.breaches = 0  # 告警后重新武装，避免刷屏
            else:
                st.breaches = 0
                # 只在正常样本上更新基线，防止被污染
                st.ema = (1 - self.alpha) * st.ema + self.alpha * ratio
        else:
            st.breaches = 0
            st.ema = ratio if st.samples == 0 else (1 - self.alpha) * st.ema + self.alpha * ratio

        st.samples += 1
        return alert
