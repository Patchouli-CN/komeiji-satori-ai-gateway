"""Anthropic Messages API 原生上游插件。

传输层：{base_url}/messages + x-api-key 鉴权。
格式层：canonical（OpenAI Chat）↔ Anthropic 原生格式的请求/非流式响应/SSE 流
全套翻译。v1 范围与客户端侧 adapters/anthropic.py 对齐：文本与图片 block、
system、stop_sequences；tools / thinking block 暂未翻译。
"""

from __future__ import annotations

from ..config import Upstream
from . import TransferPipeline

_FINISH_MAP = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop",
               "tool_use": "tool_calls"}


@TransferPipeline.register("upstream", "anthropic")
class AnthropicUpstream:
    """传输层：{base_url}/messages + x-api-key + anthropic-version。"""

    format = "anthropic"

    def __init__(self, cfg: Upstream) -> None:
        self.cfg = cfg

    def endpoint(self) -> tuple[str, dict]:
        return (
            f"{self.cfg.base_url}/messages",
            {
                "x-api-key": self.cfg.resolve_key(),
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )


def _content_to_blocks(content) -> list[dict]:
    """OpenAI content（str 或 part 列表）→ Anthropic content blocks。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict] = []
    for part in content or []:
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                header, _, data = url.partition(",")
                media_type = header[5:].split(";")[0] or "image/png"
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": data,
                }})
    return blocks or [{"type": "text", "text": ""}]


@TransferPipeline.register("convert", "anthropic")
class AnthropicConverter:
    """格式层：canonical ↔ Anthropic 原生格式。"""

    passthrough = False
    capabilities: set[str] = set()  # Anthropic 无 logprobs，声纹通道跳过

    # ---- 请求 canonical → native ----

    def request_to_native(self, canonical: dict) -> dict:
        messages: list[dict] = []
        system_parts: list[str] = []
        for msg in canonical.get("messages", []):
            role = msg.get("role", "user")
            content = msg.get("content")
            if role == "system":
                # system 消息抽为顶层 system 字段
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    system_parts.extend(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                continue
            messages.append({
                "role": role if role in ("user", "assistant") else "user",
                "content": _content_to_blocks(content),
            })

        native: dict = {
            "model": canonical.get("model", ""),
            "messages": messages,
            # Anthropic 必填 max_tokens，缺省补 4096
            "max_tokens": canonical.get("max_tokens") or 4096,
            "stream": bool(canonical.get("stream")),
        }
        if system_parts:
            native["system"] = "\n".join(system_parts)
        for key in ("temperature", "top_p"):
            if key in canonical:
                native[key] = canonical[key]
        if "stop" in canonical:
            stop = canonical["stop"]
            native["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        # logprobs / top_logprobs / stream_options 无对应能力，丢弃
        return native

    # ---- 非流式响应 native → canonical ----

    def response_to_canonical(self, native: dict) -> dict:
        text = "".join(
            b.get("text", "") for b in native.get("content", [])
            if b.get("type") == "text"
        )
        usage = native.get("usage") or {}
        return {
            "id": native.get("id", "chatcmpl-satori"),
            "object": "chat.completion",
            "model": native.get("model", ""),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _FINISH_MAP.get(
                    native.get("stop_reason") or "end_turn", "stop"),
            }],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
        }

    # ---- SSE native → canonical（状态机） ----

    def new_stream_state(self) -> dict:
        return {"input_tokens": 0, "id": "chatcmpl-satori", "model": ""}

    def translate_sse_data(self, event: dict, state: dict) -> list[dict]:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            state["id"] = message.get("id") or state["id"]
            state["model"] = message.get("model") or state["model"]
            state["input_tokens"] = (message.get("usage") or {}).get("input_tokens", 0)
            return [self._chunk(state, delta={"role": "assistant"})]
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [self._chunk(state, delta={"content": delta["text"]})]
            return []
        if etype == "message_delta":
            # 收尾：stop_reason → finish_reason，usage 合并 input/output
            delta = event.get("delta") or {}
            output_tokens = (event.get("usage") or {}).get("output_tokens", 0)
            chunk = self._chunk(
                state,
                finish=_FINISH_MAP.get(delta.get("stop_reason") or "end_turn", "stop"),
            )
            chunk["usage"] = {
                "prompt_tokens": state["input_tokens"],
                "completion_tokens": output_tokens,
            }
            return [chunk]
        # message_stop / ping / content_block_start / content_block_stop：无产出
        return []

    @staticmethod
    def _chunk(
        state: dict, delta: dict | None = None, finish: str | None = None
    ) -> dict:
        return {
            "id": state["id"],
            "object": "chat.completion.chunk",
            "model": state["model"],
            "choices": [{
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish,
            }],
        }
