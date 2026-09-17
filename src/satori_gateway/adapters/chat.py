"""OpenAI Chat Completions —— 内部规范本身，透传快车道。"""

from __future__ import annotations

from . import register_adapter


@register_adapter
class ChatAdapter:
    name = "openai-chat"
    path = "/v1/chat/completions"
    passthrough = True

    def to_canonical(self, body: dict) -> dict:
        return body

    def from_canonical(self, payload: dict) -> dict:
        return payload

    def translate_sse(self, chunk: dict, state: dict) -> list[dict]:
        return []

    def finish_sse(self, state: dict) -> list[dict]:
        return []
