"""Wave 1 / Phase 1 + 0.5 测试：反馈闭环、退役归档、状态持久化、三级判别。

对应 v4 TODO：Phase 1.0 状态持久化 / 1.1 反馈接口 / 1.2 退役归档 /
1.3 判别状态机 / 1.4 启动自检 + Phase 0.5 冷启动计量。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from satori_gateway.app import KomeijiSatori
from satori_gateway.baseline import (
    TRUST_BASIC,
    TRUST_STANDARD,
    TRUST_STRICT,
    BaselineLevel,
    BaselineManager,
    level_for,
    trust_score,
)
from satori_gateway.checkers.answerprint import answers_path
from satori_gateway.checkers.fingerprint import reference_path
from satori_gateway.config import (
    AnswerPrintConfig,
    BreakerConfig,
    Config,
    FingerprintConfig,
    GatewayConfig,
    IdentityConfig,
    LoggingConfig,
    RecordConfig,
    RulesConfig,
    SecurityConfig,
    StateConfig,
    TestingConfig,
    Upstream,
)
from satori_gateway.security import (
    H_ADMIN,
    H_REPORTER,
    H_TIMESTAMP,
    H_SIGNATURE,
    Role,
    sign_request,
)
from satori_gateway.state import StateStore

ADMIN = "test-admin-secret"
KEY = ("openai", "gpt-4o")


def build(tmp_path, testing_enabled: bool = True) -> tuple[KomeijiSatori, TestClient]:
    fp_dir = tmp_path / "fingerprints"
    fp_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        gateway=GatewayConfig(),
        fingerprint=FingerprintConfig(reference_dir=fp_dir),
        canary=[], rules=RulesConfig(), record=RecordConfig(),
        identity=IdentityConfig(), answerprint=AnswerPrintConfig(),
        logging=LoggingConfig(), breaker=BreakerConfig(enabled=True),
        upstreams=[Upstream(name="openai", base_url="http://x/v1",
                            api_key="k", models=["gpt-4o"])],
        security=SecurityConfig(enabled=True,
                                db=tmp_path / "state" / "credentials.db",
                                admin_secret=ADMIN),
        state=StateConfig(directory=tmp_path / "state",
                          archive_dir=tmp_path / "archive" / "baselines"),
        testing=TestingConfig(enabled=testing_enabled),
    )
    satori = KomeijiSatori(cfg, [], None)
    return satori, TestClient(satori.app)


@pytest.fixture()
def env(tmp_path):
    satori, client = build(tmp_path)
    yield satori, client
    satori.security.store.close()


def write_refs(satori: KomeijiSatori, source: str = "official",
               pressure: str = "LOW", collected_at: float | None = None,
               with_answers: bool = True) -> None:
    """造一份（带溯源 sidecar 的）参考基线。"""
    fp_cfg = satori.config.fingerprint
    up = satori.config.upstreams[0]
    ref = reference_path(fp_cfg, up, "gpt-4o")
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(json.dumps({"The": 0.9}), encoding="utf-8")
    if with_answers:
        answers_path(fp_cfg, up, "gpt-4o").write_text(
            json.dumps({"q": "a"}), encoding="utf-8")
    ref.with_suffix(".meta.json").write_text(json.dumps({
        "source": source,
        "collected_at": collected_at if collected_at is not None else time.time(),
        "pressure_level": pressure, "collector": "pytest",
    }), encoding="utf-8")


def issue(client: TestClient, reporter_id: str, roles: list[str] | None = None,
          trust: str = "normal") -> str:
    r = client.post(
        "/satori/admin/credentials",
        json={"reporter_id": reporter_id, "trust_level": trust,
              "roles": roles if roles is not None else ["reporter"]},
        headers={H_ADMIN: ADMIN},
    )
    assert r.status_code == 201, r.text
    return r.json()["secret"]


def post_feedback(client: TestClient, secret: str, reporter: str,
                  body: dict, ts: int | None = None):
    raw = json.dumps(body, ensure_ascii=False).encode()
    return client.post("/satori/baseline/feedback", content=raw, headers={
        H_REPORTER: reporter, **sign_request(secret, raw, ts=ts),
    })


def seed_events(satori: KomeijiSatori, n: int = 3) -> None:
    for _ in range(n):
        # 不带 upstream 的旧格式事件：read_baseline_events 按通配兼容
        satori.state.append_baseline_event(
            {"ts": time.time(), "model": "gpt-4o", "event": "refreshed"})


# ---- 可信度公式与判别联动（v4 Phase 0.3 / 1.3） ----

class TestTrustAndLevel:
    def test_level_mapping(self):
        assert level_for(1.0) is BaselineLevel.STRICT
        assert level_for(TRUST_STRICT) is BaselineLevel.STRICT
        assert level_for(0.5) is BaselineLevel.STANDARD
        assert level_for(TRUST_STANDARD) is BaselineLevel.STANDARD
        assert level_for(0.2) is BaselineLevel.BASIC
        assert level_for(TRUST_BASIC) is BaselineLevel.BASIC
        assert level_for(0.0) is BaselineLevel.BASIC

    def test_source_ordering_official_over_secondhand(self):
        official = trust_score(1.0, 1.0, 0.0, 30.0)
        secondhand = trust_score(0.5, 1.0, 0.0, 30.0)
        community = trust_score(0.3, 1.0, 0.0, 30.0)
        assert official == 1.0
        assert official > secondhand > community
        # 官方新鲜基线 STRICT，二手只能到 STANDARD——来源先于时机
        assert level_for(official) is BaselineLevel.STRICT
        assert level_for(secondhand) is BaselineLevel.STANDARD

    def test_pressure_high_downgrades(self):
        low = trust_score(1.0, 1.0, 0.0, 30.0)
        high = trust_score(1.0, 0.6, 0.0, 30.0)
        assert high < low
        assert level_for(high) is BaselineLevel.STANDARD

    def test_aging_pushes_down(self):
        fresh = trust_score(1.0, 1.0, 0.0, 30.0)
        half = trust_score(1.0, 1.0, 15.0, 30.0)   # (1-0.5)^1.5 ≈ 0.354
        dead = trust_score(1.0, 1.0, 30.0, 30.0)   # 保质期耗尽
        assert fresh > half > dead == 0.0
        assert level_for(half) is BaselineLevel.BASIC

    def test_ttl_zero_returns_zero(self):
        assert trust_score(1.0, 1.0, 5.0, 0.0) == 0.0

    def test_negative_age_clamped_to_one(self):
        """collected_at 在未来（时钟回拨/异地导入）时 trust 不许超过 1。"""
        assert trust_score(1.0, 1.0, -5.0, 30.0) == 1.0
        assert trust_score(2.0, 1.0, -5.0, 30.0) == 1.0  # 来源权重异常也钳住
        assert 0.0 <= trust_score(1.0, 1.0, 99.0, 30.0) <= 1.0


# ---- 溯源与Manager评估 ----

class TestBaselineManager:
    def test_no_reference_is_basic(self, env):
        satori, _ = env
        st = satori.baselines.recompute(*KEY)
        assert st.level is BaselineLevel.BASIC and st.trust == 0.0
        assert st.reference is None
        assert "No baseline collected" in satori.baselines.startup_report()[0]

    def test_fresh_official_baseline_is_strict(self, env):
        satori, _ = env
        write_refs(satori)  # official + LOW + 刚采
        st = satori.baselines.recompute(*KEY)
        assert st.level is BaselineLevel.STRICT
        assert st.trust == pytest.approx(1.0, abs=1e-6)  # 采集到评估隔了几微秒
        assert st.source == "official" and st.reference == "fingerprint"

    def test_missing_sidecar_defaults_to_secondhand(self, env):
        satori, _ = env
        write_refs(satori, source="official")  # 先写 sidecar
        # 删掉 sidecar：存量参考一律按 secondhand 0.5
        ref = reference_path(satori.config.fingerprint,
                             satori.config.upstreams[0], "gpt-4o")
        ref.with_suffix(".meta.json").unlink()
        st = satori.baselines.recompute(*KEY)
        assert st.source == "secondhand"
        assert st.trust < 0.8  # 到不了 STRICT
        assert st.collected_at is not None  # mtime 兜底

    def test_retire_archives_and_goes_basic(self, env):
        satori, _ = env
        write_refs(satori)
        satori.baselines.recompute(*KEY)
        st = satori.baselines.retire("openai", "gpt-4o", reason="official_update",
                                     reporter="alice", last_js=0.42)
        assert st.level is BaselineLevel.BASIC
        # 参考文件搬家到归档目录，就地消失
        arch = satori.config.state.archive_dir / "openai--gpt-4o"
        assert (arch / "openai--gpt-4o.json").exists()
        assert (arch / "openai--gpt-4o--answers.json").exists()
        assert (arch / "meta.json").exists()
        meta = json.loads((arch / "meta.json").read_text(encoding="utf-8"))
        assert meta["reason"] == "official_update" and meta["reporter"] == "alice"
        assert meta["last_js"] == 0.42
        fp_dir = satori.config.fingerprint.reference_dir
        assert not (fp_dir / "openai--gpt-4o.json").exists()
        # 事件流水记一笔（Phase 2 的学习数据源）
        events = satori.state.read_baseline_events(model="gpt-4o")
        assert events[-1]["event"] == "retired"
        # 再评估：BASIC + retired_at 留痕
        st2 = satori.baselines.recompute(*KEY)
        assert st2.level is BaselineLevel.BASIC
        assert st2.retired_at == meta["retired_at"]
        assert "Baseline expired" in satori.baselines.startup_report()[0]

    def test_cold_start_boundary(self, env):
        satori, _ = env
        assert satori.baselines.is_cold_start("openai", "gpt-4o") is True
        seed_events(satori, 3)
        assert satori.baselines.is_cold_start("openai", "gpt-4o") is False

    def test_answers_only_reference_labeled_answers(self, env):
        """只有答案指纹参考时，reference 字段按实际文件标 answers——
        不许恒标 fingerprint。"""
        satori, _ = env
        fp_cfg = satori.config.fingerprint
        up = satori.config.upstreams[0]
        answers_path(fp_cfg, up, "gpt-4o").write_text(
            json.dumps({"q": "a"}), encoding="utf-8")
        st = satori.baselines.recompute(*KEY)
        assert st.reference == "answers"
        # 无 sidecar → secondhand 0.5 → STANDARD（不到 STRICT）
        assert st.level is BaselineLevel.STANDARD

    def test_retire_then_recollect_restores_strict(self, env):
        """退役 → 重新采集 → 退役标记失效，恢复按可信度判别（不再误报 BASIC）。"""
        satori, _ = env
        write_refs(satori)
        satori.baselines.recompute(*KEY)
        satori.baselines.retire("openai", "gpt-4o", reason="official_update",
                                reporter="op")
        assert "Baseline expired" in satori.baselines.startup_report()[0]
        time.sleep(0.02)  # 保证新参考的 mtime 晚于 retired_at
        write_refs(satori)  # 重新采集（等价于 satori collect）
        st = satori.baselines.recompute(*KEY)
        assert st.level is BaselineLevel.STRICT
        assert st.retired_at is None
        assert satori.baselines.startup_report() == []  # 不再误报


# ---- 状态持久化（v4 Phase 1.0） ----

class TestStateStore:
    def test_ledger_roundtrip(self, env):
        satori, _ = env
        satori.suspicion[KEY] = 42.0
        satori._ledger_ts[KEY] = time.time()
        satori.breakers[KEY] = time.time()
        satori._breaker_blocks[KEY] = 3
        satori.state.flush(satori)

        satori2, _ = build(satori.config.state.directory.parent)
        assert satori2.state.restore(satori2, satori2.state.load())
        assert satori2.suspicion[KEY] == 42.0
        assert satori2.breakers[KEY] is not None
        assert satori2._breaker_blocks[KEY] == 3
        satori2.security.store.close()

    def test_decay_clock_restored(self, env):
        """恢复的是原始分与写入时刻——有效值按半衰期折算，不复活昨日高分。"""
        satori, _ = env
        satori.suspicion[KEY] = 100.0
        satori._ledger_ts[KEY] = time.time() - 7200  # 2 小时前
        satori.state.flush(satori)

        satori2, _ = build(satori.config.state.directory.parent)
        satori2.state.restore(satori2, satori2.state.load())
        # 半衰期 3600s，过了 2 个 → 25
        assert satori2._decayed(KEY) == pytest.approx(25.0, abs=0.5)
        satori2.security.store.close()

    def test_debounced_flush(self, env):
        satori, _ = env
        satori.state._last_flush = time.time()  # 假装刚落过盘
        satori.suspicion[KEY] = 10.0
        satori.state.mark_dirty()
        satori.state.maybe_flush(satori)
        assert not satori.state.ledger_file.exists()  # 防抖期内不写
        satori.state.flush(satori)
        assert satori.state.ledger_file.exists()

    def test_feedback_and_events_jsonl(self, env):
        satori, _ = env
        satori.state.append_feedback({"ts": 1.0, "model": "gpt-4o"})
        satori.state.append_baseline_event({"ts": 2.0, "model": "gpt-4o"})
        assert len(satori.state.read_feedback()) == 1
        assert len(satori.state.read_baseline_events()) == 1
        assert satori.state.read_baseline_events(model="other") == []

    def test_corrupt_ledger_falls_back_to_empty(self, env):
        satori, _ = env
        satori.state.ledger_file.write_text("{ 坏json", encoding="utf-8")
        assert satori.state.load() == {}


# ---- 误报反馈闭环（v4 Phase 1.1） ----

class TestFeedbackEndpoint:
    def test_requires_credential(self, env):
        _, client = env
        r = client.post("/satori/baseline/feedback",
                        json={"upstream": "openai", "model": "gpt-4o"})
        assert r.status_code == 401

    def test_bad_signature_401(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        raw = json.dumps({"upstream": "openai", "model": "gpt-4o",
                          "reason": "official_update", "confirm": True}).encode()
        r = client.post("/satori/baseline/feedback", content=raw, headers={
            H_REPORTER: "op", H_TIMESTAMP: str(int(time.time())),
            H_SIGNATURE: "0" * 64,
        })
        assert r.status_code == 401
        assert secret

    def test_unknown_target_400(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        r = post_feedback(client, secret, "op",
                          {"upstream": "ghost", "model": "nope"})
        assert r.status_code == 400

    def test_suggestion_recorded_only(self, env):
        satori, client = env
        secret = issue(client, "bob")
        satori.suspicion[KEY] = 20.0
        satori._ledger_ts[KEY] = time.time()
        r = post_feedback(client, secret, "bob", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "official_update", "confirm": False, "note": "看着像新版",
        })
        assert r.status_code == 200
        assert r.json()["action"] == "recorded"
        # 没确认：账本不动，基线不动
        assert satori.suspicion[KEY] == 20.0
        assert len(satori.state.read_baseline_events()) == 0

    def test_operator_confirm_retires_immediately(self, env):
        satori, client = env
        write_refs(satori)
        satori.baselines.recompute(*KEY)
        satori.suspicion[KEY] = 30.0
        satori._ledger_ts[KEY] = time.time()
        secret = issue(client, "op", roles=["operator"])
        r = post_feedback(client, secret, "op", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "official_update", "confirm": True,
        })
        assert r.status_code == 200
        assert r.json()["action"].startswith("retired")
        # 退役 + 账本清零 + 留痕
        assert satori.baselines.get(*KEY).level is BaselineLevel.BASIC
        assert KEY not in satori.suspicion and KEY not in satori._ledger_ts
        records = satori.state.read_feedback()
        assert records[-1]["reporter"] == "op"
        assert records[-1]["action"].startswith("retired")

    def test_cold_start_single_reporter_confirm_retires(self, env):
        satori, client = env
        write_refs(satori)
        secret = issue(client, "bob")  # 普通 reporter
        assert satori.baselines.is_cold_start("openai", "gpt-4o")  # 0 条事件
        r = post_feedback(client, secret, "bob", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "official_update", "confirm": True,
        })
        assert r.json()["action"].startswith("retired")  # 冷启动 1 次即退

    def test_normal_threshold_needs_three_confirms(self, env):
        satori, client = env
        write_refs(satori)
        seed_events(satori, 3)  # 退出冷启动
        assert not satori.baselines.is_cold_start("openai", "gpt-4o")
        secret = issue(client, "bob")
        # 每次确认带不同 note：完全相同的 confirm 请求体在窗口内算重放，
        # 只计一次（防重放兜底，见 SECURITY.md）
        def confirm(note: str):
            return post_feedback(client, secret, "bob", {
                "upstream": "openai", "model": "gpt-4o",
                "reason": "official_update", "confirm": True, "note": note,
            })
        assert confirm("第一次").json()["action"] == "confirm 1/3"
        assert confirm("第二次").json()["action"] == "confirm 2/3"
        r = confirm("第三次")
        assert r.json()["action"].startswith("retired")  # 第三笔落听

    def test_network_jitter_clears_ledger_without_retire(self, env):
        satori, client = env
        write_refs(satori)
        satori.suspicion[KEY] = 30.0
        satori._ledger_ts[KEY] = time.time()
        secret = issue(client, "bob")
        r = post_feedback(client, secret, "bob", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "network_jitter", "confirm": True,
        })
        assert r.json()["action"] == "ledger cleared"
        assert KEY not in satori.suspicion
        # 基线还在——不是官方更新就不退役
        assert (satori.config.fingerprint.reference_dir
                / "openai--gpt-4o.json").exists()
        assert satori.baselines.get(*KEY).reference == "fingerprint"

    def test_reporter_cannot_reset_breaker_but_can_feedback(self, env):
        """角色边界：feedback 提建议 reporter 足够；reset 必须 operator。"""
        satori, client = env
        secret = issue(client, "bob")
        satori.breakers[KEY] = time.time()
        r = post_feedback(client, secret, "bob", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "false_alarm", "confirm": False,
        })
        assert r.status_code == 200
        body = json.dumps({"upstream": "openai", "model": "gpt-4o"}).encode()
        r = client.post("/satori/breaker/reset", content=body, headers={
            H_REPORTER: "bob", **sign_request(secret, body),
        })
        assert r.status_code == 403  # feedback 权 ≠ reset 权

    def test_stale_timestamp_401(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        r = post_feedback(client, secret, "op", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "official_update", "confirm": True,
        }, ts=int(time.time()) - 3600)
        assert r.status_code == 401

    def test_confirm_replay_deduped_within_window(self, env):
        """防重放兜底：窗口内同 reporter 同请求体的 confirm 只计一次——
        重放合法 confirm 刷不了计数，更退役不了基线。"""
        satori, client = env
        write_refs(satori)
        seed_events(satori, 3)  # 退出冷启动，阈值 3
        secret = issue(client, "bob")
        body = {"upstream": "openai", "model": "gpt-4o",
                "reason": "official_update", "confirm": True}
        r = post_feedback(client, secret, "bob", body)
        assert r.json()["action"] == "confirm 1/3"
        # 抓包重放同一请求体：不计数、不退役
        r = post_feedback(client, secret, "bob", body)
        assert r.json()["action"] == "confirm-deduped"
        assert (satori.config.fingerprint.reference_dir
                / "openai--gpt-4o.json").exists()
        # 换 reporter 的相同内容照常计（去重键含 reporter）
        secret2 = issue(client, "carol")
        r = post_feedback(client, secret2, "carol", body)
        assert r.json()["action"] == "confirm 2/3"

    def test_admin_credential_implies_operator_for_retire(self, env):
        """角色是等级：admin 凭据天然蕴含 operator——确认即退役。"""
        satori, client = env
        write_refs(satori)
        seed_events(satori, 3)
        secret = issue(client, "boss", roles=["admin"])
        r = post_feedback(client, secret, "boss", {
            "upstream": "openai", "model": "gpt-4o",
            "reason": "official_update", "confirm": True,
        })
        assert r.json()["action"].startswith("retired")


# ---- 状态透出 ----

class TestStatusSurface:
    def test_status_baselines_section(self, env):
        satori, client = env
        write_refs(satori)
        satori.baselines.recompute_all()
        r = client.get("/satori/status")
        assert r.status_code == 200
        baselines = r.json()["baselines"]
        assert len(baselines) == 1
        entry = baselines[0]
        assert entry["upstream"] == "openai" and entry["model"] == "gpt-4o"
        assert entry["level"] == "STRICT" and entry["trust"] == 1.0
        assert entry["source"] == "official" and entry["reference"] == "fingerprint"
        assert entry["cold_start"] is True  # 0 条基线事件
        assert r.json()["security"]["enabled"] is True
