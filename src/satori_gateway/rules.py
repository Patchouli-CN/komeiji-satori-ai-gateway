"""自定义规则引擎：用户可写的可疑模式规则，命中累加可疑度。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str
    field: str = "any"  # content | reasoning | any
    match: str = "contains"  # contains | regex
    score: int = 10
    description: str = ""
    target: str = "response"  # response（默认）| request（请求侧，通常配负分做豁免）


@dataclass(frozen=True)
class RuleHit:
    rule: str
    score: int
    field: str
    snippet: str
    description: str


def load_rules(path: str | Path) -> list[Rule]:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return [Rule(**r) for r in raw.get("rules", [])]


def load_builtin_rules() -> list[Rule]:
    """加载随包发布的内置规则（调研来源见文件头注释）。"""
    return load_rules(Path(__file__).with_name("builtin_rules.toml"))


def merge_rules(builtin: list[Rule], user: list[Rule]) -> list[Rule]:
    """合并内置与用户规则：同名时用户规则覆盖内置。"""
    merged = {r.name: r for r in builtin}
    merged.update({r.name: r for r in user})
    return list(merged.values())


class RuleEngine:
    def __init__(self, rules: list[Rule]) -> None:
        self._compiled: list[tuple[Rule, re.Pattern[str] | None]] = [
            (r, re.compile(r.pattern) if r.match == "regex" else None) for r in rules
        ]

    def _hit_field(self, rule: Rule) -> list[str]:
        """规则声明的作用域展开成实际字段列表。"""
        if rule.field == "any":
            return ["content", "reasoning"]
        return [rule.field]

    def evaluate(self, content: str, reasoning: str, request: str = "") -> list[RuleHit]:
        """对一次请求-响应跑全部规则。

        target=response 的规则看响应的正文/CoT；target=request 的规则看用户
        请求原文（典型用法是负分豁免：用户主动要求假扮时，身份自报不加分）。
        """
        texts = {"content": content, "reasoning": reasoning}
        hits: list[RuleHit] = []
        for rule, regex in self._compiled:
            if rule.target == "request":
                hit = self._match(rule, regex, "request", request)
                if hit:
                    hits.append(hit)
                continue
            for field in self._hit_field(rule):
                hit = self._match(rule, regex, field, texts[field])
                if hit:
                    hits.append(hit)
                    break  # 同一规则一次响应只记一次
        return hits

    @staticmethod
    def _match(
        rule: Rule, regex: re.Pattern[str] | None, field: str, text: str
    ) -> RuleHit | None:
        if not text:
            return None
        if regex is not None:
            m = regex.search(text)
            if not m:
                return None
            snippet = text[max(0, m.start() - 20):m.end() + 40]
        else:
            idx = text.find(rule.pattern)
            if idx < 0:
                return None
            snippet = text[max(0, idx - 20):idx + len(rule.pattern) + 40]
        return RuleHit(rule.name, rule.score, field, snippet.strip(), rule.description)
