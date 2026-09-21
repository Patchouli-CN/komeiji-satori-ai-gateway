"""CLI 装配回归测试。

存在理由：曾有一次并行编辑同一文件发生读写竞争，argparse 的 dispatch 先于
函数签名落地，导致 `satori collect` 一跑就 TypeError——而测试全是网关
进程内的，没有一个碰 CLI 装配，所以全绿放行。这组测试就是那道闸门：
**main() 传的参数，函数必须收得下**。
"""

from __future__ import annotations

import inspect


def _collect_params() -> set[str]:
    from satori_gateway.__main__ import _collect
    return set(inspect.signature(_collect).parameters)


def _answers_params() -> set[str]:
    from satori_gateway.__main__ import _answers
    return set(inspect.signature(_answers).parameters)


def test_collect_signature_matches_dispatch():
    params = _collect_params()
    assert {"config_path", "upstream_name", "model", "prompt",
            "source", "notes", "force_pressure", "wait_for_low"} <= params


def test_answers_signature_matches_dispatch():
    params = _answers_params()
    assert {"config_path", "upstream_name", "model",
            "source", "notes", "force_pressure", "wait_for_low"} <= params


def test_cli_help_mentions_new_flags():
    """collect/answers 的 --source 等 Phase 0 参数在 help 里可见。"""
    from satori_gateway.__main__ import main
    parser = inspect.getsource(main)
    assert "--source" in parser and "--force-pressure" in parser
    assert "--wait-for-low" in parser and "--notes" in parser


def test_pressure_gate_low_window_passes_through(monkeypatch):
    """黄金窗口不拦不劝，直接放行并返回等级。"""
    from satori_gateway.__main__ import _pressure_gate
    from satori_gateway.pressure import ProviderPressure

    monkeypatch.setattr(ProviderPressure, "level_at", lambda self, now=None: "LOW")
    assert _pressure_gate("openai", force_pressure=False,
                          wait_for_low=False) == "LOW"


def test_pressure_gate_high_warns_and_force_overrides(monkeypatch, capsys):
    """高压窗口：默认劝退（input 答 n → SystemExit），--force-pressure 直取。"""
    from satori_gateway.__main__ import _pressure_gate
    from satori_gateway.pressure import ProviderPressure

    monkeypatch.setattr(ProviderPressure, "level_at", lambda self, now=None: "HIGH")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    import pytest
    with pytest.raises(SystemExit):
        _pressure_gate("openai", force_pressure=False, wait_for_low=False)
    out = capsys.readouterr().out
    assert "Current pressure: HIGH" in out
    # --force-pressure：不问，直取，事实记进 sidecar
    assert _pressure_gate("openai", force_pressure=True,
                          wait_for_low=False) == "HIGH"


def test_record_refreshed_appends_event(tmp_path):
    """采集成功 → refreshed 事件进流水（TTL 的学习原料闭环）。"""
    from satori_gateway.__main__ import _record_refreshed
    from satori_gateway.config import GatewayConfig, StateConfig
    from satori_gateway.config import load as _load  # noqa: F401  （占位防误用）
    from satori_gateway.state import StateStore

    state = StateStore(tmp_path / "state")
    from satori_gateway.config import Config
    cfg = Config(
        gateway=GatewayConfig(),
        fingerprint=None, canary=[], rules=None, record=None,
        identity=None, answerprint=None, logging=None, breaker=None,
        upstreams=[], state=StateConfig(directory=tmp_path / "state"),
    )
    _record_refreshed(cfg, "gpt-4o", "LOW", "official")
    events = state.read_baseline_events(model="gpt-4o")
    assert len(events) == 1
    assert events[0]["event"] == "refreshed"
    assert events[0]["pressure_level_at_collect"] == "LOW"
    assert events[0]["source"] == "official"
    # 第二笔带出距上次的天数
    _record_refreshed(cfg, "gpt-4o", "LOW", "official")
    events = state.read_baseline_events(model="gpt-4o")
    assert len(events) == 2 and events[1]["days_since_last_refresh"] is not None
