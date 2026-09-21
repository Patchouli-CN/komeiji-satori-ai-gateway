"""金丝雀：固定考题周期性抽查，把“感觉变笨了”量化成通过率。"""

from __future__ import annotations

import httpx

from ..config import CanaryCase, Upstream
from ..pipelines import chat_once
from ..registry import register_checker
from . import CheckResult


@register_checker
class CanaryChecker:
    name = "canary"

    def __init__(self, cases: list[CanaryCase]) -> None:
        self.cases = cases

    @classmethod
    def from_config(cls, cfg) -> "CanaryChecker | None":
        return cls(cfg.canary) if cfg.canary else None

    async def check(
        self, client: httpx.AsyncClient, upstream: Upstream, model: str
    ) -> CheckResult:
        if not self.cases:
            return CheckResult(self.name, upstream.name, model, True, 1.0, "无考题")

        passed, failures = 0, []
        for case in self.cases:
            data = await chat_once(client, upstream, {
                "model": model,
                "messages": [{"role": "user", "content": case.prompt}],
                "max_tokens": 64,
                "temperature": 0,
            }, timeout=60)
            content = data["choices"][0]["message"]["content"] or ""
            if case.expect.lower() in content.lower():
                passed += 1
            else:
                failures.append(f"{case.prompt!r} 期望含 {case.expect!r}，实际 {content[:80]!r}")

        score = passed / len(self.cases)
        detail = f"{passed}/{len(self.cases)} 通过"
        if failures:
            detail += "；" + "；".join(failures)
        return CheckResult(self.name, upstream.name, model, score == 1.0, score, detail)
