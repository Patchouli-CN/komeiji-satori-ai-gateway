"""usage 分词侧信道：chars/token 比例漂移检测。

原理：同一个模型+分词器，对同类文本的 字符数/prompt_tokens 比例是稳定的；
模型被偷偷路由（分词器换人）会引起比例的系统性偏移。
完全自基线——不需要官方参考，厂商不配合也能用，只需它没伪造 usage 字段。
"""

from __future__ import annotations

from .watch import DriftWatch


class TokenizerWatch:
    def __init__(
        self,
        min_samples: int = 20,
        tolerance: float = 0.25,
        breach_limit: int = 3,
        alpha: float = 0.1,
    ) -> None:
        self._drift = DriftWatch(
            "chars/token 比例",
            "——疑似分词器换人",
            min_samples=min_samples,
            tolerance=tolerance,
            breach_limit=breach_limit,
            alpha=alpha,
        )

    def observe(
        self, upstream: str, model: str, request_chars: int, prompt_tokens: int
    ) -> str | None:
        """观察一条请求的 usage。返回 None 无事，返回 str 为告警描述。"""
        if request_chars <= 0 or prompt_tokens <= 0:
            return None
        return self._drift.observe((upstream, model), request_chars / prompt_tokens)
