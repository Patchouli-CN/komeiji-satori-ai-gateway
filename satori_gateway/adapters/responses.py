"""OpenAI Responses API ↔ 规范（Chat Completions）。

v1 范围：文本 input（str / message item 列表）与 instructions；
function calling 等 item 类型暂未翻译（检测侧照常工作）。
"""

from __future__ import annotations

import time
import uuid

from . import register_adapter


def _input_to_messages(body: dict) -> list[dict]:
    messages: list[dict] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict) or "role" not in item:
                continue
            content = item.get("content")
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict)
                    and p.get("type") in ("input_text", "output_text", "text")
                )
            messages.append(
                {"role": item.get("role", "user"), "content": content or ""}
            )
    return messages


@register_adapter
class ResponsesAdapter:
    name = "openai-responses"
    path = "/v1/responses"
    passthrough = False

    # ---- 请求 ----

    def to_canonical(self, body: dict) -> dict:
        canonical: dict = {
            "model": body.get("model", ""),
            "messages": _input_to_messages(body),
        }
        mapping = {
            "max_output_tokens": "max_tokens",
            "temperature": "temperature",
            "top_p": "top_p",
            "stream": "stream",
        }
        for src, dst in mapping.items():
            if src in body:
                canonical[dst] = body[src]
        return canonical

    # ---- 非流式响应 ----

    def from_canonical(self, payload: dict) -> dict:
        choice = (payload.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        return {
            "id": payload.get("id", f"resp_{uuid.uuid4().hex[:24]}"),
            "object": "response",
            "created_at": payload.get("created", int(time.time())),
            "status": "completed",
            "model": payload.get("model", ""),
            "output": [
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            ],
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

    # ---- 流式响应 ----

    def _shell(self, state: dict, status: str) -> dict:
        return {
            "id": state["id"],
            "object": "response",
            "created_at": state["created"],
            "status": status,
            "model": state.get("model", ""),
        }

    def translate_sse(self, chunk: dict, state: dict) -> list[dict]:
        events: list[dict] = []
        if not state.get("started"):
            state["started"] = True
            state["id"] = (chunk.get("id") or f"resp_{uuid.uuid4().hex[:24]}").replace(
                "chatcmpl", "resp"
            )
            state["created"] = chunk.get("created", int(time.time()))
            state["model"] = chunk.get("model", "")
            state["item_id"] = f"msg_{uuid.uuid4().hex[:24]}"
            state["text"] = []
            events.append(
                {
                    "event": "response.created",
                    "data": {
                        "type": "response.created",
                        "response": self._shell(state, "in_progress"),
                    },
                }
            )
            events.append(
                {
                    "event": "response.output_item.added",
                    "data": {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": state["item_id"],
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                }
            )
            events.append(
                {
                    "event": "response.content_part.added",
                    "data": {
                        "type": "response.content_part.added",
                        "item_id": state["item_id"],
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                }
            )

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            state["text"].append(delta["content"])
            events.append(
                {
                    "event": "response.output_text.delta",
                    "data": {
                        "type": "response.output_text.delta",
                        "item_id": state["item_id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": delta["content"],
                    },
                }
            )

        finish = choice.get("finish_reason")
        usage = chunk.get("usage")
        if (finish or usage) and not state.get("done"):
            state["done"] = True
            events.extend(self._closing(state, usage))
        return events

    def _closing(self, state: dict, usage: dict | None) -> list[dict]:
        text = "".join(state.get("text", []))
        u = usage or {}
        item = {
            "type": "message",
            "id": state["item_id"],
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        shell = self._shell(state, "completed")
        shell["output"] = [item]
        shell["usage"] = {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
        }
        base = {"item_id": state["item_id"], "output_index": 0, "content_index": 0}
        return [
            {
                "event": "response.output_text.done",
                "data": {"type": "response.output_text.done", **base, "text": text},
            },
            {
                "event": "response.content_part.done",
                "data": {
                    "type": "response.content_part.done",
                    **base,
                    "part": {"type": "output_text", "text": text, "annotations": []},
                },
            },
            {
                "event": "response.output_item.done",
                "data": {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": item,
                },
            },
            {
                "event": "response.completed",
                "data": {"type": "response.completed", "response": shell},
            },
        ]

    def finish_sse(self, state: dict) -> list[dict]:
        if state.get("started") and not state.get("done"):
            state["done"] = True
            return self._closing(state, None)
        return []
