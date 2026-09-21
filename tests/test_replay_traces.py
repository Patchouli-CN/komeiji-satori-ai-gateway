"""Replay Forensics · Tool Call 链回放（v4 Phase 4 联动）。"""

from __future__ import annotations

import json

from satori_gateway.record import replay_tool_traces


def _write(path, records):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in records), encoding="utf-8")


def test_replay_tool_traces_summary(tmp_path):
    p = tmp_path / "tool_traces.jsonl"
    _write(p, [
        {"ts": 1000.0, "upstream": "openai", "model": "gpt-4o",
         "session": "s1", "trace_id": "t1", "slop_score": 0.0,
         "first_suspicious": -1,
         "steps": [{"index": 0, "tool": "get_weather",
                    "args_valid": True, "args_bytes": 12, "flags": []}]},
        {"ts": 1001.0, "upstream": "openai", "model": "gpt-4o",
         "session": "s1", "trace_id": "t2", "slop_score": 30.0,
         "first_suspicious": 0,
         "steps": [{"index": 0, "tool": "get_weather",
                    "args_valid": False, "args_bytes": 8,
                    "flags": ["broken-args"]}]},
    ])
    report = replay_tool_traces(p)
    assert report.total == 2 and report.suspicious == 1 and report.clean == 1
    origins = report.origins()
    assert len(origins) == 1
    assert origins[0]["trace_id"] == "t2"
    assert origins[0]["steps"][0]["flags"] == ["broken-args"]


def test_replay_missing_file_is_empty(tmp_path):
    report = replay_tool_traces(tmp_path / "nope.jsonl")
    assert report.total == 0 and report.chains == []
