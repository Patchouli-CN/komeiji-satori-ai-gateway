"""答案指纹：蒸馏偷得了人设，偷不了作答习惯。

灵感来自 LLMmap（主动指纹 + 相似度比对）与 llm-verify 的基线对比。
固定题组、temp=0 下，同一模型的作答应高度稳定；与官方参考作答的
相似度掉线，即换模型或降智。

纯黑盒通道：不需要 logprobs、不需要 usage，只要模型还在产出文本——
Anthropic 系端点（无 logprob 可采）的主力指纹手段。
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from ..config import AnswerPrintConfig, FingerprintConfig, Upstream
from ..pipelines import chat_once
from ..registry import register_checker
from . import CheckResult

# 作答稳定型题组：事实、格式、短推理，避免开放题（开放题同一模型也会漂移）
PROBE_SET: list[str] = [
    "What is the capital of Australia? One word only.",
    "用一句话解释什么是递归。",
    "Write a Python one-liner that reverses a string. Code only.",
    "What is 17 * 23? Just the number.",
    "List the first 5 prime numbers, comma separated.",
    "Translate 'good morning' into French, German and Japanese.",
    "What year did Apollo 11 land on the Moon? Just the year.",
    "Write a haiku about autumn. Exactly three lines.",
]


def answers_path(cfg: FingerprintConfig, upstream: Upstream, model: str) -> Path:
    safe = f"{upstream.name}--{model}".replace("/", "_")
    return cfg.reference_dir / f"{safe}--answers.json"


async def ask(
    client: httpx.AsyncClient, upstream: Upstream, model: str,
    prompt: str, max_tokens: int,
) -> str:
    data = await chat_once(client, upstream, {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }, timeout=60)
    return data["choices"][0]["message"].get("content") or ""


async def collect_answers(
    client: httpx.AsyncClient, upstream: Upstream, model: str, max_tokens: int
) -> dict[str, str]:
    """从可信端点采集参考作答（供 satori answers 命令使用）。"""
    out: dict[str, str] = {}
    for prompt in PROBE_SET:
        out[prompt] = await ask(client, upstream, model, prompt, max_tokens)
    return out


@register_checker
class AnswerFingerprintChecker:
    name = "answerprint"

    def __init__(self, cfg: AnswerPrintConfig, fp_cfg: FingerprintConfig) -> None:
        self.cfg = cfg
        self.fp_cfg = fp_cfg

    @classmethod
    def from_config(cls, cfg) -> "AnswerFingerprintChecker | None":
        return cls(cfg.answerprint, cfg.fingerprint) if cfg.answerprint.enabled else None

    async def check(
        self, client: httpx.AsyncClient, upstream: Upstream, model: str
    ) -> CheckResult:
        ref_file = answers_path(self.fp_cfg, upstream, model)
        if not ref_file.exists():
            return CheckResult(
                self.name, upstream.name, model, False, 0.0,
                f"无参考作答 {ref_file}，先用 satori answers 从可信端点采集",
            )
        reference = json.loads(ref_file.read_text(encoding="utf-8"))

        sims: list[float] = []
        worst = (1.0, "")
        for prompt, ref_answer in reference.items():
            current = await ask(client, upstream, model, prompt, self.cfg.max_tokens)
            sim = SequenceMatcher(None, ref_answer, current).ratio()
            sims.append(sim)
            if sim < worst[0]:
                worst = (sim, prompt)

        avg = sum(sims) / len(sims) if sims else 0.0
        ok = avg >= self.cfg.similarity_threshold
        detail = (
            f"平均相似度 {avg:.2f}（最差 {worst[0]:.2f} @ {worst[1][:30]!r}，"
            f"阈值 {self.cfg.similarity_threshold}）"
        )
        if not ok:
            detail += " —— 作答习惯对不上当初那个模型"
        # score：1 - 平均相似度，供事件流排序
        return CheckResult(self.name, upstream.name, model, ok, 1 - avg, detail)
