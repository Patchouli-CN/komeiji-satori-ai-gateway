"""Wave 2 / Phase 0 + 5A 测试：溯源 sidecar、压力感知、测试上报裁决。

覆盖 v4 两轴模型的落地：
- 信任轴（Phase 6 TrustLevel）：谁的 PASS/FAIL 有分量
- 信号轴（Phase 5A）：什么能被 PASS 洗白——只有质量类，且洗不穿负分地板
- 身份账本（声纹/答案指纹不可被测试结果洗白）的注入与隔离
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from satori_gateway.app import KomeijiSatori
from satori_gateway.checkers import CheckResult
from satori_gateway.config import (
    GatewayConfig,
    SecurityConfig,
    StateConfig,
    TestingConfig,
)
from satori_gateway.security import H_ADMIN
from satori_gateway.testing import LEVEL_SPEC, TestAdjudicator, TestReport

from test_phase1 import ADMIN, KEY, build, env, issue


# ---- Phase 0.2 压力感知 ----

class TestProviderPressure:
    # 2026-01-15 是 PST（UTC-8）：UTC 11:00 = PT 03:00（黄金窗口）
    UTC_0300_PT = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc).timestamp()
    # UTC 18:00 = PT 10:00（工作高峰）
    UTC_1000_PT = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc).timestamp()
    # UTC 21:00 = PT 13:00（午休回落）
    UTC_1300_PT = datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc).timestamp()

    def test_levels_by_vendor_local_time(self):
        from satori_gateway.pressure import ProviderPressure
        p = ProviderPressure("openai")  # America/Los_Angeles
        assert p.level_at(self.UTC_0300_PT) == "LOW"
        assert p.level_at(self.UTC_1000_PT) == "HIGH"
        assert p.level_at(self.UTC_1300_PT) == "MID"

    def test_deepseek_uses_cst(self):
        from satori_gateway.pressure import ProviderPressure
        p = ProviderPressure("deepseek")  # Asia/Shanghai
        # UTC 11:00 = 北京 19:00 → MID
        assert p.level_at(self.UTC_0300_PT) == "MID"
        # UTC 02:00 = 北京 10:00 → HIGH
        beijing_10am = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc).timestamp()
        assert p.level_at(beijing_10am) == "HIGH"

    def test_pressure_weights(self):
        from satori_gateway.pressure import PRESSURE_WEIGHT
        assert PRESSURE_WEIGHT == {"LOW": 1.0, "MID": 1.0,
                                   "HIGH": 0.6, "EXTR": 0.0}

    def test_unknown_vendor_falls_back_to_utc(self):
        from satori_gateway.pressure import ProviderPressure
        p = ProviderPressure("some-unknown-vendor")
        assert p.tz_name == "UTC"
        # UTC 03:00 对 UTC 模式 = LOW
        assert p.level_at(datetime(2026, 1, 15, 3, 0,
                                   tzinfo=timezone.utc).timestamp()) == "LOW"

    def test_next_golden_window_is_golden(self):
        from satori_gateway.pressure import ProviderPressure
        p = ProviderPressure("openai")
        target = p.next_golden_window(self.UTC_1000_PT)
        assert target > self.UTC_1000_PT
        assert p.level_at(target) in ("LOW", "MID")
        # 从黄金窗口内问，应立即返回（不赌）
        assert p.next_golden_window(self.UTC_0300_PT) == self.UTC_0300_PT

    def test_describe_contains_weight(self):
        from satori_gateway.pressure import ProviderPressure
        text = ProviderPressure("openai").describe(self.UTC_1000_PT)
        assert "Current pressure: HIGH" in text
        assert "Weight: 0.6" in text


# ---- Phase 0.1 溯源 sidecar ----

class TestProvenance:
    def test_sidecar_path_naming(self):
        from pathlib import Path

        from satori_gateway.provenance import sidecar_path
        ref = Path("fingerprints/openai--gpt-4o.json")
        assert sidecar_path(ref).name == "openai--gpt-4o.meta.json"

    def test_write_and_read_roundtrip(self, tmp_path):
        from satori_gateway.provenance import read_sidecar, write_sidecar
        ref = tmp_path / "openai--gpt-4o.json"
        ref.write_text("{}", encoding="utf-8")
        write_sidecar(ref, source="official", pressure_level="LOW",
                      notes="官方直采", prompt="The capital of France is",
                      vendor="openai")
        meta = read_sidecar(ref)
        assert meta["source"] == "official"
        assert meta["pressure_level"] == "LOW"
        assert meta["notes"] == "官方直采"
        assert meta["probe_prompt_hash"]
        assert meta["collector"].startswith("satori-gateway/")
        assert meta["collected_at"] > 0

    def test_missing_sidecar_reads_empty(self, tmp_path):
        from satori_gateway.provenance import read_sidecar
        assert read_sidecar(tmp_path / "nope.json") == {}

    def test_default_pressure_when_vendor_unknown(self, tmp_path):
        from satori_gateway.provenance import read_sidecar, write_sidecar
        ref = tmp_path / "x.json"
        ref.write_text("{}", encoding="utf-8")
        write_sidecar(ref, source="secondhand", vendor=" ghost ")
        # 不认识的 vendor → UTC 模式当前等级（至少是合法值）
        assert read_sidecar(ref)["pressure_level"] in ("LOW", "MID", "HIGH")

    def test_sidecar_drives_trust_weight(self, env):
        """写进 sidecar 的 source/pressure 真实参与可信度评分（闭环 Phase 0.1）。"""
        satori, _ = env
        from satori_gateway.baseline import trust_score
        from satori_gateway.pressure import PRESSURE_WEIGHT
        from satori_gateway.provenance import write_sidecar

        up = satori.config.upstreams[0]
        from satori_gateway.checkers.fingerprint import reference_path
        ref = reference_path(satori.config.fingerprint, up, "gpt-4o")
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(json.dumps({"The": 0.9}), encoding="utf-8")
        write_sidecar(ref, source="official", pressure_level="LOW", vendor="openai")
        st = satori.baselines.recompute(*KEY)
        assert st.source == "official" and st.pressure == "LOW"
        assert st.trust == pytest.approx(1.0, abs=1e-6)

        # 同一份参考改判 secondhand + HIGH：trust 应显著下降
        write_sidecar(ref, source="secondhand", pressure_level="HIGH",
                      collected_at=meta_now(), vendor="openai")
        st2 = satori.baselines.recompute(*KEY)
        assert st2.source == "secondhand" and st2.pressure == "HIGH"
        expected = trust_score(0.5, PRESSURE_WEIGHT["HIGH"], 0.0, 30.0)
        assert st2.trust == pytest.approx(expected, abs=1e-3)


def meta_now() -> float:
    return time.time()


# ---- Phase 5A 裁决引擎（单元） ----

def make_report(**kw) -> TestReport:
    base = dict(trace_id="t1", test_suite="core", test_name="test_add",
                level="L0", status="fail", attempt=1, max_attempts=1,
                failure_diff="", model_claimed="gpt-4o", upstream="openai")
    base.update(kw)
    return TestReport(**base)


class TestAdjudicatorUnit:
    def test_l0_needs_three_fails_to_trigger(self):
        adj = TestAdjudicator()
        d1 = adj.decide(make_report())
        assert d1.action == "recorded" and d1.score_delta == 0.0
        d2 = adj.decide(make_report(trace_id="t2", attempt=1))
        assert d2.action == "recorded"
        d3 = adj.decide(make_report(trace_id="t3"))
        assert d3.action == "fail-trigger"
        assert d3.score_delta == LEVEL_SPEC["L0"]["score"] == 15
        assert d3.trigger_breaker is True

    def test_window_clears_after_trigger(self):
        adj = TestAdjudicator()
        for i in range(3):
            adj.decide(make_report(trace_id=f"t{i}"))
        # 触发后窗口清空重新武装：再来两次失败不该再次触发
        assert adj.decide(make_report(trace_id="a")).action == "recorded"
        assert adj.decide(make_report(trace_id="b")).action == "recorded"
        assert adj.decide(make_report(trace_id="c")).action == "fail-trigger"

    def test_levels_have_their_own_thresholds(self):
        adj = TestAdjudicator()
        for i in range(4):
            d = adj.decide(make_report(trace_id=f"l1-{i}", level="L1"))
        assert d.action == "recorded"  # 4 < 5
        d = adj.decide(make_report(trace_id="l1-x", level="L1"))
        assert d.action == "fail-trigger"
        assert d.score_delta == LEVEL_SPEC["L1"]["score"] == 10

    def test_pass_decays_by_level(self):
        adj = TestAdjudicator()
        d = adj.decide(make_report(status="pass", level="L0"))
        assert d.action == "pass-decay" and d.decay_amount == 10
        d = adj.decide(make_report(status="pass", level="L1", trace_id="t2"))
        assert d.decay_amount == 5
        d = adj.decide(make_report(status="pass", level="L2", trace_id="t3"))
        assert d.decay_amount == 2

    def test_l3_never_scores(self):
        adj = TestAdjudicator()
        for i in range(10):
            d = adj.decide(make_report(trace_id=f"l3-{i}", level="L3"))
        assert d.action == "ignored" and d.score_delta == 0.0

    def test_idempotent_by_trace_and_attempt(self):
        adj = TestAdjudicator()
        assert adj.decide(make_report()).accepted is True
        again = adj.decide(make_report())  # 同 trace + name + attempt
        assert again.accepted is False and again.action == "deduped"
        # 重试用 attempt=2 区分，不算重复
        retry = adj.decide(make_report(attempt=2, max_attempts=3))
        assert retry.accepted is True

    def test_flaky_test_marked_unreliable(self):
        adj = TestAdjudicator()
        # 20 个样本、3 次失败（15% > 5%）→ unreliable
        decisions = [
            adj.decide(make_report(trace_id=f"f{i}",
                                   status="fail" if i in (2, 7, 13) else "pass"))
            for i in range(20)
        ]
        assert decisions[-1].action == "unreliable"
        # 失败散布未达连续窗口，从未触发过裁决
        assert all(d.action != "fail-trigger" for d in decisions)
        # unreliable 之后仅记录
        after = adj.decide(make_report(trace_id="later", status="fail"))
        assert after.action == "recorded" and after.score_delta == 0.0

    def test_persistence_rebuilds_state(self, tmp_path):
        from satori_gateway.state import StateStore
        state = StateStore(tmp_path / "state")
        adj = TestAdjudicator(state)
        adj.decide(make_report(trace_id="p1"))
        adj.decide(make_report(trace_id="p2"))
        # 重启：新 adjudicator 从流水重建——幂等集与窗口都还在
        adj2 = TestAdjudicator(state)
        assert adj2.decide(make_report(trace_id="p1")).accepted is False
        assert adj2.decide(make_report(trace_id="p3")).accepted is True
        assert len(adj2.summary()) == 1
        assert adj2.summary()[0]["total"] == 3

    def test_validation_errors(self):
        bad_cases = [
            ({}, "trace_id"),
            ({"trace_id": "t", "test_suite": "s", "test_name": "n",
              "upstream": "u", "model_claimed": "m", "level": "L9"}, "level"),
            ({"trace_id": "t", "test_suite": "s", "test_name": "n",
              "upstream": "u", "model_claimed": "m", "level": "L0",
              "status": "maybe"}, "status"),
            ({"trace_id": "t", "test_suite": "s", "test_name": "n",
              "upstream": "u", "model_claimed": "m", "level": "L0",
              "status": "pass", "attempt": 0}, "attempt"),
        ]
        for payload, fragment in bad_cases:
            report, err = TestReport.from_payload(payload)
            assert report is None and fragment in err


# ---- Phase 5A 端点 + 两轴 + 账本拆分 ----

def post_report(client, secret, reporter, body, ts=None):
    raw = json.dumps(body, ensure_ascii=False).encode()
    return client.post("/satori/test/report", content=raw, headers={
        "X-Satori-Reporter": reporter, **sign(secret, raw, ts),
    })


def sign(secret: str, raw: bytes, ts: int | None = None) -> dict[str, str]:
    import hashlib
    import hmac as _hmac

    from satori_gateway.security import H_TIMESTAMP, H_SIGNATURE
    ts = ts if ts is not None else int(time.time())
    sig = _hmac.new(secret.encode(), f"{ts}.".encode() + raw,
                    hashlib.sha256).hexdigest()
    return {H_TIMESTAMP: str(ts), H_SIGNATURE: sig}


REPORT_BODY = {"trace_id": "tr-1", "test_suite": "core",
               "test_name": "test_add", "level": "L0", "status": "fail",
               "upstream": "openai", "model_claimed": "gpt-4o"}


class TestReportEndpoint:
    def test_requires_credential(self, env):
        _, client = env
        r = client.post("/satori/test/report", json=REPORT_BODY)
        assert r.status_code == 401

    def test_invalid_payload_400(self, env):
        _, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        r = post_report(client, secret, "bot", {"trace_id": "x"})
        assert r.status_code == 400
        assert "必填" in r.json()["detail"]

    def test_unknown_target_400(self, env):
        _, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        r = post_report(client, secret, "bot",
                        {**REPORT_BODY, "upstream": "ghost"})
        assert r.status_code == 400

    def test_three_l0_fails_degrade_and_break(self, env):
        """5C 验收：必定失败的 L0 → 3 次内 DEGRADED + 熔断（跳过 WATCH 缓冲）。"""
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        for i in range(3):
            r = post_report(client, secret, "bot",
                            {**REPORT_BODY, "trace_id": f"tr-{i}"})
            assert r.status_code == 200
        assert r.json()["action"] == "fail-trigger"
        # DEGRADED 级：注入分至少够跨过熔断线（分值 15 保底，跨线优先）
        assert r.json()["score_delta"] >= satori.config.rules.suspicion_threshold
        assert KEY in satori.breakers  # 立即熔断，实时停工
        assert satori._decayed_quality(KEY) == pytest.approx(50.0, abs=0.01)
        # 质量类入账，身份类分文未动
        assert satori._decayed_identity(KEY) == 0.0

    def test_trusted_pass_decays_quality(self, env):
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        satori.suspicion[KEY] = 30.0
        satori._ledger_ts[KEY] = time.time()
        r = post_report(client, secret, "bot",
                        {**REPORT_BODY, "trace_id": "p1", "status": "pass"})
        assert r.json()["action"] == "pass-decay"
        # L0 的衰减量是 10（不是 L1 的 5）；容许微秒级半衰期误差
        assert satori._decayed_quality(KEY) == pytest.approx(20.0, abs=0.01)

    def test_normal_pass_only_records(self, env):
        """信任轴：NORMAL 的 PASS 不衰减。"""
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="normal")
        satori.suspicion[KEY] = 30.0
        satori._ledger_ts[KEY] = time.time()
        r = post_report(client, secret, "bot",
                        {**REPORT_BODY, "trace_id": "p1", "status": "pass"})
        assert r.json()["action"] == "recorded"
        assert satori._decayed_quality(KEY) == pytest.approx(30.0, abs=0.01)

    def test_unverified_fail_only_records(self, env):
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="unverified")
        r = post_report(client, secret, "bot", REPORT_BODY)
        assert r.json()["action"] == "recorded"
        assert satori._decayed(KEY) == 0.0

    def test_dedup_same_trace_attempt(self, env):
        _, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        r1 = post_report(client, secret, "bot", REPORT_BODY)
        assert r1.json()["accepted"] is True
        r2 = post_report(client, secret, "bot", REPORT_BODY)
        assert r2.json()["accepted"] is False
        assert r2.json()["action"] == "deduped"

    def test_negative_floor_and_dimension_isolation(self, env):
        """核心验收：PASS 洗不穿负分地板，更洗不掉身份类嫌疑。"""
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        satori.suspicion[KEY] = 6.0          # 质量类 6
        satori._ledger_ts[KEY] = time.time()
        satori.identity[KEY] = 20.0          # 身份类 20（声纹对不上）
        satori._identity_ts[KEY] = time.time()
        # L0 PASS 想衰减 10：6-10 = -4，但身份类 20 > 地板 5 → 质量类归零即可
        r = post_report(client, secret, "bot",
                        {**REPORT_BODY, "trace_id": "fp", "status": "pass"})
        assert r.json()["action"] == "pass-decay"
        assert satori._decayed_quality(KEY) == pytest.approx(0.0, abs=0.01)
        assert satori._decayed_identity(KEY) == pytest.approx(20.0, abs=0.01)
        assert satori._decayed(KEY) == pytest.approx(20.0, abs=0.01)

    def test_floor_applies_to_total(self, env):
        """质量类单独存在时，PASS 也洗不穿总分地板。"""
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        satori.suspicion[KEY] = 12.0
        satori._ledger_ts[KEY] = time.time()
        post_report(client, secret, "bot",
                    {**REPORT_BODY, "trace_id": "fp2", "status": "pass"})
        # 12 - 10 = 2 < 地板 5 → 钳到 5
        assert satori._decayed_quality(KEY) == pytest.approx(5.0, abs=0.01)

    def test_breaker_needs_trusted(self, env):
        """非 TRUSTED 的 FAIL 触发裁决但不熔断。"""
        satori, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="normal")
        for i in range(3):
            post_report(client, secret, "bot",
                        {**REPORT_BODY, "trace_id": f"nb-{i}"})
        assert KEY not in satori.breakers

    def test_status_tests_section(self, env):
        _, client = env
        secret = issue(client, "bot", roles=["reporter"], trust="trusted")
        post_report(client, secret, "bot", REPORT_BODY)
        r = client.get("/satori/status")
        tests = r.json()["tests"]
        assert tests and tests[0]["upstream"] == "openai"
        assert tests[0]["model"] == "gpt-4o"
        assert tests[0]["total"] == 1 and tests[0]["failed"] == 1
        assert tests[0]["unreliable"] is False


class TestIdentityCleared:
    """身份解冻的第二条路：operator 显式裁决（另一条是重新采集基线）。"""

    def test_operator_clears_identity_only(self, env):
        satori, client = env
        secret = issue(client, "op", roles=["operator"], trust="trusted")
        satori.suspicion[KEY] = 30.0          # 质量类
        satori._ledger_ts[KEY] = time.time()
        satori.identity[KEY] = 20.0           # 身份类
        satori._identity_ts[KEY] = time.time()
        body = json.dumps({"upstream": "openai", "model": "gpt-4o",
                           "reason": "官方确认换模型，误报"}).encode()
        r = client.post("/satori/baseline/identity-cleared", content=body,
                        headers={"X-Satori-Reporter": "op",
                                 **sign(secret, body)})
        assert r.status_code == 200
        assert r.json()["cleared"] is True
        assert satori._decayed_identity(KEY) == 0.0     # 身份类解冻
        assert satori._decayed_quality(KEY) == pytest.approx(30.0, abs=0.01)  # 质量类不动

    def test_reporter_cannot_clear_403(self, env):
        satori, client = env
        secret = issue(client, "bob")  # reporter
        satori.identity[KEY] = 20.0
        satori._identity_ts[KEY] = time.time()
        body = json.dumps({"upstream": "openai", "model": "gpt-4o"}).encode()
        r = client.post("/satori/baseline/identity-cleared", content=body,
                        headers={"X-Satori-Reporter": "bob",
                                 **sign(secret, body)})
        assert r.status_code == 403
        assert satori._decayed_identity(KEY) == pytest.approx(20.0, abs=0.01)

    def test_clearing_empty_ledger_is_noop(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        body = json.dumps({"upstream": "openai", "model": "gpt-4o"}).encode()
        r = client.post("/satori/baseline/identity-cleared", content=body,
                        headers={"X-Satori-Reporter": "op",
                                 **sign(secret, body)})
        assert r.json()["cleared"] is False

    def test_disabled_returns_503(self, tmp_path):
        satori, client = build(tmp_path, testing_enabled=False)
        # 注意：testing 关闭不影响 [security]——admin 端点仍要 admin_secret
        r = client.post("/satori/admin/credentials",
                        json={"reporter_id": "bot", "trust_level": "normal",
                              "roles": ["reporter"]},
                        headers={H_ADMIN: ADMIN})
        assert r.status_code == 201
        secret = r.json()["secret"]
        r = post_report(client, secret, "bot", REPORT_BODY)
        assert r.status_code == 503
        satori.security.store.close()


# ---- checker 失败 → 身份账本接线 ----

class FakeFingerprint:
    name = "fingerprint"

    async def check(self, client, upstream, model):
        return CheckResult("fingerprint", upstream.name, model, False, 0.5,
                           "JS 散度 0.5000（阈值 0.15）")


class FakeNoReference:
    """无参考时的 checker 表现：ok=False 但 score=0——不该入账。"""
    name = "fingerprint"

    async def check(self, client, upstream, model):
        return CheckResult("fingerprint", upstream.name, model, False, 0.0,
                           "无参考指纹，先用 collect 命令采集")


class TestIdentityLedgerWiring:
    def test_checker_failure_feeds_identity_ledger(self, tmp_path):
        from satori_gateway.app import _IDENTITY_CHECKERS
        satori, _ = build(tmp_path)
        satori.checkers = [FakeFingerprint()]
        asyncio.run(satori.run_all_checks(None))
        assert satori._decayed_identity(KEY) == pytest.approx(
            _IDENTITY_CHECKERS["fingerprint"], abs=0.01)
        assert satori._decayed_quality(KEY) == 0.0  # 质量类分文未动
        satori.security.store.close()

    def test_no_reference_failure_does_not_feed(self, tmp_path):
        satori, _ = build(tmp_path)
        satori.checkers = [FakeNoReference()]
        asyncio.run(satori.run_all_checks(None))
        assert satori._decayed(KEY) == 0.0  # BASIC 模式不积累
        satori.security.store.close()

    def test_identity_survives_persistence(self, tmp_path):
        from satori_gateway.state import StateStore
        satori, _ = build(tmp_path)
        satori.checkers = [FakeFingerprint()]
        asyncio.run(satori.run_all_checks(None))
        satori.state.flush(satori)

        satori2, _ = build(tmp_path)
        satori2.state.restore(satori2, satori2.state.load())
        assert satori2._decayed_identity(KEY) == pytest.approx(20.0, abs=0.01)
        satori.security.store.close()
        satori2.security.store.close()
