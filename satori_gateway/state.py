"""审计状态持久化（v4 Phase 1.0）：重启不失忆。

state/ 目录的三样家当：
    ledger.json            账本/熔断/衰减时间戳的快照（防抖合并 + 原子替换）
    feedback.jsonl         误报反馈流水（append-only，replay 可关联翻旧账）
    baseline_events.jsonl  基线事件流水（Phase 2 动态 TTL 的学习数据源）

写入策略：变更打 dirty 标记，1s 防抖合并落盘；重要状态迁移（反馈/退役/复位）
与关闭时强制 flush。审计状态是时间函数——持久化是默认而非特性。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("satori")


def _key(upstream: str, model: str) -> str:
    return f"{upstream}|{model}"


def _split(key: str) -> tuple[str, str]:
    up, _, model = key.partition("|")
    return up, model


class StateStore:
    """state/ 目录的唯一写者。ledger 快照原子替换，流水 append-only。"""

    def __init__(self, directory: Path, debounce_seconds: float = 1.0) -> None:
        self.directory = directory
        self.ledger_file = directory / "ledger.json"
        self.feedback_file = directory / "feedback.jsonl"
        self.events_file = directory / "baseline_events.jsonl"
        self._debounce = debounce_seconds
        self._dirty = False
        self._last_flush = 0.0
        self._lock = threading.Lock()
        directory.mkdir(parents=True, exist_ok=True)

    # ---- ledger 快照 ----

    @staticmethod
    def snapshot(satori) -> dict:
        """从网关本体取出四件套。复合键 upstream|model（上游名不含 |）。"""
        return {
            "saved_at": time.time(),
            "suspicion": {_key(u, m): v for (u, m), v in satori.suspicion.items()},
            "ledger_ts": {_key(u, m): v for (u, m), v in satori._ledger_ts.items()},
            # 身份类嫌疑（声纹/答案指纹）：不可被测试 PASS 洗白的另一半账本
            "identity": {_key(u, m): v for (u, m), v in satori.identity.items()},
            "identity_ts": {_key(u, m): v
                            for (u, m), v in satori._identity_ts.items()},
            "breakers": {_key(u, m): v for (u, m), v in satori.breakers.items()},
            "breaker_blocks": {_key(u, m): v
                               for (u, m), v in satori._breaker_blocks.items()},
        }

    def restore(self, satori, snapshot: dict) -> bool:
        """写回网关本体。恢复的是原始分与写入时刻——有效值由 _decayed 按半衰期
        自然折算，不会原样复活昨日高分。"""
        if not snapshot:
            return False
        satori.suspicion = {_split(k): v
                            for k, v in snapshot.get("suspicion", {}).items()}
        satori._ledger_ts = {_split(k): v
                             for k, v in snapshot.get("ledger_ts", {}).items()}
        satori.identity = {_split(k): v
                           for k, v in snapshot.get("identity", {}).items()}
        satori._identity_ts = {_split(k): v
                               for k, v in snapshot.get("identity_ts", {}).items()}
        satori.breakers = {_split(k): v
                           for k, v in snapshot.get("breakers", {}).items()}
        satori._breaker_blocks = {_split(k): v
                                  for k, v in snapshot.get("breaker_blocks", {}).items()}
        log.info("[state] 恢复审计状态：%d 条账本 / %d 个熔断在册",
                 len(satori.suspicion), len(satori.breakers))
        return True

    # ---- 落盘 ----

    def mark_dirty(self) -> None:
        self._dirty = True

    def maybe_flush(self, satori) -> None:
        """防抖合并写入：_add_suspicion 每次调用，爆发期只落一次盘。"""
        if not self._dirty:
            return
        if time.time() - self._last_flush < self._debounce:
            return
        self.flush(satori)

    def flush(self, satori) -> None:
        with self._lock:
            tmp = self.ledger_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot(satori), ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.ledger_file)  # 原子替换，不留半截文件
            self._dirty = False
            self._last_flush = time.time()

    def load(self) -> dict:
        if not self.ledger_file.exists():
            return {}
        try:
            return json.loads(self.ledger_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("[state] ledger.json 读取失败，按空账本启动：%r", exc)
            return {}

    # ---- 流水 ----

    def _append(self, path: Path, record: dict) -> None:
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_feedback(self, record: dict) -> None:
        self._append(self.feedback_file, record)

    def append_baseline_event(self, record: dict) -> None:
        self._append(self.events_file, record)

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def read_feedback(self) -> list[dict]:
        return self._read_jsonl(self.feedback_file)

    def read_baseline_events(self, model: str | None = None,
                             upstream: str | None = None) -> list[dict]:
        events = self._read_jsonl(self.events_file)
        if upstream is not None:
            # 旧格式事件没有 upstream 字段——按通配处理（不丢历史学习原料）
            events = [e for e in events
                      if e.get("upstream", upstream) == upstream]
        if model is not None:
            events = [e for e in events if e.get("model") == model]
        return events

    def append_test_report(self, record: dict) -> None:
        self._append(self.directory / "test_reports.jsonl", record)

    def read_test_reports(self) -> list[dict]:
        return self._read_jsonl(self.directory / "test_reports.jsonl")

    def append_tool_trace(self, record: dict) -> None:
        self._append(self.directory / "tool_traces.jsonl", record)

    def read_tool_traces(self) -> list[dict]:
        return self._read_jsonl(self.directory / "tool_traces.jsonl")
