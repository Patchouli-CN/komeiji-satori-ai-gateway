"""指纹核验：首 token top-logprobs 分布 vs 可信参考，JS 散度判定身份。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import httpx

from ..config import FingerprintConfig, Upstream
from ..registry import register_checker
from . import CheckResult

_FLOOR = 1e-9  # 对齐词表时缺失 token 的兜底概率


def _normalize(dist: dict[str, float]) -> dict[str, float]:
    total = sum(dist.values())
    return {t: p / total for t, p in dist.items()} if total > 0 else dist


def js_divergence(p: dict[str, float], q: dict[str, float]) -> float:
    """两个离散分布的 Jensen-Shannon 散度（0~1，越大越不像）。"""
    p, q = _normalize(p), _normalize(q)
    keys = set(p) | set(q)
    m = {k: (p.get(k, _FLOOR) + q.get(k, _FLOOR)) / 2 for k in keys}

    def kl(a: dict[str, float], b: dict[str, float]) -> float:
        return sum(
            a.get(k, _FLOOR) * math.log(a.get(k, _FLOOR) / b[k]) for k in keys
        )

    return (kl(p, m) + kl(q, m)) / 2 / math.log(2)


async def probe(
    client: httpx.AsyncClient,
    upstream: Upstream,
    model: str,
    cfg: FingerprintConfig,
    prompt: str | None = None,
) -> dict[str, float]:
    """向上游请求探针 prompt，返回 {token: prob} 分布。"""
    resp = await client.post(
        f"{upstream.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {upstream.resolve_key()}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt or cfg.probe_prompt}],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": cfg.top_logprobs,
        },
        timeout=30,
    )
    resp.raise_for_status()
    top = resp.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    return {item["token"]: math.exp(item["logprob"]) for item in top}


def reference_path(
    cfg: FingerprintConfig, upstream: Upstream, model: str, prompt: str | None = None
) -> Path:
    """参考指纹文件路径。非默认探针 prompt 的指纹带哈希后缀区分。"""
    safe = f"{upstream.name}--{model}".replace("/", "_")
    if prompt is not None and prompt != cfg.probe_prompt:
        safe += "--" + hashlib.sha1(prompt.encode()).hexdigest()[:8]
    return cfg.reference_dir / f"{safe}.json"


@register_checker
class FingerprintChecker:
    name = "fingerprint"

    def __init__(self, cfg: FingerprintConfig) -> None:
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg) -> "FingerprintChecker | None":
        return cls(cfg.fingerprint)

    async def check(
        self, client: httpx.AsyncClient, upstream: Upstream, model: str
    ) -> CheckResult:
        ref_file = reference_path(self.cfg, upstream, model)
        if not ref_file.exists():
            return CheckResult(
                self.name, upstream.name, model, False, 0.0,
                f"无参考指纹 {ref_file}，先用 collect 命令从可信端点采集",
            )
        reference = json.loads(ref_file.read_text(encoding="utf-8"))
        current = await probe(client, upstream, model, self.cfg)
        js = js_divergence(reference, current)
        ok = js <= self.cfg.js_threshold
        detail = f"JS 散度 {js:.4f}（阈值 {self.cfg.js_threshold}）"
        if not ok:
            detail += " —— 覚「心の中の弱者」：端上来的不是当初那个模型"
        return CheckResult(self.name, upstream.name, model, ok, js, detail)
