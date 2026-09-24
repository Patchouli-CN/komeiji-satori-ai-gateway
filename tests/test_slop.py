"""Wave 3 / Phase 4 测试：adapter v2 工具调用翻译 + Slop 链级审计。

两侧翻译各自单测（客户端 Anthropic 适配器 / 上游 Anthropic 管线），
再加规范响应里 tool_calls 的组装与 session 级实锤注入。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from satori_gateway.adapters.anthropic import AnthropicAdapter
from satori_gateway.app import _extract_tool_calls
from satori_gateway.config import Upstream
from satori_gateway.pipelines.anthropic import AnthropicConverter
from satori_gateway.pipelines import build_pipeline
from satori_gateway.slop import (
    CONFIRMED_SCORE,
    SLOP_ARGS_BLOAT,
    SLOP_BROKEN_ARGS,
    SLOP_REPEAT,
    SLOP_UNKNOWN_TOOL,
    SlopLedger,
    score_tool_calls,
)

from test_phase1 import KEY, env  # noqa: F401  （pytest fixture 注入用）


# ---- 结构化 Slop 评分 ----


class TestSlopScoring:
    DECLARED = [
        {
            "type": "function",
            "function": {"name": "get_weather", "parameters": {"type": "object"}},
        }
    ]

    def _calls(self, *args_pairs):
        return [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": n, "arguments": a},
            }
            for i, (n, a) in enumerate(args_pairs)
        ]

    def test_clean_calls_no_trace(self):
        trace = score_tool_calls(
            self._calls(("get_weather", '{"city":"SF"}')), self.DECLARED
        )
        assert trace is not None
        assert trace.slop_score == 0.0 and trace.first_suspicious == -1

    def test_no_tool_calls_returns_none(self):
        assert score_tool_calls([], self.DECLARED) is None

    def test_broken_args_scores_and_marks_step(self):
        trace = score_tool_calls(
            self._calls(
                ("get_weather", '{"city":'),  # 断裂的 JSON
            ),
            self.DECLARED,
        )
        assert trace.slop_score == SLOP_BROKEN_ARGS
        assert trace.first_suspicious == 0
        assert trace.steps[0].args_valid is False
        assert "broken-args" in trace.steps[0].flags

    def test_unknown_tool_scores(self):
        trace = score_tool_calls(self._calls(("hack_the_planet", "{}")), self.DECLARED)
        assert trace.slop_score == SLOP_UNKNOWN_TOOL
        assert "unknown-tool" in trace.steps[0].flags

    def test_repeat_scores_on_second_occurrence(self):
        trace = score_tool_calls(
            self._calls(
                ("get_weather", '{"city":"SF"}'), ("get_weather", '{"city":"SF"}')
            ),
            self.DECLARED,
        )
        assert trace.slop_score == SLOP_REPEAT
        assert "repeat-of-step-0" in trace.steps[1].flags

    def test_bloat_scores(self):
        trace = score_tool_calls(
            self._calls(("get_weather", '{"x":"' + "a" * 9000 + '"}')), self.DECLARED
        )
        assert trace.slop_score == SLOP_ARGS_BLOAT

    def test_flags_stack(self):
        trace = score_tool_calls(
            self._calls(("hack_the_planet", '{"broken":')), self.DECLARED
        )
        assert trace.slop_score == SLOP_UNKNOWN_TOOL + SLOP_BROKEN_ARGS

    def test_native_declared_shape_supported(self):
        """Anthropic 原生 tools 形状（name/input_schema）同样认得。"""
        trace = score_tool_calls(
            [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
            [{"name": "get_weather", "input_schema": {}}],
        )
        assert trace.slop_score == 0.0

    def test_no_declared_tools_skips_unknown_check(self):
        """没带 tools 的请求不判幻觉——无从判起。"""
        trace = score_tool_calls(self._calls(("anything", "{}")), declared_tools=None)
        assert trace.slop_score == 0.0


class TestSlopLedger:
    def _trace(self, tid="t1"):
        return score_tool_calls(
            [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"broken":'},
                }
            ],
            [{"name": "f"}],
            trace_id=tid,
        )

    def test_confirm_after_threshold_steps(self):
        ledger = SlopLedger(confirm_steps=2)
        assert ledger.observe("s1", self._trace("a")) is None
        assert ledger.pending("s1") == 1
        confirmed = ledger.observe("s1", self._trace("b"))
        assert confirmed is not None
        assert confirmed["origin_trace"] == "a"  # 回溯到第一个突变步
        assert confirmed["origin_step"] == 0
        assert ledger.pending("s1") == 0  # 实锤后重新武装

    def test_sessions_are_isolated(self):
        ledger = SlopLedger(confirm_steps=2)
        ledger.observe("s1", self._trace("a"))
        assert ledger.observe("s2", self._trace("b")) is None

    def test_sessions_bounded_oldest_evicted(self):
        """未实锤 session 槽位有上限：超限按最久不活动淘汰，不无界增长。"""
        ledger = SlopLedger(confirm_steps=2, max_sessions=3)
        for i in range(3):
            ledger.observe(f"s{i}", self._trace(f"t{i}"))
        ledger.observe("s3", self._trace("t3"))  # 超限 → 淘汰最旧的 s0
        assert ledger.pending("s0") == 0
        assert ledger.pending("s1") == 1
        assert ledger.pending("s3") == 1

    def test_active_session_survives_eviction(self):
        """活跃的 session 排到队尾：淘汰先动久未活动的。"""
        ledger = SlopLedger(confirm_steps=3, max_sessions=2)
        ledger.observe("s1", self._trace("a"))
        ledger.observe("s2", self._trace("b"))
        ledger.observe("s1", self._trace("c"))  # s1 活跃 → 移尾
        ledger.observe("s3", self._trace("d"))  # 淘汰的是 s2 不是 s1
        assert ledger.pending("s2") == 0
        assert ledger.pending("s1") == 2

    def test_clean_trace_never_confirms(self):
        ledger = SlopLedger()
        clean = score_tool_calls(
            [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ],
            [{"name": "f"}],
        )
        assert ledger.observe("s1", clean) is None
        assert ledger.summary() == []


# ---- 客户端 Anthropic 适配器（adapter v2） ----


class TestAnthropicClientAdapter:
    @pytest.fixture(autouse=True)
    def _adapter(self):
        self.ad = AnthropicAdapter()

    def test_tools_translation(self):
        body = {
            "model": "claude-x",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "查天气",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
        }
        canonical = self.ad.to_canonical(body)
        assert canonical["tools"][0]["type"] == "function"
        fn = canonical["tools"][0]["function"]
        assert fn["name"] == "get_weather"
        assert fn["parameters"]["properties"]["city"]["type"] == "string"

    def test_assistant_tool_use_to_tool_calls(self):
        body = {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "天气?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "查一下"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "SF"},
                        },
                    ],
                },
            ],
        }
        messages = self.ad.to_canonical(body)["messages"]
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "查一下"
        tc = messages[1]["tool_calls"][0]
        assert tc["id"] == "toolu_1" and tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "SF"}

    def test_tool_result_to_tool_message(self):
        body = {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "晴，20度",
                        },
                    ],
                },
            ],
        }
        messages = self.ad.to_canonical(body)["messages"]
        assert messages[0] == {
            "role": "tool",
            "tool_call_id": "toolu_1",
            "content": "晴，20度",
        }

    def test_tool_choice_translation(self):
        body = {
            "model": "m",
            "max_tokens": 10,
            "messages": [],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
        assert self.ad.to_canonical(body)["tool_choice"] == {
            "type": "function",
            "function": {"name": "get_weather"},
        }
        body["tool_choice"] = {"type": "auto"}
        assert self.ad.to_canonical(body)["tool_choice"] == {"type": "auto"}

    def test_from_canonical_tool_calls_to_blocks(self):
        payload = {
            "id": "x",
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"SF"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {},
        }
        out = self.ad.from_canonical(payload)
        assert out["stop_reason"] == "tool_use"
        blocks = out["content"]
        assert blocks[0]["type"] == "text"
        assert blocks[1] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "SF"},
        }

    def test_from_canonical_broken_arguments_preserved(self):
        """畸形参数照样转运——断链现场留给检测核心。"""
        payload = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c",
                                "type": "function",
                                "function": {"name": "f", "arguments": '{"broken":'},
                            }
                        ],
                    },
                }
            ],
        }
        out = self.ad.from_canonical(payload)
        assert out["content"][1]["input"] == {"_unparseable": '{"broken":'}

    def test_streaming_tool_events(self):
        state: dict = {}
        events: list[dict] = []
        # 首块：role + 工具调用开头（带 id/name）
        chunk1 = {
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ],
                    }
                }
            ]
        }
        events += self.ad.translate_sse(chunk1, state)
        # 参数增量
        chunk2 = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"city":'}}
                        ]
                    }
                }
            ]
        }
        events += self.ad.translate_sse(chunk2, state)
        chunk3 = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '"SF"}'}}]
                    }
                }
            ]
        }
        events += self.ad.translate_sse(chunk3, state)
        # 收尾
        events += self.ad.translate_sse(
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"completion_tokens": 5},
            },
            state,
        )

        names = [e["event"] for e in events]
        assert "content_block_start" in names  # 文本块
        tool_starts = [
            e
            for e in events
            if e["event"] == "content_block_start"
            and e["data"]["content_block"]["type"] == "tool_use"
        ]
        assert len(tool_starts) == 1
        assert tool_starts[0]["data"]["content_block"]["id"] == "call_1"
        assert tool_starts[0]["data"]["content_block"]["name"] == "get_weather"
        deltas = [
            e
            for e in events
            if e["event"] == "content_block_delta"
            and e["data"]["delta"]["type"] == "input_json_delta"
        ]
        assert [d["data"]["delta"]["partial_json"] for d in deltas] == [
            '{"city":',
            '"SF"}',
        ]
        # 工具块 index 从 1 开始（0 是文本块），且文本块被合上
        assert tool_starts[0]["data"]["index"] == 1
        stops = [e for e in events if e["event"] == "content_block_stop"]
        assert {s["data"]["index"] for s in stops} == {0, 1}
        assert events[-1]["event"] == "message_stop"
        md = [e for e in events if e["event"] == "message_delta"][0]
        assert md["data"]["delta"]["stop_reason"] == "tool_use"

    def test_text_only_stream_unchanged(self):
        """v1 行为回归：纯文本流的字符级翻译不受 v2 影响。"""
        state: dict = {}
        events = self.ad.translate_sse(
            {"choices": [{"delta": {"content": "你好"}}]}, state
        )
        # 首块 Always 带 message_start + content_block_start(text)，随后才是字符增量
        assert [e["event"] for e in events] == [
            "message_start",
            "content_block_start",
            "content_block_delta",
        ]
        assert events[-1]["data"]["delta"]["text"] == "你好"


# ---- 上游 Anthropic 管线（adapter v2） ----


class TestAnthropicUpstreamPipeline:
    @pytest.fixture(autouse=True)
    def _conv(self):
        self.conv = AnthropicConverter()

    def test_request_tools_and_tool_messages(self):
        canonical = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "天气?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"SF"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "晴，20度"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "查天气",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
        native = self.conv.request_to_native(canonical)
        assert native["tools"][0]["name"] == "get_weather"
        assert native["tools"][0]["input_schema"] == {"type": "object"}
        assert native["tool_choice"] == {"type": "tool", "name": "get_weather"}
        assistant_msg = native["messages"][1]
        assert assistant_msg["role"] == "assistant"
        tool_use = [b for b in assistant_msg["content"] if b["type"] == "tool_use"][0]
        assert tool_use["name"] == "get_weather" and tool_use["id"] == "call_1"
        assert tool_use["input"] == {"city": "SF"}
        tool_result_msg = native["messages"][2]
        assert tool_result_msg["role"] == "user"
        block = tool_result_msg["content"][0]
        assert block["type"] == "tool_result" and block["tool_use_id"] == "call_1"
        assert block["content"] == "晴，20度"

    def test_response_tool_use_to_tool_calls(self):
        native = {
            "id": "msg_1",
            "model": "m",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "查一下"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "SF"},
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        canonical = self.conv.response_to_canonical(native)
        msg = canonical["choices"][0]["message"]
        assert canonical["choices"][0]["finish_reason"] == "tool_calls"
        tc = msg["tool_calls"][0]
        assert tc["id"] == "toolu_1" and tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "SF"}

    def test_streaming_tool_use_assembles_canonical(self):
        state = self.conv.new_stream_state()
        chunks: list[dict] = []
        chunks += self.conv.translate_sse_data(
            {"type": "message_start", "message": {"id": "m1", "model": "m"}}, state
        )
        chunks += self.conv.translate_sse_data(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {},
                },
            },
            state,
        )
        chunks += self.conv.translate_sse_data(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
            },
            state,
        )
        chunks += self.conv.translate_sse_data(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"SF"}'},
            },
            state,
        )
        chunks += self.conv.translate_sse_data(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 5},
            },
            state,
        )

        # 规范侧看到的 tool_calls：index 0、id/name 首片、arguments 分片拼接
        tool_deltas = [
            c["choices"][0]["delta"]["tool_calls"][0]
            for c in chunks
            if c["choices"][0]["delta"].get("tool_calls")
        ]
        assert tool_deltas[0]["index"] == 0
        assert tool_deltas[0]["id"] == "toolu_1"
        assert tool_deltas[0]["function"]["name"] == "get_weather"
        args = "".join(d["function"].get("arguments", "") for d in tool_deltas)
        assert args == '{"city":"SF"}'
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_end_turn_with_tool_calls_forces_finish(self):
        native = {
            "content": [{"type": "tool_use", "id": "t", "name": "f", "input": {}}],
            "stop_reason": "end_turn",  # Anthropic 偶发收尾
        }
        canonical = self.conv.response_to_canonical(native)
        assert canonical["choices"][0]["finish_reason"] == "tool_calls"

    def test_roundtrip_through_pipeline(self):
        """规范请求 → Anthropic 原生请求（走完整管线装配）。"""
        upstream = Upstream(
            name="a",
            base_url="http://x/v1",
            api_key="k",
            protocol="anthropic",
            models=["m"],
        )
        pipe = build_pipeline(upstream)
        prepared = pipe.build_request(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "f", "parameters": {"type": "object"}},
                    }
                ],
            }
        )
        native = json.loads(prepared.body)
        assert native["tools"][0]["name"] == "f"
        assert prepared.url == "http://x/v1/messages"


# ---- 规范响应 tool_calls 组装 + app 接线 ----


class TestToolCallExtraction:
    def test_from_non_stream_json(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "f", "arguments": '{"a":1}'},
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        calls = _extract_tool_calls(body, "application/json")
        assert calls[0]["function"]["name"] == "f"
        assert calls[0]["function"]["arguments"] == '{"a":1}'

    def test_from_stream_assembles_fragments(self):
        lines = [
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "f", "arguments": ""},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '{"a":'}}
                                ]
                            }
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": "1}"}}
                                ]
                            }
                        }
                    ]
                }
            ),
        ]
        body = "".join(f"data: {line}\n\n" for line in lines).encode()
        calls = _extract_tool_calls(body, "text/event-stream")
        assert calls[0]["function"]["arguments"] == '{"a":1}'

    def test_no_tool_calls_empty(self):
        body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
        assert _extract_tool_calls(body, "application/json") == []


class TestObserveToolsWiring:
    """_observe_tools：怀疑留痕 → session 累计 → 实锤注入质量账本。"""

    def _fwd(self, tools=True):
        if not tools:
            return json.dumps({"messages": []}).encode()
        return json.dumps(
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "parameters": {}},
                    }
                ]
            }
        ).encode()

    def _broken_calls(self):
        return [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"broken":'},
            }
        ]

    def test_clean_trace_records_but_never_scores(self, env):
        """干净的链也留痕（回放取证要翻旧账），但不动账本。"""
        satori, _ = env
        asyncio.run(
            satori._observe_tools(
                "openai",
                "gpt-4o",
                self._fwd(),
                [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"SF"}',
                        },
                    }
                ],
                "s1",
            )
        )
        traces = satori.state.read_tool_traces()
        assert len(traces) == 1 and traces[0]["slop_score"] == 0.0
        assert satori._decayed(KEY) == 0.0

    def test_suspicious_then_confirmed_injects_quality(self, env):
        satori, _ = env
        # 第一次：怀疑（留痕 + 广播，不入账）
        asyncio.run(
            satori._observe_tools(
                "openai", "gpt-4o", self._fwd(), self._broken_calls(), "s1"
            )
        )
        traces = satori.state.read_tool_traces()
        assert len(traces) == 1 and traces[0]["slop_score"] == SLOP_BROKEN_ARGS
        assert satori._decayed(KEY) == 0.0  # 一步只是怀疑
        # 第二次：实锤 → 注入 DEGRADED 级**质量类**嫌疑
        asyncio.run(
            satori._observe_tools(
                "openai", "gpt-4o", self._fwd(), self._broken_calls(), "s1"
            )
        )
        assert satori._decayed_quality(KEY) == pytest.approx(CONFIRMED_SCORE, abs=0.01)
        assert satori._decayed_identity(KEY) == 0.0
        assert satori.slop.pending("s1") == 0  # 重新武装

    def test_session_isolated(self, env):
        satori, _ = env
        for i in range(3):
            asyncio.run(
                satori._observe_tools(
                    "openai",
                    "gpt-4o",
                    self._fwd(),
                    self._broken_calls(),
                    f"session-{i}",
                )
            )  # 每个会话只有一步可疑
        assert satori._decayed(KEY) == 0.0  # 永不实锤

    def test_no_session_header_falls_back_to_upstream_model(self, env):
        """S3 回归：客户端不发 X-Satori-Session 时按 上游×模型 归并——
        两步可疑即实锤（旧代码拿每请求新 uuid 的 trace_id 当 session 键，
        实锤路径永远凑不满 CONFIRM_STEPS）。"""
        satori, _ = env
        asyncio.run(
            satori._observe_tools(
                "openai", "gpt-4o", self._fwd(), self._broken_calls(), ""
            )
        )
        assert satori._decayed(KEY) == 0.0  # 一步只是怀疑
        asyncio.run(
            satori._observe_tools(
                "openai", "gpt-4o", self._fwd(), self._broken_calls(), ""
            )
        )
        assert satori._decayed_quality(KEY) == pytest.approx(CONFIRMED_SCORE, abs=0.01)
        assert satori.slop.pending("openai/gpt-4o") == 0  # 实锤后重新武装

    def test_no_declared_tools_still_scores_broken_args(self, env):
        satori, _ = env
        asyncio.run(
            satori._observe_tools(
                "openai", "gpt-4o", self._fwd(tools=False), self._broken_calls(), "s1"
            )
        )
        assert satori.state.read_tool_traces()[0]["slop_score"] == SLOP_BROKEN_ARGS
