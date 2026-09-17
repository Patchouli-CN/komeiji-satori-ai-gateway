"""KomeijiSatori 主类：OpenAI 兼容转发 + 后台周期性核验 + 状态查询。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import Adapter, get_adapters
from .checkers import CheckResult, Checker
from .config import Config, Upstream
from .levels import AlertLevel, level_of
from .record import append_record, make_entry
from .rules import RuleEngine
from .tokenwatch import TokenizerWatch
from .watch import BillingWatch, HitRateWatch, LatencyWatch

log = logging.getLogger("satori")


def _extract_request_text(body: bytes) -> str:
    """从请求体里拼出全部用户可见文本（含多模态消息的 text 段）。"""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    parts: list[str] = []
    for msg in payload.get("messages", []):
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
    return "\n".join(parts)


def _extract_text(body: bytes, content_type: str) -> tuple[str, str, dict]:
    """从响应体里抽出 (正文, CoT, usage)。兼容 SSE 流和普通 JSON。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict = {}

    def collect(payload: dict, key: str) -> None:
        nonlocal usage
        if payload.get("usage"):
            usage = payload["usage"]
        for choice in payload.get("choices", []):
            part = choice.get(key) or {}
            if part.get("content"):
                content_parts.append(part["content"])
            rc = part.get("reasoning_content") or part.get("reasoning")
            if rc:
                reasoning_parts.append(rc)

    if "text/event-stream" in content_type:
        for line in body.decode(errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                collect(json.loads(data), "delta")
            except json.JSONDecodeError:
                continue
    else:
        try:
            collect(json.loads(body), "message")
        except json.JSONDecodeError:
            pass
    return "".join(content_parts), "".join(reasoning_parts), usage


class KomeijiSatori:
    """觉之瞳网关本体。

    用法：
        satori = KomeijiSatori(config, checkers)
        uvicorn.run(satori.app, host=..., port=...)
    """

    def __init__(
        self,
        config: Config,
        checkers: list[Checker],
        rule_engine: RuleEngine | None = None,
    ) -> None:
        self.config = config
        self.checkers = checkers
        self.rule_engine = rule_engine
        self.tokenwatch = TokenizerWatch()
        self.latencywatch = LatencyWatch()
        self.billingwatch = BillingWatch()
        self.hitratewatch = HitRateWatch(
            threshold=config.rules.hit_rate_threshold,
            min_samples=config.rules.hit_rate_min_samples,
        )
        # (checker, upstream, model) -> 最新核验结果
        self.results: dict[tuple[str, str, str], CheckResult] = {}
        # (upstream, model) -> 累计可疑度（写入时的有效值，读取需衰减）
        self.suspicion: dict[tuple[str, str], float] = {}
        # (upstream, model) -> 账本最后写入时刻（衰减基准）
        self._ledger_ts: dict[tuple[str, str], float] = {}
        # (upstream, model) -> 熔断时刻；在册即拦截
        self.breakers: dict[tuple[str, str], float] = {}
        # (upstream, model) -> 熔断期间拦截次数
        self._breaker_blocks: dict[tuple[str, str], int] = {}
        # /satori/live 的 WebSocket 订阅者
        self._subscribers: set[WebSocket] = set()
        self.app = self._create_app()

    # ---- 实时事件总线 ----

    async def publish(self, event: dict) -> None:
        """向所有订阅者广播事件；发不出去的连接顺手清掉。"""
        event.setdefault("ts", time.time())
        dead = []
        for ws in self._subscribers:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._subscribers.discard(ws)

    # ---- 核验 ----

    async def run_all_checks(self, client: httpx.AsyncClient) -> None:
        for upstream in self.config.upstreams:
            for model in upstream.models:
                for checker in self.checkers:
                    try:
                        r = await checker.check(client, upstream, model)
                    except Exception as exc:
                        r = CheckResult(
                            checker.name, upstream.name, model, False, 0.0,
                            f"checker 自身报错: {exc!r}",
                        )
                    self.results[(checker.name, upstream.name, model)] = r
                    level = logging.INFO if r.ok else logging.WARNING
                    log.log(level, "[%s] %s/%s: %s", r.checker, r.upstream, r.model, r.detail)
                    await self.publish({
                        "type": "check",
                        "checker": r.checker,
                        "upstream": r.upstream,
                        "model": r.model,
                        "ok": r.ok,
                        "score": r.score,
                        "detail": r.detail,
                    })

    async def _check_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                await self.run_all_checks(client)
                await asyncio.sleep(self.config.gateway.check_interval_seconds)

    # ---- 转发 ----

    @staticmethod
    def _build_forward_request(
        client: httpx.AsyncClient, upstream: Upstream, body: bytes
    ) -> httpx.Request:
        return client.build_request(
            "POST",
            f"{upstream.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {upstream.resolve_key()}",
                "Content-Type": "application/json",
            },
            content=body,
            timeout=300,
        )

    # ---- ASGI 应用 ----

    def _create_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.state.client = httpx.AsyncClient()
            task = asyncio.create_task(self._check_loop())
            yield
            task.cancel()
            await app.state.client.aclose()

        app = FastAPI(title="KomeijiSatori", lifespan=lifespan)
        app.state.satori = self

        # 前后分离：页面独立部署，跨域放开（本地面板场景）
        app.add_middleware(
            CORSMiddleware,
            allow_origins=self.config.gateway.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        for adapter in get_adapters():
            app.post(adapter.path)(self._make_proxy_handler(adapter))
        app.get("/v1/models")(self.list_models)
        app.get("/satori/status")(self.status)
        app.post("/satori/breaker/reset")(self.breaker_reset)
        app.websocket("/satori/live")(self.live)
        return app

    def _make_proxy_handler(self, adapter: Adapter):
        async def handler(request: Request):
            return await self._proxy(request, adapter)
        return handler

    async def _proxy(self, request: Request, adapter: Adapter):
        """统一转发入口：客户端协议 → 规范 → 上游 → 规范 → 客户端协议。

        检测（规则/侧信道/录制）全部工作在规范（Chat Completions）格式上，
        与客户端说什么协议无关。
        """
        body = await request.body()
        try:
            client_payload = json.loads(body)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        canonical = adapter.to_canonical(client_payload)
        model = canonical.get("model", "")

        # 流式请求补 stream_options.include_usage（OpenAI 标准字段），
        # 让 usage 分词侧信道在流式下也能拿到数据
        mutated = not adapter.passthrough
        if canonical.get("stream") and self.config.gateway.inject_usage:
            opts = canonical.setdefault("stream_options", {})
            if "include_usage" not in opts:
                opts["include_usage"] = True
                mutated = True
        fwd_body = json.dumps(canonical).encode() if mutated else body

        upstream = self.config.upstream_for(model)
        if upstream is None:
            return JSONResponse({"error": "no upstream configured"}, status_code=502)

        # 熔断检查：已跳闸的 上游×模型 直接拦截，不放行污染流量
        key = (upstream.name, model)
        if key in self.breakers:
            self._breaker_blocks[key] = self._breaker_blocks.get(key, 0) + 1
            log.info("[breaker] 拦截 %s/%s（第 %d 次）", upstream.name, model,
                     self._breaker_blocks[key])
            return JSONResponse({
                "error": "breaker open",
                "detail": f"{upstream.name}/{model} 可疑度越界已熔断——质量存疑的流量不会污染你的项目。"
                          "人工确认后 POST /satori/breaker/reset 复位",
                "since": self.breakers[key],
                "blocked": self._breaker_blocks[key],
            }, status_code=503)

        client: httpx.AsyncClient = request.app.state.client
        req = self._build_forward_request(client, upstream, fwd_body)
        started = time.perf_counter()
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await self.publish({
                "type": "request",
                "upstream": upstream.name,
                "model": model,
                "status": 502,
                "first_byte_ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes": 0,
            })
            return JSONResponse(
                {"error": "upstream unreachable", "detail": str(exc)},
                status_code=502,
            )
        first_byte_ms = (time.perf_counter() - started) * 1000

        if resp.status_code != 200:
            content = await resp.aread()
            await resp.aclose()
            await self.publish({
                "type": "request",
                "upstream": upstream.name,
                "model": model,
                "status": resp.status_code,
                "first_byte_ms": round(first_byte_ms, 1),
                "bytes": len(content),
            })
            return JSONResponse(
                {"error": "upstream error", "status": resp.status_code, "body": content.decode(errors="replace")},
                status_code=resp.status_code,
            )

        content_type = resp.headers.get("content-type", "")

        # 透传快车道：客户端协议即规范，逐字节转发
        if adapter.passthrough:
            return StreamingResponse(
                self._passthrough_stream(resp, upstream.name, model, fwd_body,
                                         content_type, started, first_byte_ms),
                status_code=resp.status_code,
                media_type=content_type or "application/json",
            )

        # 非流式：攒完整响应，检测后翻译回客户端协议
        if not canonical.get("stream"):
            raw = await resp.aread()
            await resp.aclose()
            await self._finalize(upstream.name, model, resp.status_code, content_type,
                                 raw, fwd_body, started, first_byte_ms, len(raw))
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return JSONResponse({"error": "upstream returned non-JSON"}, status_code=502)
            return JSONResponse(adapter.from_canonical(payload))

        # 流式翻译：规范 SSE → 客户端协议 SSE
        return StreamingResponse(
            self._translated_stream(resp, adapter, upstream.name, model, fwd_body,
                                    content_type, started, first_byte_ms),
            status_code=resp.status_code,
            media_type="text/event-stream",
        )

    async def _passthrough_stream(
        self, resp: httpx.Response, upstream: str, model: str, fwd_body: bytes,
        content_type: str, started: float, first_byte_ms: float,
    ):
        sent = 0
        parts: list[bytes] = []
        try:
            async for chunk in resp.aiter_bytes():
                sent += len(chunk)
                parts.append(chunk)
                yield chunk
        finally:
            await resp.aclose()
            await self._finalize(upstream, model, resp.status_code, content_type,
                                 b"".join(parts), fwd_body, started, first_byte_ms, sent)

    async def _translated_stream(
        self, resp: httpx.Response, adapter: Adapter, upstream: str, model: str,
        fwd_body: bytes, content_type: str, started: float, first_byte_ms: float,
    ):
        sent = 0
        parts: list[bytes] = []
        state: dict = {}

        def emit(events: list[dict]):
            out = []
            for evt in events:
                out.append(
                    f"event: {evt['event']}\n"
                    f"data: {json.dumps(evt['data'], ensure_ascii=False)}\n\n".encode()
                )
            return out

        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw_line = (line + "\n\n").encode()
                parts.append(raw_line)
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for out in emit(adapter.translate_sse(chunk, state)):
                    sent += len(out)
                    yield out
            for out in emit(adapter.finish_sse(state)):
                sent += len(out)
                yield out
        finally:
            await resp.aclose()
            await self._finalize(upstream, model, resp.status_code, content_type,
                                 b"".join(parts), fwd_body, started, first_byte_ms, sent)

    async def _finalize(
        self, upstream: str, model: str, status: int, content_type: str,
        raw: bytes, fwd_body: bytes, started: float, first_byte_ms: float, sent: int,
    ) -> None:
        """一次转发的收尾：提取 → 事件 → 规则 → 侧信道 → 录制。"""
        content, reasoning, usage = _extract_text(raw, content_type)
        request_text = _extract_request_text(fwd_body)
        await self.publish({
            "type": "request",
            "upstream": upstream,
            "model": model,
            "status": status,
            "first_byte_ms": round(first_byte_ms, 1),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
            "bytes": sent,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        })
        if self.rule_engine is not None:
            hits = await self._apply_rules(upstream, model, content, reasoning, request_text)
            # 命中率通道：严重规则（≥25 分）命中率，抗掺水
            hr_alert = self.hitratewatch.observe(
                upstream, model, any(h["score"] >= 25 for h in hits),
            )
            if hr_alert:
                await self._add_suspicion(upstream, model, [{
                    "rule": "hit-rate", "score": 20, "field": "content",
                    "snippet": hr_alert,
                    "description": "严重规则命中率通道（抗掺水）",
                }])
        # usage 分词侧信道
        tw_alert = self.tokenwatch.observe(
            upstream, model, len(request_text), usage.get("prompt_tokens", 0),
        )
        if tw_alert:
            await self._add_suspicion(upstream, model, [{
                "rule": "tokenizer-drift", "score": 30, "field": "usage",
                "snippet": tw_alert,
                "description": "usage 分词侧信道（自基线，无需官方参考）",
            }])
        # 延迟画像
        lat_alert = self.latencywatch.observe(upstream, model, first_byte_ms)
        if lat_alert:
            await self._add_suspicion(upstream, model, [{
                "rule": "latency-drift", "score": 15, "field": "latency",
                "snippet": lat_alert,
                "description": "首字节延迟画像漂移（自基线）",
            }])
        # 计费一致性
        bill_alert = self.billingwatch.observe(
            upstream, model, len(content), usage.get("completion_tokens", 0),
        )
        if bill_alert:
            await self._add_suspicion(upstream, model, [{
                "rule": "billing-drift", "score": 30, "field": "usage",
                "snippet": bill_alert,
                "description": "计费一致性审计（completion_tokens vs 实收文本）",
            }])
        if self.config.record.enabled:
            append_record(
                self.config.record.directory,
                make_entry(upstream, model, status,
                           request_text, content, reasoning, usage),
            )

    async def _apply_rules(
        self, upstream: str, model: str,
        content: str, reasoning: str, request_text: str = "",
    ) -> list[dict]:
        """对一条完整响应跑规则引擎，命中交给账本，并返回命中供命中率统计。"""
        assert self.rule_engine is not None
        hits = self.rule_engine.evaluate(content, reasoning, request_text)
        hit_dicts = [
            {"rule": h.rule, "score": h.score, "field": h.field,
             "snippet": h.snippet, "description": h.description}
            for h in hits
        ]
        if hit_dicts:
            await self._add_suspicion(upstream, model, hit_dicts)
        return hit_dicts

    def _decayed(self, key: tuple[str, str]) -> float:
        """账本有效分：按半衰期衰减后的当前值。

        孤立小错随时间归零，持续掺水的加分速度远超衰减，照样积聚——
        审计学的"重要性水平"：不追究孤立小错，只追频率异常。
        """
        score = self.suspicion.get(key, 0.0)
        if not score:
            return 0.0
        half = self.config.rules.decay_half_life_seconds
        if half <= 0:
            return score
        dt = time.time() - self._ledger_ts.get(key, time.time())
        return score * (0.5 ** (dt / half))

    async def _add_suspicion(self, upstream: str, model: str, hits: list[dict]) -> None:
        """可疑度账本：衰减、累加、广播、等级跃迁（SAFETY/WATCH/DEGRADED）。"""
        key = (upstream, model)
        gained = sum(h["score"] for h in hits)
        prev = self._decayed(key)
        total = max(0.0, prev + gained)  # 豁免分不能把账本扣成负数
        self.suspicion[key] = total
        self._ledger_ts[key] = time.time()

        watch_th = self.config.rules.watch_threshold
        break_th = self.config.rules.suspicion_threshold
        prev_level = level_of(prev, watch_th, break_th)
        new_level = level_of(total, watch_th, break_th)

        log.warning("[suspicion] %s/%s 可疑度 %+d → %d [%s]：%s",
                    upstream, model, gained, total, new_level.value,
                    ", ".join(h["rule"] for h in hits))
        await self.publish({
            "type": "suspicion",
            "upstream": upstream,
            "model": model,
            "gained": gained,
            "total": total,
            "level": new_level.value,
            "hits": hits,
        })

        # 等级跃迁：升级才广播，降级静默（复位走 breaker_reset 的 closed 事件）
        if new_level.rank > prev_level.rank:
            log.warning("[level] %s/%s 报警等级 %s → %s",
                        upstream, model, prev_level.value, new_level.value)
            await self.publish({
                "type": "level",
                "upstream": upstream,
                "model": model,
                "from": prev_level.value,
                "to": new_level.value,
                "total": total,
            })

        if new_level == AlertLevel.DEGRADED and prev_level != AlertLevel.DEGRADED:
            log.warning("[suspicion] %s/%s 可疑度越界（%d ≥ %d）——覚「想起うさぎは警戒を」",
                        upstream, model, total, break_th)
            await self.publish({
                "type": "alert",
                "upstream": upstream,
                "model": model,
                "total": total,
                "threshold": break_th,
            })
            # 熔断：味道变了实时停工，低质量输出不得污染项目
            if self.config.breaker.enabled:
                self.breakers[key] = time.time()
                log.warning("[breaker] %s/%s 熔断器跳闸，后续请求拦截直至人工复位",
                            upstream, model)
                await self.publish({
                    "type": "breaker",
                    "state": "open",
                    "upstream": upstream,
                    "model": model,
                    "total": total,
                })

    async def list_models(self):
        data = [
            {"id": m, "object": "model", "owned_by": up.name}
            for up in self.config.upstreams
            for m in up.models
        ]
        return {"object": "list", "data": data}

    async def breaker_reset(self, request: Request):
        """人工复位熔断器：清除熔断状态和可疑度。"""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        key = (payload.get("upstream", ""), payload.get("model", ""))
        was_open = key in self.breakers
        self.breakers.pop(key, None)
        self.suspicion.pop(key, None)
        self._ledger_ts.pop(key, None)
        if was_open:
            log.info("[breaker] %s/%s 已人工复位", key[0], key[1])
            await self.publish({
                "type": "breaker", "state": "closed",
                "upstream": key[0], "model": key[1],
            })
        return {"reset": was_open, "upstream": key[0], "model": key[1]}

    async def status(self):
        return {
            "checks": [
                {
                    "checker": r.checker,
                    "upstream": r.upstream,
                    "model": r.model,
                    "ok": r.ok,
                    "score": r.score,
                    "detail": r.detail,
                    "checked_at": r.checked_at,
                }
                for r in self.results.values()
            ],
            "suspicion": [
                {"upstream": up, "model": m,
                 "score": round(self._decayed((up, m)), 1),
                 "level": level_of(self._decayed((up, m)),
                                   self.config.rules.watch_threshold,
                                   self.config.rules.suspicion_threshold).value,
                 "threshold": self.config.rules.suspicion_threshold,
                 "watch_threshold": self.config.rules.watch_threshold}
                for (up, m), s in self.suspicion.items()
            ],
            "breakers": [
                {"upstream": up, "model": m, "since": since,
                 "blocked": self._breaker_blocks.get((up, m), 0)}
                for (up, m), since in self.breakers.items()
            ],
        }

    async def live(self, ws: WebSocket):
        """实时盯梢频道：连接即收全量快照，之后持续推送 check/request 事件。"""
        await ws.accept()
        self._subscribers.add(ws)
        try:
            await ws.send_json({"type": "snapshot", "results": await self.status()})
            while True:
                # 客户端目前不需要上行消息，receive 只为探测断连
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            self._subscribers.discard(ws)
