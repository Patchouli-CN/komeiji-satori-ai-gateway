"""TransferPipeline：声明式上游协议适配层。

客户端侧协议由 adapters/ 翻译成内部规范（OpenAI Chat Completions）；
本包负责规范 → 上游原生协议 的最后一公里，两类插件：

  - upstream 插件：传输层——端点 URL、鉴权头，并声明自己说的原生格式（format）
  - convert 插件：格式层——canonical ↔ 原生格式的请求/响应/SSE 翻译

管线按 upstream 插件声明的 format 自动配转换器，也可 .convert(name) 手动覆盖：

    pipe = TransferPipeline.from_upstream("anthropic", upstream_cfg)  # 自动 .convert("anthropic")
    prepared = pipe.build_request(canonical)   # canonical → PreparedRequest
    payload = pipe.parse_response(raw)         # 原生 JSON → canonical
    async for chunk in pipe.translate_stream(resp.aiter_lines()): ...  # 原生 SSE → canonical chunk

新增上游协议只要把模块放进 pipelines/ 并加 @TransferPipeline.register，
包扫描自动发现（与 checkers/adapters 注册表同款套路）。
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

import httpx

from ..config import Upstream

log = logging.getLogger("satori")


@dataclass
class PreparedRequest:
    """一次上游调用的传输层就绪请求。"""

    method: str
    url: str
    headers: dict
    body: bytes


class TransferPipeline:
    """上游传输插件 + 格式转换器的组合体。"""

    _UPSTREAMS: dict[str, type] = {}
    _CONVERTERS: dict[str, type] = {}

    def __init__(self, upstream_plugin, converter) -> None:
        self.upstream = upstream_plugin
        self.converter = converter

    # ---- 注册 ----

    @classmethod
    def register(cls, kind: str, name: str):
        """装饰器工厂：@TransferPipeline.register("upstream"/"convert", name)。"""
        table = {"upstream": cls._UPSTREAMS, "convert": cls._CONVERTERS}.get(kind)
        if table is None:
            raise ValueError(f"未知插件类别 {kind!r}（应为 upstream/convert）")

        def deco(plugin_cls: type) -> type:
            table[name] = plugin_cls
            return plugin_cls

        return deco

    # ---- 装配 ----

    @classmethod
    def from_upstream(cls, name: str, cfg: Upstream) -> TransferPipeline:
        """按上游协议名装配管线，按插件声明的 format 自动配转换器。"""
        discover_pipelines()
        try:
            plugin_cls = cls._UPSTREAMS[name]
        except KeyError:
            raise ValueError(
                f"未知上游协议 {name!r}，已注册: {sorted(cls._UPSTREAMS)}"
            ) from None
        plugin = plugin_cls(cfg)
        return cls(plugin, None).convert(plugin.format)

    def convert(self, name: str) -> TransferPipeline:
        """覆盖转换器（链式），name 为原生格式名。"""
        try:
            conv_cls = self._CONVERTERS[name]
        except KeyError:
            raise ValueError(
                f"未知原生格式 {name!r}，已注册: {sorted(self._CONVERTERS)}"
            ) from None
        self.converter = conv_cls()
        return self

    # ---- 属性 ----

    @property
    def passthrough(self) -> bool:
        """转换器是否恒等（openai 时为 True，供透传快车道判断）。"""
        return self.converter.passthrough

    @property
    def capabilities(self) -> set[str]:
        """该上游的能力集，如 {"logprobs"}；无 logprobs 能力的上游跳过声纹通道。"""
        return self.converter.capabilities

    # ---- 三个动词 ----

    def build_request(self, canonical: dict) -> PreparedRequest:
        """canonical 请求体 → 传输层就绪请求（转换器出 body，插件出 url/headers）。"""
        native = self.converter.request_to_native(canonical)
        url, headers = self.upstream.endpoint()
        return PreparedRequest(
            "POST", url, headers, json.dumps(native, ensure_ascii=False).encode("utf-8")
        )

    def parse_response(self, raw: bytes) -> dict:
        """原生非流式 JSON 响应 → canonical dict。"""
        return self.converter.response_to_canonical(json.loads(raw))

    async def translate_stream(
        self, lines: Iterable[str] | AsyncIterator[str]
    ) -> AsyncIterator[dict]:
        """原生 SSE 行流 → canonical chunk dict 流（per-request state 在转换器里）。"""
        state = self.converter.new_stream_state()
        async for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            for chunk in self.converter.translate_sse_data(event, state):
                yield chunk


# 注册表自动发现与实例缓存
_PIPELINES: dict[str, TransferPipeline] = {}
_discovered = False


def discover_pipelines() -> None:
    """扫描 pipelines 包下所有模块，触发其中的装饰器注册。"""
    global _discovered
    if _discovered:
        return
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")
    _discovered = True
    log.info(
        "pipeline 自动发现完成：%d 个上游协议，%d 个格式转换器",
        len(TransferPipeline._UPSTREAMS), len(TransferPipeline._CONVERTERS),
    )


def build_pipeline(upstream: Upstream) -> TransferPipeline:
    """按 upstream.protocol 构建管线，按上游名缓存。"""
    pipe = _PIPELINES.get(upstream.name)
    if pipe is None:
        pipe = TransferPipeline.from_upstream(upstream.protocol, upstream)
        _PIPELINES[upstream.name] = pipe
    return pipe


async def chat_once(
    client: httpx.AsyncClient, upstream: Upstream, payload: dict, timeout: float = 30
) -> dict:
    """探针一次性问答：canonical 请求 → canonical 响应。

    四个 checker 与 CLI collect/answers 的统一入口，协议细节全在管线里。
    """
    pipe = build_pipeline(upstream)
    prepared = pipe.build_request(payload)
    resp = await client.post(
        prepared.url, headers=prepared.headers, content=prepared.body, timeout=timeout
    )
    resp.raise_for_status()
    return pipe.parse_response(resp.content)
