"""Anthropic Messages API ↔ 规范（Chat Completions）。

v2 范围（v4 Phase 4 adapter v2）：文本与图片 block（base64 → data URL）、
system、stop_sequences、流式事件翻译，以及 **tools / tool_use / tool_result
双向翻译**——没有它，检测核心看不到工具链，Phase 4 的 Slop 链级审计无从谈起
（v1 的 tool_calls 只活在 finish reason 映射里）。

翻译形状：
    请求  tools[{name,description,input_schema}] ↔ 规范 tools[{type:"function",function:{...,parameters}}]
          assistant 消息的 tool_use blocks ↔ assistant.tool_calls
          user 消息的 tool_result blocks ↔ role:"tool" 消息（tool_call_id）
    响应  message.tool_calls ↔ content[].tool_use blocks
          finish_reason=tool_calls ↔ stop_reason=tool_use（_FINISH_MAP 已有）
    流式  canonical delta.tool_calls ↔ content_block_start(tool_use) +
          content_block_delta(input_json_delta) 增量 + content_block_stop
"""

from __future__ import annotations

import json
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


def _tools_to_openai(tools) -> list[dict]:
    """Anthropic tools → 规范 tools。input_schema → parameters。"""
    out = []
    for t in tools or []:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _tool_choice_to_openai(choice):
    """Anthropic tool_choice → 规范。形状相近的直传，auto/none/any 同名；
    tool{name} → function{name}。"""
    if isinstance(choice, dict) and choice.get("type") == "tool":
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return choice


def _tool_result_text(content) -> str:
    """tool_result 的 content（str 或 block 列表）→ 纯文本。"""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _message_to_canonical(msg: dict) -> list[dict]:
    """单条 Anthropic 消息 → 零到多条规范消息。

    assistant 带 tool_use → 一条带 tool_calls 的 assistant 消息；
    user 带 tool_result → 若干 role:tool 消息（可能连同文本一条）。
    """
    role = msg.get("role", "user")
    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    texts: list = []  # str 或 image_url part（混排时整体作为 parts）
    tool_calls, tool_results = [], []
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                texts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
        elif btype == "tool_result":
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _tool_result_text(block.get("content")),
                }
            )

    out: list[dict] = []
    if role == "assistant" and tool_calls:
        text_only = "\n".join(t for t in texts if isinstance(t, str))
        out.append(
            {
                "role": "assistant",
                "content": text_only or None,
                "tool_calls": tool_calls,
            }
        )
    elif texts:
        if all(isinstance(t, str) for t in texts):
            out.append({"role": role, "content": "\n".join(texts)})
        else:
            # 图文混排：保持规范的 parts 形状
            parts: list[dict] = []
            for t in texts:
                parts.append({"type": "text", "text": t} if isinstance(t, str) else t)
            out.append({"role": role, "content": parts})
    out.extend(tool_results)
    if not out:
        return [{"role": role, "content": ""}]
    return out


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
            messages.extend(_message_to_canonical(msg))

        canonical: dict = {"model": body.get("model", ""), "messages": messages}
        for key in ("max_tokens", "temperature", "top_p", "stream"):
            if key in body:
                canonical[key] = body[key]
        if "stop_sequences" in body:
            canonical["stop"] = body["stop_sequences"]
        if body.get("tools"):
            canonical["tools"] = _tools_to_openai(body["tools"])
        if "tool_choice" in body:
            canonical["tool_choice"] = _tool_choice_to_openai(body["tool_choice"])
        return canonical

    # ---- 非流式响应 ----

    def from_canonical(self, payload: dict) -> dict:
        choice = (payload.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = payload.get("usage") or {}
        content_blocks: list[dict] = [
            {"type": "text", "text": msg.get("content") or ""}
        ]
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or ""
            try:
                input_json = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                # 畸形参数也照样转运——断链的现场留给检测核心去认
                input_json = {"_unparseable": raw_args}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fn.get("name", ""),
                    "input": input_json,
                }
            )
        return {
            "id": payload.get("id", "msg_satori"),
            "type": "message",
            "role": "assistant",
            "model": payload.get("model", ""),
            "content": content_blocks,
            "stop_reason": _FINISH_MAP.get(
                choice.get("finish_reason") or "stop", "end_turn"
            ),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    # ---- 流式响应 ----

    def translate_sse(self, chunk: dict, state: dict) -> list[dict]:
        events: list[dict] = []
        state.setdefault(
            "blocks", {}
        )  # canonical tool_calls index → Anthropic block index
        state.setdefault("open_tools", set())
        state.setdefault("text_open", True)
        if not state.get("started"):
            state["started"] = True
            events.append(
                {
                    "event": "message_start",
                    "data": {
                        "type": "message_start",
                        "message": {
                            "id": chunk.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
                            "type": "message",
                            "role": "assistant",
                            "model": chunk.get("model", ""),
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                }
            )
            events.append(
                {
                    "event": "content_block_start",
                    "data": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                }
            )

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            events.append(
                {
                    "event": "content_block_delta",
                    "data": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": delta["content"]},
                    },
                }
            )

        for tc in delta.get("tool_calls") or []:
            events.extend(self._tool_events(tc, state))

        finish = choice.get("finish_reason")
        usage = chunk.get("usage")
        if (finish or usage) and not state.get("done"):
            state["done"] = True
            events.extend(self._closing(finish, usage, state))
        return events

    @staticmethod
    def _tool_events(tc: dict, state: dict) -> list[dict]:
        """canonical tool_calls delta → Anthropic 工具块事件序列。"""
        idx = tc.get("index", 0)
        fn = tc.get("function") or {}
        events: list[dict] = []
        block_index = state["blocks"].get(idx)
        if block_index is None:
            block_index = 1 + len(state["blocks"])
            state["blocks"][idx] = block_index
            if state.get("text_open"):
                events.append(
                    {
                        "event": "content_block_stop",
                        "data": {"type": "content_block_stop", "index": 0},
                    }
                )
                state["text_open"] = False
            events.append(
                {
                    "event": "content_block_start",
                    "data": {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": fn.get("name", ""),
                            "input": {},
                        },
                    },
                }
            )
            state["open_tools"].add(block_index)
        args = fn.get("arguments")
        if args:
            events.append(
                {
                    "event": "content_block_delta",
                    "data": {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    },
                }
            )
        return events

    @staticmethod
    def _closing(
        finish: str | None, usage: dict | None, state: dict | None = None
    ) -> list[dict]:
        events: list[dict] = []
        # 先合上还开着的块：文本块 + 所有工具块（顺序无关，index 自带）
        if state is not None:
            if state.get("text_open"):
                events.append(
                    {
                        "event": "content_block_stop",
                        "data": {"type": "content_block_stop", "index": 0},
                    }
                )
                state["text_open"] = False
            for block_index in sorted(state.get("open_tools") or ()):
                events.append(
                    {
                        "event": "content_block_stop",
                        "data": {"type": "content_block_stop", "index": block_index},
                    }
                )
            (state.get("open_tools") or set()).clear()
        events.append(
            {
                "event": "message_delta",
                "data": {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _FINISH_MAP.get(finish or "stop", "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": {
                        "output_tokens": (usage or {}).get("completion_tokens", 0)
                    },
                },
            }
        )
        events.append({"event": "message_stop", "data": {"type": "message_stop"}})
        return events

    def finish_sse(self, state: dict) -> list[dict]:
        if state.get("started") and not state.get("done"):
            state["done"] = True
            return self._closing(None, None, state)
        return []
