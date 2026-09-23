"""Wave 3 / Phase 2 测试：动态 TTL 引擎、老化预警、手动锁定。

核心原则回归测试：**预测不误杀**——TTL 到期只点灯，退役必经 feedback。
"""

from __future__ import annotations

import json
import time

import pytest

from satori_gateway.state import StateStore
from satori_gateway.ttl import (
    AGING_AT,
    COLD_START_TTL_DAYS,
    CRITICAL_AT,
    EXPIRED_AT,
    MAX_TTL_DAYS,
    MIN_TTL_DAYS,
    TtlEngine,
    median,
    reject_outliers,
)

from test_phase1 import KEY, build, env, issue


def make_events(state: StateStore, model: str,
                intervals_days: list[float], upstream: str = "openai") -> None:
    """造 len(intervals)+1 条事件，使相邻间隔正好等于 intervals_days
    （intervals() 返回的是 n-1 个间隔——事件比间隔多一个）。
    事件归属 (upstream, model)：同名模型跨上游的账分开学。"""
    ts = time.time() - 86400 * (sum(intervals_days) + 1)
    state.append_baseline_event(
        {"ts": ts, "upstream": upstream, "model": model, "event": "refreshed"})
    for gap in intervals_days:
        ts += 86400 * gap
        state.append_baseline_event(
            {"ts": ts, "upstream": upstream, "model": model,
             "event": "refreshed"})


class TestMedianAndOutliers:
    def test_median_odd_even(self):
        assert median([3.0, 1.0, 2.0]) == 2.0
        assert median([4.0, 1.0, 3.0, 2.0]) == 2.5
        assert median([]) == 0.0

    def test_outlier_rejection(self):
        # 10 个 ~10 天的间隔里混一个 500 天的异常值 → 被剔除
        values = [10.0] * 10 + [500.0]
        clean = reject_outliers(values)
        assert 500.0 not in clean and len(clean) == 10
        # 样本 < 2 无从估计离散度，原样返回
        assert reject_outliers([99.0]) == [99.0]


class TestTtlLearning:
    def test_cold_start_fixed_30d(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        assert engine.ttl_for("openai", "gpt-4o") == (
            COLD_START_TTL_DAYS, "cold-start", 0)
        make_events(state, "gpt-4o", [10.0, 12.0])  # 只 2 个间隔（3 条事件）
        assert engine.ttl_for("openai", "gpt-4o")[0] == COLD_START_TTL_DAYS

    def test_three_events_factor_07(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        # 3 个间隔 = 4 条事件：中位数 10 × 0.7 = 7
        make_events(state, "gpt-4o", [10.0, 10.0, 10.0])
        ttl, source, events = engine.ttl_for("openai", "gpt-4o")
        assert events == 3 and source == "median"
        assert ttl == pytest.approx(7.0)

    def test_five_events_factor_09_and_clamp(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        # 5 个间隔中位数 20 × 0.9 = 18
        make_events(state, "gpt-4o", [20.0, 20.0, 20.0, 20.0, 20.0])
        ttl, source, _ = engine.ttl_for("openai", "gpt-4o")
        assert source == "median" and ttl == pytest.approx(18.0)
        # 钳制下限：间隔 5 天 × 0.9 = 4.5 → 钳到 7
        make_events(state, "m2", [5.0] * 5)
        assert TtlEngine(state).ttl_for("openai", "m2")[0] == MIN_TTL_DAYS
        # 钳制上限：间隔 400 天 × 0.9 = 360 → 钳到 180
        make_events(state, "m3", [400.0] * 5)
        assert TtlEngine(state).ttl_for("openai", "m3")[0] == MAX_TTL_DAYS

    def test_outliers_dont_poison_median(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        # 15 个 ~10 天的正常间隔里混一个 500 天的异常值 → 3σ 剔除
        # （样本太少时 3σ 会被异常值自己撑大——这是有限样本的已知边界，
        # 所以引擎要求 ≥5 个间隔才启用学习）
        make_events(state, "gpt-4o", [10.0] * 15 + [500.0])
        ttl, _, _ = engine.ttl_for("openai", "gpt-4o")
        assert ttl == pytest.approx(9.0)  # 中位数 10 × 0.9

    def test_outlier_survives_in_tiny_samples(self, tmp_path):
        """6 个间隔里混 1 个异常：3σ 被自己撑大剔不掉——老实承认，不假装。"""
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        make_events(state, "gpt-4o", [10.0, 10.0, 10.0, 10.0, 10.0, 500.0])
        ttl, source, events = engine.ttl_for("openai", "gpt-4o")
        assert source == "median" and events == 6
        # 中位数仍是 10（中位数天生抗异常），学习值不受污染
        assert ttl == pytest.approx(9.0)

    def test_per_model_isolation(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        make_events(state, "gpt-4o", [10.0] * 5)
        assert engine.ttl_for("openai", "gpt-4o")[0] == pytest.approx(9.0)
        assert engine.ttl_for("openai", "claude-x")[0] == COLD_START_TTL_DAYS

    def test_per_upstream_isolation(self, tmp_path):
        """官方 + 中转同名模型：事件不串味——中转侧照样冷启动。"""
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        make_events(state, "gpt-4o", [10.0] * 5, upstream="openai")
        assert engine.ttl_for("openai", "gpt-4o")[0] == pytest.approx(9.0)
        assert engine.ttl_for("relay", "gpt-4o")[0] == COLD_START_TTL_DAYS

    def test_legacy_events_without_upstream_match_any(self, tmp_path):
        """旧格式事件（无 upstream 字段）按通配兼容——不丢历史学习原料。"""
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        ts = time.time() - 86400 * 60
        for _ in range(6):
            ts += 86400 * 10
            state.append_baseline_event(
                {"ts": ts, "model": "gpt-4o", "event": "refreshed"})
        assert engine.ttl_for("openai", "gpt-4o")[1] == "median"
        assert engine.ttl_for("relay", "gpt-4o")[1] == "median"


class TestVerdictAndWarnings:
    def _engine(self, tmp_path):
        state = StateStore(tmp_path / "s")
        return state, TtlEngine(state)

    def test_no_collected_at_no_verdict(self, tmp_path):
        _, engine = self._engine(tmp_path)
        assert engine.verdict("openai", "gpt-4o", None) is None

    def test_states_by_consumption(self, tmp_path):
        _, engine = self._engine(tmp_path)
        now = time.time()
        # 手动锁定 10 天 TTL，逐段看状态翻转
        engine.override("openai", "gpt-4o", ttl_days=10.0)
        assert engine.verdict("openai", "gpt-4o",
                              now - 86400 * 5).state == "fresh"
        assert engine.verdict("openai", "gpt-4o",
                              now - 86400 * 8).state == "aging"
        assert engine.verdict("openai", "gpt-4o",
                              now - 86400 * 9.5).state == "critical"
        assert engine.verdict("openai", "gpt-4o",
                              now - 86400 * 12).state == "expired"

    def test_warning_fires_once_per_state(self, tmp_path):
        _, engine = self._engine(tmp_path)
        now = time.time()
        engine.override("openai", "gpt-4o", ttl_days=10.0)
        first = engine.due_warnings(engine.verdict("openai", "gpt-4o",
                                                   now - 86400 * 8))
        assert first and "老化" in first
        # 同一状态不重复点灯
        assert engine.due_warnings(engine.verdict(
            "openai", "gpt-4o", now - 86400 * 8.1)) is None
        # 状态升级再点
        second = engine.due_warnings(engine.verdict("openai", "gpt-4o",
                                                    now - 86400 * 12))
        assert second and "不会自动退役" in second  # 预测不误杀写进灯语

    def test_expired_never_retires_baseline(self, env, tmp_path):
        """红灯只是灯——reference 文件必须还在（退役只认 feedback）。"""
        from test_phase1 import write_refs
        satori, _ = env
        write_refs(satori, collected_at=time.time() - 86400 * 60)  # 60 天前采集
        satori.baselines.recompute(*KEY)
        st = satori.baselines.get(*KEY)
        verdict = satori.ttl.verdict(st.upstream, st.model, st.collected_at)
        assert verdict.state == "expired"
        # 过期了，但基线文件没被动过，也没有任何退役动作
        assert (satori.config.fingerprint.reference_dir
                / "openai--gpt-4o.json").exists()
        assert satori.baselines.get(*KEY).retired_at is None

    def test_thresholds_ordered(self):
        assert 0 < AGING_AT < CRITICAL_AT < EXPIRED_AT


class TestOverride:
    def test_override_and_persist(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        engine.override("openai", "gpt-4o", ttl_days=45.0, reason="等官方发版")
        # 新引擎（模拟重启）从文件恢复
        engine2 = TtlEngine(state)
        assert engine2.ttl_for("openai", "gpt-4o") == (45.0, "override", 0)

    def test_override_isolated_per_upstream(self, tmp_path):
        """同名模型跨上游：锁定官方的不影响中转。"""
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        engine.override("openai", "gpt-4o", ttl_days=45.0)
        assert engine.ttl_for("openai", "gpt-4o")[0] == 45.0
        assert engine.ttl_for("relay", "gpt-4o")[0] == COLD_START_TTL_DAYS

    def test_legacy_bare_model_key_still_honored(self, tmp_path):
        """旧格式 override 文件（裸 model 键）按通配兼容读出。"""
        state = StateStore(tmp_path / "s")
        (tmp_path / "s" / "ttl_overrides.json").write_text(json.dumps(
            {"gpt-4o": {"model": "gpt-4o", "ttl_days": 45.0,
                        "expires_at": None, "reason": "旧格式",
                        "set_at": time.time()}}), encoding="utf-8")
        engine = TtlEngine(state)
        assert engine.ttl_for("openai", "gpt-4o") == (45.0, "override", 0)
        assert engine.override_of("openai", "gpt-4o")["reason"] == "旧格式"
        # 清除时新旧键一并清
        assert engine.clear_override("openai", "gpt-4o") is True
        assert engine.ttl_for("openai", "gpt-4o")[0] == COLD_START_TTL_DAYS

    def test_expires_at_auto_unlock(self, tmp_path):
        state = StateStore(tmp_path / "s")
        engine = TtlEngine(state)
        engine.override("openai", "gpt-4o", expires_at=time.time() - 10)
        # 已过期的锁定自动解锁，回归冷启动值
        assert engine.ttl_for("openai", "gpt-4o")[0] == COLD_START_TTL_DAYS
        assert engine.override_of("openai", "gpt-4o") is None

    def test_endpoint_requires_operator(self, env):
        satori, client = env
        body = json.dumps({"upstream": "openai", "model": "gpt-4o",
                           "ttl_days": 60, "reason": "等发版"}).encode()
        # 无凭据 → 401
        r = client.post("/satori/baseline/ttl/override", content=body)
        assert r.status_code == 401
        # reporter 角色 → 403
        secret = issue(client, "bob")
        from satori_gateway.security import H_REPORTER, sign_request
        r = client.post("/satori/baseline/ttl/override", content=body, headers={
            H_REPORTER: "bob", **sign_request(secret, body)})
        assert r.status_code == 403
        # operator → 200，且立即反映到基线状态
        op = issue(client, "op", roles=["operator"])
        r = client.post("/satori/baseline/ttl/override", content=body, headers={
            H_REPORTER: "op", **sign_request(op, body)})
        assert r.status_code == 200
        assert satori.baselines.get(*KEY).ttl_days == 60.0
        assert satori.ttl.override_of("openai", "gpt-4o")["reason"] == "等发版"

    def test_endpoint_validates(self, env):
        _, client = env
        op = issue(client, "op", roles=["operator"])
        from satori_gateway.security import H_REPORTER, sign_request

        def post(payload):
            raw = json.dumps(payload).encode()
            return client.post("/satori/baseline/ttl/override", content=raw,
                               headers={H_REPORTER: "op",
                                        **sign_request(op, raw)})

        assert post({"upstream": "ghost", "model": "x",
                     "ttl_days": 30}).status_code == 400
        assert post({"upstream": "openai", "model": "gpt-4o"}).status_code == 400
        assert post({"upstream": "openai", "model": "gpt-4o",
                     "ttl_days": "abc"}).status_code == 400
        # 负数 / NaN / expires_at-only 一并拒绝——锁的语义必须完整且有限
        assert post({"upstream": "openai", "model": "gpt-4o",
                     "ttl_days": -5}).status_code == 400
        assert post({"upstream": "openai", "model": "gpt-4o",
                     "ttl_days": float("nan")}).status_code == 400
        r = post({"upstream": "openai", "model": "gpt-4o",
                  "expires_at": time.time() + 86400})
        assert r.status_code == 400 and "ttl_days" in r.json()["detail"]


class TestStatusSurface:
    def test_status_exposes_ttl(self, env):
        from test_phase1 import write_refs
        satori, client = env
        write_refs(satori, collected_at=time.time() - 86400 * 8)
        satori.baselines.recompute_all()
        r = client.get("/satori/status")
        entry = r.json()["baselines"][0]
        assert entry["ttl"]["ttl_days"] == 30.0     # 冷启动固定值
        assert entry["ttl"]["state"] == "fresh"
        assert entry["ttl"]["events"] == 0
        assert entry["ttl_override"] is None
