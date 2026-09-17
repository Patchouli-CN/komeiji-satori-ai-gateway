"""流量录制与回放取证。

录制格式（JSONL，按天一个文件）：
  {"ts": ..., "upstream": ..., "model": ..., "status": 200,
   "request_text": "...", "content": "...", "reasoning": "..."}

回放：用当前生效的规则集重新审历史流量，输出可疑度报告。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .rules import RuleEngine, RuleHit
from .tokenwatch import TokenizerWatch


def make_entry(
    upstream: str, model: str, status: int,
    request_text: str, content: str, reasoning: str,
    usage: dict | None = None,
) -> dict:
    return {
        "ts": time.time(),
        "upstream": upstream,
        "model": model,
        "status": status,
        "request_text": request_text,
        "content": content,
        "reasoning": reasoning,
        "usage": usage or {},
    }


def append_record(directory: Path, entry: dict, max_field_chars: int = 64_000) -> None:
    """追加一条录制。超长字段截断，防止单条记录撑爆磁盘。"""
    directory.mkdir(parents=True, exist_ok=True)
    for key in ("request_text", "content", "reasoning"):
        if len(entry.get(key, "")) > max_field_chars:
            entry[key] = entry[key][:max_field_chars] + "…[truncated]"
    day = time.strftime("%Y-%m-%d", time.localtime(entry["ts"]))
    with open(directory / f"{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def iter_records(path: str | Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


@dataclass
class ReplayReport:
    total: int = 0
    flagged: int = 0
    suspicion: dict[tuple[str, str], int] = field(default_factory=dict)
    hits: list[tuple[dict, list[RuleHit]]] = field(default_factory=list)
    tokenwatch_alerts: list[str] = field(default_factory=list)


def replay(path: str | Path, engine: RuleEngine) -> ReplayReport:
    report = ReplayReport()
    tw = TokenizerWatch()
    for entry in iter_records(path):
        report.total += 1
        upstream = entry.get("upstream", "?")
        model = entry.get("model", "?")
        hits = engine.evaluate(
            entry.get("content", ""), entry.get("reasoning", ""),
            entry.get("request_text", ""),
        )
        tw_alert = tw.observe(
            upstream, model, len(entry.get("request_text", "")),
            entry.get("usage", {}).get("prompt_tokens", 0),
        )
        if tw_alert:
            report.tokenwatch_alerts.append(f"[{upstream}/{model}] {tw_alert}")
        if not hits:
            continue
        report.flagged += 1
        report.hits.append((entry, hits))
        key = (upstream, model)
        gained = sum(h.score for h in hits)
        report.suspicion[key] = max(0, report.suspicion.get(key, 0) + gained)
    return report


def ingest_markdown(
    path: str | Path, upstream: str = "import", model: str = "transcript"
) -> Iterator[dict]:
    """把 markdown 会话记录转成录制条目。

    启发式：`> ` 引用块视为助手输出，其余非空、非 HTML 标签行视为用户输入；
    按出现顺序把“用户块 + 紧随的助手块”配成一对。
    """
    user_block: list[str] = []
    asst_block: list[str] = []

    def flush() -> dict | None:
        nonlocal user_block, asst_block
        if not user_block or not asst_block:
            user_block, asst_block = [], []
            return None
        entry = make_entry(
            upstream, model, 200,
            "\n".join(user_block), "\n".join(asst_block), "",
        )
        user_block, asst_block = [], []
        return entry

    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("<"):
            continue
        if line.lstrip().startswith(">"):
            asst_block.append(line.lstrip()[1:].lstrip())
        else:
            if asst_block:
                # 助手块结束后出现新的用户输入：上一对落盘
                entry = flush()
                if entry:
                    yield entry
            user_block.append(line.strip())
    entry = flush()
    if entry:
        yield entry
