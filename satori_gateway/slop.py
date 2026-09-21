"""Tool Call 链级审计（v4 Phase 4）：change_trace + 结构化 Slop 评分。

Tool Call 场景下，"模型被降级"的危害不再是"回答变笨"，而是"工具调用参数错了、
逻辑链断了"。这种 Slop 比直接拒绝更危险，因为它会**静默污染下游系统**。
觉不仅要读出"味道变了"，还要**沿着思维脉络逆流而上，找到第一个被污染的念头**。

**诚实的边界**：这里的 slop_score 是**结构化启发式**（断链/幻觉工具/复读/膨胀），
不是语义判定——语义需要回放基线（Phase 4 后续）。但它 Cheap、确定、可复现，
且恰好覆盖"降级模型污染下游"的最典型形态：参数 JSON 断裂。

三阶段流程：怀疑（单步 score>0）→ 实锤（同 session 累计 N 步）→
回溯（第一个突变步高亮）。confirmed_slop 注入 DEGRADED 级**质量类**嫌疑
（可被 L0/L1 PASS 部分衰减，但洗不穿负分地板——Phase 5A 的账）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

# 结构化异常权重（保守：单项不足以独立熔断，持续异常才累积）
SLOP_BROKEN_ARGS = 30.0     # 参数不是合法 JSON——断链的最典型形态
SLOP_UNKNOWN_TOOL = 20.0    # 调了请求里没声明的工具（幻觉）
SLOP_REPEAT = 15.0          # 同一响应内重复 identical 调用（复读机）
SLOP_ARGS_BLOAT = 10.0      # 参数膨胀 > 8KB
SLOP_ARGS_BLOAT_BYTES = 8192
CONFIRM_STEPS = 2           # 同 session 累计几步 suspicious 算实锤
CONFIRMED_SCORE = 40.0      # 实锤注入分（权重高于 Logprob 的 20；仍受跨线规则钳制）


@dataclass
class ToolStep:
    index: int
    tool: str
    arguments: str
    args_valid: bool
    args_bytes: int
    flags: list[str] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"index": self.index, "tool": self.tool,
                "args_valid": self.args_valid, "args_bytes": self.args_bytes,
                "flags": self.flags, "score": self.score}


@dataclass
class ToolTrace:
    trace_id: str
    ts: float
    steps: list[ToolStep]
    slop_score: float
    first_suspicious: int     # 第一个 score>0 的步；-1 表示干净

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id, "ts": self.ts,
            "steps": [s.to_dict() for s in self.steps],
            "slop_score": self.slop_score,
            "first_suspicious": self.first_suspicious,
        }


def _declared_names(declared_tools) -> set[str]:
    """兼容两种 tools 形状：规范（function.name）与 Anthropic 原生（name）。"""
    names: set[str] = set()
    for t in declared_tools or []:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            names.add(fn.get("name") or t.get("name") or "")
    names.discard("")
    return names


def score_tool_calls(tool_calls: list[dict],
                     declared_tools=None,
                     trace_id: str | None = None) -> ToolTrace | None:
    """给一次响应里的 tool_calls 链打分。无工具调用返回 None。"""
    if not tool_calls:
        return None
    names = _declared_names(declared_tools)
    steps: list[ToolStep] = []
    seen: dict[tuple[str, str], int] = {}
    total = 0.0
    for i, tc in enumerate(tool_calls):
        fn = tc.get("function") or {}
        name = fn.get("name", "") or ""
        args = fn.get("arguments", "") or ""
        flags: list[str] = []
        score = 0.0
        # 空参数视为合法（无参工具）；非空但解析失败才是断链
        valid = True
        if args:
            import json
            try:
                json.loads(args)
            except json.JSONDecodeError:
                valid = False
                flags.append("broken-args")
                score += SLOP_BROKEN_ARGS
        if names and name not in names:
            flags.append("unknown-tool")
            score += SLOP_UNKNOWN_TOOL
        key = (name, args)
        if key in seen:
            flags.append(f"repeat-of-step-{seen[key]}")
            score += SLOP_REPEAT
        else:
            seen[key] = i
        if len(args) > SLOP_ARGS_BLOAT_BYTES:
            flags.append("args-bloat")
            score += SLOP_ARGS_BLOAT
        steps.append(ToolStep(i, name, args, valid, len(args), flags, score))
        total += score
    first = next((s.index for s in steps if s.score > 0), -1)
    return ToolTrace(trace_id or uuid.uuid4().hex[:16], time.time(),
                     steps, total, first)


class SlopLedger:
    """session 级累计：怀疑 → 实锤 → 回溯。实锤后该 session 重新武装。"""

    def __init__(self, confirm_steps: int = CONFIRM_STEPS) -> None:
        self.confirm_steps = confirm_steps
        self._sessions: dict[str, list[dict]] = {}

    def observe(self, session: str, trace: ToolTrace) -> dict | None:
        """记一笔可疑 trace。累计到阈值返回实锤报告（含回溯原点），否则 None。"""
        if trace.slop_score <= 0:
            return None
        steps = self._sessions.setdefault(session, [])
        steps.append({"trace_id": trace.trace_id,
                      "first_suspicious": trace.first_suspicious,
                      "score": trace.slop_score,
                      "flags": [f for s in trace.steps for f in s.flags]})
        if len(steps) < self.confirm_steps:
            return None
        confirmed = {
            "session": session,
            "steps": list(steps),
            "origin_trace": steps[0]["trace_id"],
            "origin_step": steps[0]["first_suspicious"],
            "total_score": sum(s["score"] for s in steps),
        }
        self._sessions[session] = []  # 实锤后重新武装，避免刷屏
        return confirmed

    def pending(self, session: str) -> int:
        return len(self._sessions.get(session, []))

    def summary(self) -> list[dict]:
        return [{"session": s, "suspicious_steps": len(v)}
                for s, v in self._sessions.items() if v]
