"""OpenAI Chat Completions 上游插件 —— 规范本身，恒等转换器。"""

from __future__ import annotations

from ..config import Upstream
from . import TransferPipeline


@TransferPipeline.register("upstream", "openai")
class OpenAIUpstream:
    """传输层：{base_url}/chat/completions + Bearer 鉴权。"""

    format = "openai"

    def __init__(self, cfg: Upstream) -> None:
        self.cfg = cfg

    def endpoint(self) -> tuple[str, dict]:
        return (
            f"{self.cfg.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.cfg.resolve_key()}",
                "Content-Type": "application/json",
            },
        )


@TransferPipeline.register("convert", "openai")
class OpenAIConverter:
    """格式层：规范即原生，全部恒等。"""

    passthrough = True
    capabilities = {"logprobs"}

    def request_to_native(self, canonical: dict) -> dict:
        return canonical

    def response_to_canonical(self, native: dict) -> dict:
        return native

    def new_stream_state(self) -> dict:
        return {}

    def translate_sse_data(self, event: dict, state: dict) -> list[dict]:
        return [event]
