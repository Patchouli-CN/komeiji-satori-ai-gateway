"""Anthropic Messages API ↔ 规范（Chat Completions）。

v1 范围：文本与图片 block（base64 → data URL）、system、stop_sequences、
流式事件翻译。tools / thinking block 暂未翻译——检测侧不受影响
（规则照常审文本），但依赖工具调用的客户端请走 v1 之后的版本。
"""

from __future__ import annotations

import uuid

from . import register_adapter

_FINISH_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


def _blocks_to_openai(content):
    """Anthropic content（str 或 block 列表）→ OpenAI content。"""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts or None


@register_adapter
class AnthropicAdapter:
    name = "anthropic-messages"
    path = "/v1/messages"
    passthrough = False

    # ---- 请求 ----

    def to_canonical(self, body: dict) -> dict:
        messages: list[dict] = []
        system = body.get("system")
        if system:
            if isinstance(system, list):
                system = "".join(
                    b.get("text", "") for b in system if b.get("type") == "text"
                )
            messages.append({"role": "system", "content": system})
        for msg in body.get("messages", []):
            content = _blocks_to_openai(msg.get("content"))
            if content is not None:
                messages.append({"role": msg.get("role", "user"), "content": content})

        canonical: dict = {"model": body.get("model", ""), "messages": messages}
        for key in ("max_tokens", "temperature", "top_p", "stream"):
            if key in body:
                canonical[key] = body[key]
        if "stop_sequences" in body:
            canonical["stop"] = body["stop_sequences"]
        return canonical

    # ---- 非流式响应 ----

    def from_canonical(self, payload: dict) -> dict:
        choice = (payload.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = payload.get("usage") or {}
        return {
            "id": payload.get("id", "msg_satori"),
            "type": "message",
            "role": "assistant",
            "model": payload.get("model", ""),
            "content": [{"type": "text", "text": msg.get("content") or ""}],
            "stop_reason": _FINISH_MAP.get(choice.get("finish_reason") or "stop", "end_turn"),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    # ---- 流式响应 ----

    def translate_sse(self, chunk: dict, state: dict) -> list[dict]:
        events: list[dict] = []
        if not state.get("started"):
            state["started"] = True
            events.append({"event": "message_start", "data": {
                "type": "message_start",
                "message": {
                    "id": chunk.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message", "role": "assistant",
                    "model": chunk.get("model", ""),
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }})
            events.append({"event": "content_block_start", "data": {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""},
            }})

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            events.append({"event": "content_block_delta", "data": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": delta["content"]},
            }})

        finish = choice.get("finish_reason")
        usage = chunk.get("usage")
        if (finish or usage) and not state.get("done"):
            state["done"] = True
            events.extend(self._closing(finish, usage))
        return events

    @staticmethod
    def _closing(finish: str | None, usage: dict | None) -> list[dict]:
        return [
            {"event": "content_block_stop", "data": {
                "type": "content_block_stop", "index": 0}},
            {"event": "message_delta", "data": {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _FINISH_MAP.get(finish or "stop", "end_turn"),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": (usage or {}).get("completion_tokens", 0)},
            }},
            {"event": "message_stop", "data": {"type": "message_stop"}},
        ]

    def finish_sse(self, state: dict) -> list[dict]:
        if state.get("started") and not state.get("done"):
            state["done"] = True
            return self._closing(None, None)
        return []
