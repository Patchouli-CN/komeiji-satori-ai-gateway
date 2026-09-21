"""KomeijiSatori 主类：OpenAI 兼容转发 + 后台周期性核验 + 状态查询。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .adapters import Adapter, get_adapters
from .baseline import BaselineManager
from .checkers import CheckResult, Checker
from .config import Config
from .levels import AlertLevel, level_of
from .pipelines import TransferPipeline, build_pipeline
from .record import append_record, make_entry
from .rules import RuleEngine
from .security import ControlAuth, Role, TrustLevel
from .slop import CONFIRMED_SCORE, SlopLedger, score_tool_calls
from .state import StateStore
from .testing import TestAdjudicator, TestReport
from .tokenwatch import TokenizerWatch
from .ttl import TtlEngine
from .watch import BillingWatch, HitRateWatch, LatencyWatch

log = logging.getLogger("satori")

# 身份类核验通道失败注入身份账本的权重（v4 Phase 5A 维度隔离）：
# 声纹/答案指纹是"是不是你"的证据，与质量类分开记账——测试 PASS 洗不掉。
# 无参考时 checker 返回 score=0，天然跳过（BASIC 模式不积累）。
# 权重保守：单次失败不足以独立熔断，持续对不上才会累积到阈值
_IDENTITY_CHECKERS: dict[str, float] = {"fingerprint": 20.0, "answerprint": 10.0}


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


def _merge_tool_call(calls: dict[int, dict], tc: dict) -> None:
    """流式 tool_calls 增量并入组装表（arguments 是分片到达的）。"""
    idx = tc.get("index", 0)
    cur = calls.setdefault(idx, {"id": "", "type": "function",
                                 "function": {"name": "", "arguments": ""}})
    if tc.get("id"):
        cur["id"] = tc["id"]
    fn = tc.get("function") or {}
    if fn.get("name"):
        cur["function"]["name"] = fn["name"]
    if fn.get("arguments"):
        cur["function"]["arguments"] += fn["arguments"]


def _extract_tool_calls(body: bytes, content_type: str) -> list[dict]:
    """从规范响应里组装 tool_calls（非流式 JSON 或流式 SSE 增量拼接）。"""
    calls: dict[int, dict] = {}

    def collect(payload: dict) -> None:
        for choice in payload.get("choices", []):
            for tc in (choice.get("message") or {}).get("tool_calls") or []:
                _merge_tool_call(calls, tc)
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                _merge_tool_call(calls, tc)

    if "text/event-stream" in content_type:
        for line in body.decode(errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                collect(json.loads(data))
            except json.JSONDecodeError:
                continue
    else:
        try:
            collect(json.loads(body))
        except json.JSONDecodeError:
            pass
    return [calls[i] for i in sorted(calls)]


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
        # (upstream, model) -> 身份类累计可疑度（声纹/答案指纹）。
        # 与质量类分开记账：测试 PASS 洗得掉质量类，洗不掉身份类（v4 Phase 5A）
        self.identity: dict[tuple[str, str], float] = {}
        # (upstream, model) -> 身份账本最后写入时刻（衰减基准）
        self._identity_ts: dict[tuple[str, str], float] = {}
        # (upstream, model) -> 熔断时刻；在册即拦截
        self.breakers: dict[tuple[str, str], float] = {}
        # (upstream, model) -> 熔断期间拦截次数
        self._breaker_blocks: dict[tuple[str, str], int] = {}
        # /satori/live 的 WebSocket 订阅者
        self._subscribers: set[WebSocket] = set()
        # 控制面封印授权（Gate 0 / Phase 6）：feedback/reset/admin 的信任链
        self.security = ControlAuth(
            db=config.security.db,
            enabled=config.security.enabled,
            mode=config.security.mode,
            admin_secret=config.security.admin_secret,
            window_seconds=config.security.timestamp_window_seconds,
            host=config.gateway.host,
            require_tls=config.security.require_tls,
            trusted_proxies=config.security.trusted_proxies,
        )
        # 审计状态持久化 + 基线判别账（v4 Phase 1.0/1.3）
        self.state = StateStore(config.state.directory, config.state.debounce_seconds)
        # 动态 TTL 引擎（v4 Phase 2）：保质期学习 + 老化预警（只预警不退役）
        self.ttl = TtlEngine(self.state)
        self.baselines = BaselineManager(config.fingerprint, config.upstreams,
                                         config.state.archive_dir, self.state,
                                         ttl_engine=self.ttl)
        # 业务测试裁决引擎（v4 Phase 5A）：滑窗 + 幂等 + flaky，
        # 重启后从 state/test_reports.jsonl 重建
        self.tests = TestAdjudicator(self.state)
        # Tool Call 链级 Slop 审计（v4 Phase 4）：session 累计怀疑→实锤
        self.slop = SlopLedger()
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
                    # 身份类通道失败注入身份账本（v4 开放讨论点定论：探针失败
                    # 要入账，但入的是身份账）。无参考时 score=0 天然跳过——
                    # BASIC 模式不积累。权重保守：单次失败不足以独立熔断
                    if (not r.ok and r.score > 0
                            and checker.name in _IDENTITY_CHECKERS):
                        await self._add_suspicion(
                            upstream.name, model,
                            [{"rule": checker.name,
                              "score": _IDENTITY_CHECKERS[checker.name],
                              "field": "identity", "snippet": r.detail,
                              "description": "身份类核验失败（不可被测试 PASS 洗白）"}],
                            identity=True,
                        )

    async def _check_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                await self.run_all_checks(client)
                await self._ttl_cycle()
                await asyncio.sleep(self.config.gateway.check_interval_seconds)

    async def _ttl_cycle(self) -> None:
        """TTL 老化预警（v4 Phase 2）：每轮核验顺带点灯，状态翻转才广播。
        预测只预警——退役必经 feedback 的地面真值（预测不误杀）。"""
        self.baselines.recompute_all()
        for key, st in self.baselines.states.items():
            verdict = self.ttl.verdict(st.model, st.collected_at)
            if verdict is None:
                continue
            warning = self.ttl.due_warnings(verdict)
            if warning:
                log.warning("[ttl] %s/%s %s", key[0], key[1], warning)
                await self.publish({"type": "ttl", "upstream": key[0],
                                    "model": key[1], **verdict.to_dict(),
                                    "warning": warning})

    # ---- 转发 ----

    @staticmethod
    def _build_forward_request(
        client: httpx.AsyncClient, pipe: TransferPipeline,
        canonical: dict, body: bytes, mutated: bool,
    ) -> httpx.Request:
        prepared = pipe.build_request(canonical)
        # 恒等管线且请求体未突变时透传原始 body 字节，省一次序列化
        content = body if (not mutated and pipe.passthrough) else prepared.body
        return client.build_request(
            prepared.method,
            prepared.url,
            headers=prepared.headers,
            content=content,
            timeout=300,
        )

    # ---- ASGI 应用 ----

    def _create_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.state.client = httpx.AsyncClient()
            # 控制面自检：鉴权关闭→醒目警告；回环+无 admin_secret→打印引导 token；
            # 非回环+无 admin_secret→拒绝启动（不接受带伤监听）
            self.security.startup_check()
            # 状态恢复（v4 Phase 1.0）：审计状态是时间函数，重启不失忆
            if self.state.restore(self, self.state.load()):
                await self.publish({"type": "restored",
                                    "ledger": self.state.snapshot(self)})
            # 基线判别自检（v4 Phase 1.4）：知道自己戴着哪副眼镜上岗
            self.baselines.recompute_all()
            for line in self.baselines.startup_report():
                log.warning("[WARN] %s", line)
            task = asyncio.create_task(self._check_loop())
            yield
            task.cancel()
            await app.state.client.aclose()
            self.state.flush(self)

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
        # 端点授权矩阵（v4 Phase 6A）：reset 需要 operator——今天它完全裸奔，最高危
        app.post("/satori/breaker/reset",
                 dependencies=[Depends(self.security.require(Role.OPERATOR))])(
            self.breaker_reset
        )
        # 误报反馈（v4 Phase 1.1）：reporter 提建议；official_update 确认累积到
        # 阈值（冷启动 1 次 / 正常 3 次），或 operator 一锤定音 → 基线退役
        app.post("/satori/baseline/feedback",
                 dependencies=[Depends(self.security.require(Role.REPORTER))])(
            self.baseline_feedback
        )
        # 业务测试上报（v4 Phase 5A）：质量锚点入口。
        # 信任轴门控（TRUSTED 才衰减/熔断）在端点内叠加，信号轴滑窗在
        # TestAdjudicator——两轴都要过，缺一即被绕过
        app.post("/satori/test/report",
                 dependencies=[Depends(self.security.require(Role.REPORTER))])(
            self.test_report
        )
        # 身份类嫌疑解冻裁决（v4 Phase 5A 维度隔离的第二条路）：
        # 重新采集基线之外，operator 可以显式为身份账本翻案——只清身份类
        app.post("/satori/baseline/identity-cleared",
                 dependencies=[Depends(self.security.require(Role.OPERATOR))])(
            self.identity_cleared
        )
        # 手动锁定保质期（v4 Phase 2 操作员工具）：锁定≠退役，
        # expires_at 到期或手动清除后自动回归学习值
        app.post("/satori/baseline/ttl/override",
                 dependencies=[Depends(self.security.require(Role.OPERATOR))])(
            self.ttl_override
        )
        # Admin：凭据签发/查询/吊销（Phase 6C），admin_secret 或引导 token 把关
        admin = [Depends(self.security.require_admin())]
        app.post("/satori/admin/credentials", dependencies=admin)(
            self.admin_issue_credential
        )
        app.get("/satori/admin/credentials", dependencies=admin)(
            self.admin_list_credentials
        )
        app.get("/satori/admin/credentials/{reporter_id}", dependencies=admin)(
            self.admin_get_credential
        )
        app.delete("/satori/admin/credentials/{reporter_id}", dependencies=admin)(
            self.admin_revoke_credential
        )
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
        # Tool Call 链级审计的线索（v4 Phase 4）：每请求一个 trace_id；
        # session 由客户端 X-Satori-Session 头声明（没有就按请求粒度算）
        trace_id = uuid.uuid4().hex[:16]
        session_id = request.headers.get("x-satori-session", "")

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
        pipe = build_pipeline(upstream)
        req = self._build_forward_request(client, pipe, canonical, body, mutated)
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

        # 透传快车道：客户端协议与上游协议都是规范本身，逐字节转发
        if adapter.passthrough and pipe.passthrough:
            return StreamingResponse(
                self._passthrough_stream(resp, upstream.name, model, fwd_body,
                                         content_type, started, first_byte_ms,
                                         session_id),
                status_code=resp.status_code,
                media_type=content_type or "application/json",
            )

        # 非流式：攒完整响应，管线译回规范，检测后翻译回客户端协议
        if not canonical.get("stream"):
            raw = await resp.aread()
            await resp.aclose()
            try:
                payload = pipe.parse_response(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse({"error": "upstream returned non-JSON"}, status_code=502)
            # 检测/录制看到的永远是规范格式
            canonical_body = json.dumps(payload, ensure_ascii=False).encode()
            await self._finalize(upstream.name, model, resp.status_code,
                                 "application/json", canonical_body, fwd_body,
                                 started, first_byte_ms, len(raw), session_id)
            return JSONResponse(adapter.from_canonical(payload))

        # 流式翻译：上游 SSE → 规范 → 客户端协议 SSE
        return StreamingResponse(
            self._translated_stream(resp, adapter, pipe, upstream.name, model,
                                    fwd_body, started, first_byte_ms, session_id),
            status_code=resp.status_code,
            media_type="text/event-stream",
        )

    async def _passthrough_stream(
        self, resp: httpx.Response, upstream: str, model: str, fwd_body: bytes,
        content_type: str, started: float, first_byte_ms: float,
        session_id: str = "",
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
                                 b"".join(parts), fwd_body, started, first_byte_ms,
                                 sent, session_id)

    async def _translated_stream(
        self, resp: httpx.Response, adapter: Adapter, pipe: TransferPipeline,
        upstream: str, model: str, fwd_body: bytes,
        started: float, first_byte_ms: float, session_id: str = "",
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
            # 管线把上游原生 SSE 译成规范 chunk；规范 chunk 序列化进 parts，
            # _finalize/_extract_text 看到的永远是规范 SSE
            async for chunk in pipe.translate_stream(resp.aiter_lines()):
                parts.append(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                )
                if adapter.passthrough:
                    # 客户端说的就是规范协议，直接吐规范 SSE 行
                    out = parts[-1]
                    sent += len(out)
                    yield out
                else:
                    for out in emit(adapter.translate_sse(chunk, state)):
                        sent += len(out)
                        yield out
            if adapter.passthrough:
                tail = b"data: [DONE]\n\n"
                sent += len(tail)
                yield tail
            else:
                for out in emit(adapter.finish_sse(state)):
                    sent += len(out)
                    yield out
        finally:
            await resp.aclose()
            await self._finalize(upstream, model, resp.status_code,
                                 "text/event-stream", b"".join(parts), fwd_body,
                                 started, first_byte_ms, sent, session_id)

    async def _finalize(
        self, upstream: str, model: str, status: int, content_type: str,
        raw: bytes, fwd_body: bytes, started: float, first_byte_ms: float, sent: int,
        session_id: str = "",
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
        # Tool Call 链级审计（v4 Phase 4）：有工具调用才开张
        tool_calls = _extract_tool_calls(raw, content_type)
        if tool_calls:
            await self._observe_tools(upstream, model, fwd_body, tool_calls,
                                      session_id)

    async def _observe_tools(self, upstream: str, model: str, fwd_body: bytes,
                             tool_calls: list[dict], session_id: str) -> None:
        """一次响应的工具链 → 结构化评分 → session 累计 → 实锤注入账本。

        诚实的边界：slop_score 是结构化启发式（断链/幻觉工具/复读/膨胀），
        不是语义判定——但它恰好覆盖"降级模型污染下游"的最典型形态：
        参数 JSON 断裂。语义回放基线是后续。
        """
        declared: list = []
        try:
            declared = json.loads(fwd_body).get("tools") or []
        except (json.JSONDecodeError, AttributeError):
            pass
        trace = score_tool_calls(tool_calls, declared)
        if trace is None:
            return
        session = session_id or trace.trace_id
        self.state.append_tool_trace({
            **trace.to_dict(), "upstream": upstream, "model": model,
            "session": session,
        })
        if trace.slop_score <= 0:
            return
        # 怀疑标记：单步偏差超阈值。事件广播出去，Dashboard 可以点亮
        flags = ", ".join(f for s in trace.steps for f in s.flags)
        log.warning("[slop] %s/%s 工具链可疑 score=%.0f（首个突变步 #%s）：%s",
                    upstream, model, trace.slop_score, trace.first_suspicious,
                    flags)
        await self.publish({"type": "slop", "state": "suspicious",
                            "upstream": upstream, "model": model,
                            "session": session, **trace.to_dict()})
        confirmed = self.slop.observe(session, trace)
        if confirmed is None:
            return
        # 实锤：同 session 累计 N 步可疑——注入 DEGRADED 级**质量类**嫌疑
        # （权重高于 Logprob；可被 L0/L1 PASS 部分衰减，但洗不穿负分地板）
        await self._add_suspicion(upstream, model, [{
            "rule": "confirmed-slop", "score": CONFIRMED_SCORE, "field": "tools",
            "snippet": f"session {session} 累计 {len(confirmed['steps'])} 步可疑，"
                       f"起源 trace {confirmed['origin_trace']} 步 "
                       f"#{confirmed['origin_step']}",
            "description": "Tool Call 链级 Slop 实锤（权重高于 Logprob）",
        }])
        log.warning("[slop] %s/%s 实锤：session %s 累计 %d 步——"
                    "覚「偽りの魂に、真の力は宿らない」",
                    upstream, model, session, len(confirmed["steps"]))
        await self.publish({"type": "slop", "state": "confirmed",
                            "upstream": upstream, "model": model,
                            "session": session, **confirmed})

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

    def _decay_component(self, values: dict, stamps: dict,
                         key: tuple[str, str]) -> float:
        """单个账本组件的半衰期折算。

        孤立小错随时间归零，持续掺水的加分速度远超衰减，照样积聚——
        审计学的"重要性水平"：不追究孤立小错，只追频率异常。
        """
        score = values.get(key, 0.0)
        if not score:
            return 0.0
        half = self.config.rules.decay_half_life_seconds
        if half <= 0:
            return score
        dt = time.time() - stamps.get(key, time.time())
        return score * (0.5 ** (dt / half))

    def _decayed_quality(self, key: tuple[str, str]) -> float:
        """质量类有效分：规则/侧信道/测试 FAIL。可被 TRUSTED 的 PASS 衰减。"""
        return self._decay_component(self.suspicion, self._ledger_ts, key)

    def _decayed_identity(self, key: tuple[str, str]) -> float:
        """身份类有效分：声纹 JS / 答案指纹。测试 PASS 洗不掉（维度隔离）。"""
        return self._decay_component(self.identity, self._identity_ts, key)

    def _decayed(self, key: tuple[str, str]) -> float:
        """账本有效总分 = 质量类 + 身份类。"""
        return self._decayed_quality(key) + self._decayed_identity(key)

    async def _add_suspicion(self, upstream: str, model: str, hits: list[dict],
                             *, identity: bool = False) -> None:
        """可疑度账本：衰减、累加、广播、等级跃迁（SAFETY/WATCH/DEGRADED）。

        identity=True 记身份类账本（声纹/答案指纹）——两组分分开衰减口径：
        测试 PASS 洗得掉质量类，洗不掉身份类（v4 Phase 5A 维度隔离）。
        """
        key = (upstream, model)
        gained = sum(h["score"] for h in hits)
        prev_q = self._decayed_quality(key)
        prev_i = self._decayed_identity(key)
        prev = prev_q + prev_i
        now = time.time()
        if identity:
            component = max(0.0, prev_i + gained)
            self.identity[key] = component
            self._identity_ts[key] = now
            total = prev_q + component
        else:
            component = max(0.0, prev_q + gained)  # 豁免分不能把账本扣成负数
            self.suspicion[key] = component
            self._ledger_ts[key] = now
            total = component + prev_i

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

        # 账本变更防抖落盘（v4 Phase 1.0）：重启不失忆
        self.state.mark_dirty()
        self.state.maybe_flush(self)

    def _decay_quality(self, key: tuple[str, str], amount: float,
                       floor: float) -> float:
        """PASS 衰减（v4 Phase 5A 非对称注入）：只洗质量类，且洗不穿负分地板；
        身份类嫌疑分文不动——维度隔离的另一半在这。"""
        now = time.time()
        q = self._decayed_quality(key)
        identity = self._decayed_identity(key)
        new_q = max(q - amount, floor - identity, 0.0)
        self.suspicion[key] = new_q
        self._ledger_ts[key] = now
        self.state.mark_dirty()
        self.state.maybe_flush(self)
        return new_q

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
        self.identity.pop(key, None)
        self._identity_ts.pop(key, None)
        if was_open:
            log.info("[breaker] %s/%s 已人工复位", key[0], key[1])
            await self.publish({
                "type": "breaker", "state": "closed",
                "upstream": key[0], "model": key[1],
            })
        return {"reset": was_open, "upstream": key[0], "model": key[1]}

    # ---- TTL 手动覆盖（Phase 2） ----

    async def ttl_override(self, request: Request):
        """锁定保质期（operator 工具）。锁定 ≠ 退役：expires_at 到期或
        clear 后自动回归学习值——操作员能拖时间，不能改判生死。"""
        identity = request.state.identity
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        upstream = str(payload.get("upstream", ""))
        model = str(payload.get("model", ""))
        ttl_days = payload.get("ttl_days")
        expires_at = payload.get("expires_at")
        reason = str(payload.get("reason", ""))
        if not any(u.name == upstream and model in u.models
                   for u in self.config.upstreams):
            return JSONResponse(
                {"error": "unknown target",
                 "detail": f"配置里找不到 {upstream}/{model}"}, status_code=400)
        if ttl_days is None and expires_at is None:
            return JSONResponse(
                {"error": "invalid",
                 "detail": "ttl_days 与 expires_at 至少给一个"}, status_code=400)
        try:
            ttl_days = float(ttl_days) if ttl_days is not None else None
            expires_at = float(expires_at) if expires_at is not None else None
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "invalid",
                 "detail": "ttl_days / expires_at 必须是数字（unix 时间戳）"},
                status_code=400)
        record = self.ttl.override(model, ttl_days=ttl_days,
                                   expires_at=expires_at, reason=reason)
        st = self.baselines.recompute(upstream, model)
        log.info("[ttl] %s/%s 保质期由 %s 锁定（reason=%s）",
                 upstream, model, identity.reporter_id, reason or "未注明")
        await self.publish({"type": "ttl", "upstream": upstream, "model": model,
                            "ttl_days": st.ttl_days,
                            "warning": f"保质期被 {identity.reporter_id} 手动锁定",
                            "override": record})
        return {"override": record, "baseline": st.to_dict()}

    # ---- 误报反馈（Phase 1.1） ----
    def _clear_ledger(self, key: tuple[str, str]) -> None:
        """清零账本（质量+身份）。不动熔断——复位走 breaker/reset，原因可溯。"""
        self.suspicion.pop(key, None)
        self._ledger_ts.pop(key, None)
        self.identity.pop(key, None)
        self._identity_ts.pop(key, None)
        self.state.mark_dirty()
        self.state.flush(self)

    async def baseline_feedback(self, request: Request):
        """误报反馈闭环：确认官方更新 → 退役旧基线，BASIC 模式优雅降级；
        网络抖动/误报 → 仅清零账本。全部留痕 feedback.jsonl，replay 可关联。"""
        identity = request.state.identity  # 路由级 dependency 挂上的身份
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        upstream = str(payload.get("upstream", ""))
        model = str(payload.get("model", ""))
        reason = str(payload.get("reason", "false_alarm"))
        note = str(payload.get("note", ""))
        confirm = bool(payload.get("confirm", False))
        if not any(u.name == upstream and model in u.models
                   for u in self.config.upstreams):
            return JSONResponse(
                {"error": "unknown target",
                 "detail": f"配置里找不到 {upstream}/{model}"}, status_code=400)

        record = {
            "ts": time.time(), "upstream": upstream, "model": model,
            "reason": reason, "note": note, "confirm": confirm,
            "reporter": identity.reporter_id,
            "trust_level": identity.trust_level.value,
            "action": "recorded",
        }
        key = (upstream, model)
        if confirm:
            if reason == "official_update":
                cold = self.baselines.is_cold_start(model)
                record["bootstrap"] = cold  # 冷启动样本标记（v4 Phase 0.5）
                threshold = 1 if cold else 3
                last_retired = self.baselines.get(upstream, model).retired_at
                count = self.baselines.official_update_confirms(
                    upstream, model, last_retired) + 1  # 含本次
                if Role.OPERATOR in identity.roles or count >= threshold:
                    last = self.results.get(("fingerprint", *key))
                    st = self.baselines.retire(
                        upstream, model, reason=reason,
                        reporter=identity.reporter_id,
                        last_js=last.score if last else None)
                    self._clear_ledger(key)
                    record["action"] = f"retired (level={st.level.value})"
                    log.info("[feedback] %s/%s 基线退役（%s 确认，%s）",
                             upstream, model, identity.reporter_id, reason)
                    await self.publish({"type": "baseline", "state": "retired",
                                        "upstream": upstream, "model": model,
                                        "reporter": identity.reporter_id})
                else:
                    record["action"] = f"confirm {count}/{threshold}"
            else:
                # network_jitter / false_alarm：仅清零账本，不退役
                self._clear_ledger(key)
                record["action"] = "ledger cleared"
        self.state.append_feedback(record)
        await self.publish({"type": "feedback", "upstream": upstream,
                            "model": model, "reason": reason,
                            "action": record["action"],
                            "reporter": identity.reporter_id})
        return record

    # ---- 业务测试上报（Phase 5A） ----

    async def test_report(self, request: Request):
        """业务测试上报：质量锚点入口。

        信任轴（Phase 6）门控在此叠加：TRUSTED 的 PASS 才衰减、FAIL 才可熔断；
        UNVERIFIED 一切仅记录。信号轴（滑窗/flaky/负分地板/维度隔离）由
        TestAdjudicator 与账本拆分负责——两轴都过才算数。
        """
        if not self.config.testing.enabled:
            return JSONResponse(
                {"error": "testing disabled",
                 "detail": "[testing] enabled = false，质量锚点未开启"},
                status_code=503)
        identity = request.state.identity
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        report, err = TestReport.from_payload(payload)
        if report is None:
            return JSONResponse({"error": "invalid", "detail": err},
                                status_code=400)
        if not any(u.name == report.upstream
                   and report.model_claimed in u.models
                   for u in self.config.upstreams):
            return JSONResponse(
                {"error": "unknown target",
                 "detail": f"配置里找不到 {report.upstream}/{report.model_claimed}"},
                status_code=400)

        decision = self.tests.decide(report)
        if not decision.accepted:
            return {"accepted": False, "action": decision.action,
                    "detail": decision.detail}
        # ---- 信任轴门控 ----
        if (decision.decay_amount > 0
                and identity.trust_level is not TrustLevel.TRUSTED):
            decision.action = "recorded"
            decision.decay_amount = 0.0
            decision.detail = "非 TRUSTED 凭据的 PASS 不衰减嫌疑分（信任轴）"
        if (decision.score_delta > 0
                and identity.trust_level is TrustLevel.UNVERIFIED):
            decision.action = "recorded"
            decision.score_delta = 0.0
            decision.detail = "UNVERIFIED 凭据的 FAIL 仅记录（信任轴）"
        if identity.trust_level is not TrustLevel.TRUSTED:
            decision.trigger_breaker = False
        # ---- 落账本/熔断 ----
        key = (report.upstream, report.model_claimed)
        floor = self.config.testing.score_floor
        if decision.score_delta > 0:
            # "DEGRADED 级别嫌疑分"（v4 5A）：**仅 TRUSTED 凭据**享受
            # "分值保底、跨线优先"——触发即够跨过熔断线，跳过 WATCH 缓冲期。
            # NORMAL 只加等级分值（15/10/8），能否熔断续由通用账本阈值决定；
            # UNVERIFIED 在上面已被降级为仅记录
            topped = identity.trust_level is TrustLevel.TRUSTED
            if topped:
                injected = max(decision.score_delta,
                               math.ceil(self.config.rules.suspicion_threshold
                                         - self._decayed(key)))
            else:
                injected = decision.score_delta
            decision.score_delta = injected
            await self._add_suspicion(report.upstream, report.model_claimed, [{
                "rule": "test-fail", "score": injected,
                "field": "test", "snippet": report.failure_diff[:200],
                "description": f"业务测试连续失败（{report.level} {report.test_name}）",
            }])
        elif decision.decay_amount > 0:
            new_q = self._decay_quality(key, decision.decay_amount, floor)
            decision.detail += (f" → 质量类余 {new_q:.1f}"
                                f"（总分地板 {floor}，身份类不动）")
        if decision.trigger_breaker and self.config.breaker.enabled:
            self.breakers[key] = time.time()
            log.warning("[breaker] %s/%s 测试连续 FAIL 触发熔断", *key)
            await self.publish({"type": "breaker", "state": "open",
                                "upstream": key[0], "model": key[1],
                                "reason": "test-fail"})
        await self.publish({"type": "test", "upstream": key[0], "model": key[1],
                            "suite": report.test_suite, "name": report.test_name,
                            "level": report.level, "status": report.status,
                            "action": decision.action,
                            "reporter": identity.reporter_id})
        return {"accepted": True, "action": decision.action,
                "detail": decision.detail,
                "score_delta": decision.score_delta,
                "decay_amount": decision.decay_amount,
                "trigger_breaker": decision.trigger_breaker}

    # ---- Admin：凭据签发/查询/吊销（Phase 6C） ----

    async def identity_cleared(self, request: Request):
        """operator 裁决：身份类嫌疑解冻（v4 Phase 5A 两条路之一）。

        重新采集基线之外的另一条路。只清身份账本不动质量类——
        身份维度不吃"测试作证"那一套，只认"重新采样"或"长官裁决"。
        """
        identity = request.state.identity
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        upstream = str(payload.get("upstream", ""))
        model = str(payload.get("model", ""))
        reason = str(payload.get("reason", ""))
        if not any(u.name == upstream and model in u.models
                   for u in self.config.upstreams):
            return JSONResponse(
                {"error": "unknown target",
                 "detail": f"配置里找不到 {upstream}/{model}"}, status_code=400)
        key = (upstream, model)
        had = self.identity.pop(key, None)
        self._identity_ts.pop(key, None)
        if had is None:
            return {"cleared": False, "upstream": upstream, "model": model}
        self.state.mark_dirty()
        self.state.flush(self)
        log.info("[baseline] %s/%s 身份类嫌疑由 %s 裁决解冻（was %.1f, reason=%s）",
                 upstream, model, identity.reporter_id, had, reason or "未注明")
        await self.publish({"type": "baseline", "state": "identity-cleared",
                            "upstream": upstream, "model": model,
                            "reporter": identity.reporter_id,
                            "cleared": had})
        return {"cleared": True, "upstream": upstream, "model": model,
                "previous": had}

    async def admin_issue_credential(self, request: Request):
        """签发凭据：201 + 明文 secret（仅此一次，丢失只能吊销重签）。"""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        roles = payload.get("roles", ["reporter"])
        if isinstance(roles, str):
            roles = [roles]
        try:
            meta, secret = self.security.issue_for_request(
                str(payload.get("reporter_id", "")),
                str(payload.get("trust_level", "normal")),
                list(roles),
                method=payload.get("method"),
                public_key=str(payload.get("public_key", "")),
            )
        except ValueError as exc:
            return JSONResponse({"error": "invalid", "detail": str(exc)},
                                status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": "conflict", "detail": exc.args[0]},
                                status_code=409)
        log.info("[security] 签发凭据 %s（trust=%s, roles=%s, method=%s）",
                 meta["reporter_id"], meta["trust_level"], meta["roles"],
                 meta["method"])
        return JSONResponse(
            {**meta, "secret": secret}, status_code=201,
            # HTTP 头只吃 latin-1，提示语保持 ASCII；
            # Cache-Control: no-store——明文凭据不许被任何中间层缓存（v4 6F）
            headers={"X-Satori-Credential-Notice":
                     "plaintext secret shown once - store it now",
                     "Cache-Control": "no-store"},
        )

    async def admin_list_credentials(self, status: str | None = None,
                                     trust_level: str | None = None):
        creds = self.security.store.list(status=status or None)
        if trust_level:
            creds = [c for c in creds if c.trust_level.value == trust_level]
        return {"credentials": [c.to_meta() for c in creds]}

    async def admin_get_credential(self, reporter_id: str):
        cred = self.security.store.get(reporter_id)
        if cred is None:
            return JSONResponse(
                {"error": "not found", "detail": f"无凭据 {reporter_id!r}"},
                status_code=404,
            )
        return cred.to_meta()

    async def admin_revoke_credential(self, reporter_id: str):
        if not self.security.store.revoke(reporter_id):
            return JSONResponse(
                {"error": "not found", "detail": f"无有效凭据 {reporter_id!r}"},
                status_code=404,
            )
        log.info("[security] 吊销凭据 %s", reporter_id)
        return Response(status_code=204)

    async def status(self):
        return {
            "security": {
                "enabled": self.security.enabled,
                "mode": self.security.mode,
            },
            "baselines": [
                {**st.to_dict(),
                 "cold_start": self.baselines.is_cold_start(st.model),
                 "ttl": (v.to_dict() if (v := self.ttl.verdict(
                     st.model, st.collected_at)) is not None else None),
                 "ttl_override": self.ttl.override_of(st.model)}
                for st in self.baselines.states.values()
            ],
            "tests": self.tests.summary(),
            "slop": self.slop.summary(),
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
                 "quality": round(self._decayed_quality((up, m)), 1),
                 "identity": round(self._decayed_identity((up, m)), 1),
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
