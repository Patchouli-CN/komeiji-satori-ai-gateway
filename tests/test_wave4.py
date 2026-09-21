"""收官波测试：Phase 6E/6F/6G（Ed25519 / TLS / 测试侧集成）+ Phase 5B 插件生态。"""

from __future__ import annotations

import json
import time
import types

import pytest
from fastapi.testclient import TestClient

from satori_gateway.app import KomeijiSatori
from satori_gateway.config import (
    AnswerPrintConfig,
    BreakerConfig,
    CanaryCase,
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
from satori_gateway.pytest_plugin import (
    _level_of,
    pytest_sessionfinish,
    satori_test,
)
from satori_gateway.reporting import SatoriReporter
from satori_gateway.security import (
    ControlAuth,
    Ed25519Verifier,
    H_ADMIN,
    generate_keypair,
    sign_request,
    sign_request_ed25519,
)
from satori_gateway.test_mock import create_app

from test_phase1 import ADMIN, KEY, build, env, issue


# ---- 6G Ed25519 ----

class TestEd25519:
    def test_sign_verify_roundtrip(self, tmp_path):
        private_pem, public_pem = generate_keypair()
        assert "PRIVATE KEY" in private_pem and "PUBLIC KEY" in public_pem
        verifier = Ed25519Verifier()
        body = json.dumps({"x": 1}).encode()
        headers = sign_request_ed25519(private_pem, "bot", body)
        assert verifier.verify(public_pem, "bot", headers["X-Satori-Timestamp"],
                               headers["X-Satori-Nonce"],
                               headers["X-Satori-Signature"], body) is True

    def test_wrong_key_rejected(self, tmp_path):
        priv_a, pub_a = generate_keypair()
        _, pub_b = generate_keypair()
        verifier = Ed25519Verifier()
        body = b"{}"
        headers = sign_request_ed25519(priv_a, "bot", body)
        assert verifier.verify(pub_b, "bot", headers["X-Satori-Timestamp"],
                               headers["X-Satori-Nonce"],
                               headers["X-Satori-Signature"], body) is False

    def test_nonce_replay_rejected(self):
        priv, pub = generate_keypair()
        verifier = Ed25519Verifier()
        body = b"{}"
        headers = sign_request_ed25519(priv, "bot", body, nonce="fixed-nonce")
        assert verifier.verify(pub, "bot", headers["X-Satori-Timestamp"],
                               "fixed-nonce",
                               headers["X-Satori-Signature"], body) is True
        # 同一 nonce 再来一遍 → 重放拒绝
        assert verifier.verify(pub, "bot", headers["X-Satori-Timestamp"],
                               "fixed-nonce",
                               headers["X-Satori-Signature"], body) is False

    def test_stale_timestamp_rejected(self):
        priv, pub = generate_keypair()
        verifier = Ed25519Verifier()
        body = b"{}"
        headers = sign_request_ed25519(priv, "bot", body,
                                       ts=int(time.time()) - 3600)
        assert verifier.verify(pub, "bot", headers["X-Satori-Timestamp"],
                               headers["X-Satori-Nonce"],
                               headers["X-Satori-Signature"], body) is False

    def test_both_methods_coexist(self, env):
        """HMAC 与 Ed25519 同一实例共存：不同 reporter 用不同模式。"""
        satori, client = env
        priv, pub = generate_keypair()
        # admin 签一个 ed25519 operator（公钥入册）
        r = client.post("/satori/admin/credentials",
                        json={"reporter_id": "ed-op", "trust_level": "trusted",
                              "roles": ["operator"], "method": "ed25519",
                              "public_key": pub},
                        headers={H_ADMIN: ADMIN})
        assert r.status_code == 201
        # hmac operator 也签一个
        issue(client, "hmac-op", roles=["operator"])

        body = json.dumps({"upstream": "openai", "model": "gpt-4o"}).encode()
        # ed25519 凭据的请求：验签通过
        r = client.post("/satori/breaker/reset", content=body, headers={
            "X-Satori-Reporter": "ed-op",
            **sign_request_ed25519(priv, "ed-op", body)})
        assert r.status_code == 200
        # 公钥模式下私钥签名错乱 → 401
        priv2, _ = generate_keypair()
        r = client.post("/satori/breaker/reset", content=body, headers={
            "X-Satori-Reporter": "ed-op",
            **sign_request_ed25519(priv2, "ed-op", body)})
        assert r.status_code == 401
        # hmac 老模式不受影响
        secret = issue(client, "hmac-op2", roles=["operator"])
        r = client.post("/satori/breaker/reset", content=body, headers={
            "X-Satori-Reporter": "hmac-op2", **sign_request(secret, body)})
        assert r.status_code == 200

    def test_issue_requires_public_key_for_ed25519(self, env):
        _, client = env
        r = client.post("/satori/admin/credentials",
                        json={"reporter_id": "x", "trust_level": "normal",
                              "roles": ["reporter"], "method": "ed25519"},
                        headers={H_ADMIN: ADMIN})
        assert r.status_code == 400 and "public_key" in r.json()["detail"]
        # 坏公钥 → 400
        r = client.post("/satori/admin/credentials",
                        json={"reporter_id": "x", "trust_level": "normal",
                              "roles": ["reporter"], "method": "ed25519",
                              "public_key": "not-a-pem"},
                        headers={H_ADMIN: ADMIN})
        assert r.status_code == 400 and "PEM" in r.json()["detail"]


# ---- 6F TLS 前置 ----

class TestTlsGate:
    def _reset_body(self):
        return json.dumps({"upstream": "openai", "model": "gpt-4o"}).encode()

    def test_loopback_exempt_by_default(self, env):
        _, client = env  # host 127.0.0.1 → 免 TLS
        secret = issue(client, "op", roles=["operator"])
        r = client.post("/satori/breaker/reset", content=self._reset_body(),
                        headers={"X-Satori-Reporter": "op",
                                 **sign_request(secret, self._reset_body())})
        assert r.status_code == 200

    def test_require_tls_blocks_http(self, tmp_path):
        satori, client = build(tmp_path)
        secret = issue(client, "op", roles=["operator"])  # 先签凭据（回环免 TLS）
        satori.security.require_tls = True                # 再打开 TLS 前置
        r = client.post("/satori/breaker/reset", content=self._reset_body(),
                        headers={"X-Satori-Reporter": "op",
                                 **sign_request(secret, self._reset_body())})
        assert r.status_code == 403
        assert "HTTPS" in r.json()["detail"]

    def test_forwarded_proto_only_from_trusted_proxy(self, tmp_path):
        satori, client = build(tmp_path)
        secret = issue(client, "op", roles=["operator"])
        satori.security.require_tls = True
        satori.security.trusted_proxies = ["testclient"]  # TestClient 的 client host
        body = self._reset_body()
        headers = {"X-Satori-Reporter": "op",
                   **sign_request(secret, body)}
        # 不信任的来源自称 https：不采信 → 403
        r = client.post("/satori/breaker/reset", content=body, headers=headers)
        assert r.status_code == 403
        # 信任代理转发来的 https：放行
        r = client.post("/satori/breaker/reset", content=body,
                        headers={**headers, "X-Forwarded-Proto": "https"})
        assert r.status_code == 200

    def test_cache_control_on_credential_response(self, env):
        _, client = env
        r = client.post("/satori/admin/credentials",
                        json={"reporter_id": "cc", "trust_level": "normal",
                              "roles": ["reporter"]},
                        headers={H_ADMIN: ADMIN})
        assert r.headers.get("cache-control") == "no-store"


# ---- 5B/6E 上报核心 ----

class TestReporter:
    def _payload(self):
        return SatoriReporter(url="http://mock/satori/test/report",
                              reporter="bot", secret="s3cret",
                              upstream="openai", model="gpt-4o")

    def test_build_payload_shape(self):
        report = self._payload().build_payload(
            test_suite="core", test_name="test_add", status="fail",
            level="L0", failure_diff="boom")
        assert report["trace_id"] == "core::test_add::1"
        assert report["level"] == "L0" and report["status"] == "fail"
        assert report["upstream"] == "openai" and report["model_claimed"] == "gpt-4o"

    def test_from_env_absent_returns_none(self):
        assert SatoriReporter.from_env({}) is None

    def test_from_env_hmac(self):
        r = SatoriReporter.from_env({
            "SATORI_URL": "http://127.0.0.1:8400", "SATORI_REPORTER": "ci",
            "SATORI_SECRET": "x", "SATORI_UPSTREAM": "openai",
            "SATORI_MODEL": "gpt-4o"})
        assert r is not None and r.url.endswith("/satori/test/report")

    def test_from_env_ed25519(self):
        priv, _ = generate_keypair()
        r = SatoriReporter.from_env({
            "SATORI_URL": "http://x", "SATORI_METHOD": "ed25519",
            "SATORI_PRIVATE_KEY": priv})
        assert r is not None and r.method == "ed25519"

    def test_send_success_against_mock(self, tmp_path):
        from satori_gateway.reporting import ReportResult

        reporter = self._payload()
        mock = TestClient(create_app(secret="s3cret",
                                     store_file=tmp_path / "m.jsonl"))
        report = reporter.build_payload("core", "t1", "fail", "L0")
        raw = json.dumps(report, ensure_ascii=False).encode()
        r = mock.post("/satori/test/report", content=raw, headers={
            "X-Satori-Reporter": "bot", **reporter.sign(raw)})
        assert r.status_code == 200 and r.json()["accepted"] is True
        # 错签名 → 401
        r = mock.post("/satori/test/report", content=raw, headers={
            "X-Satori-Reporter": "bot", "X-Satori-Timestamp": str(int(time.time())),
            "X-Satori-Signature": "0" * 64})
        assert r.status_code == 401
        assert ReportResult(200, "ok", "").ok is True

    def test_queue_on_connection_error(self, tmp_path, monkeypatch):
        """Satori 不可达：连不上 → 进队列，测试不中断（容错验证）。"""
        import httpx as _httpx

        def boom(*a, **kw):
            raise _httpx.ConnectError("refused")

        monkeypatch.setattr(_httpx, "post", boom)
        reporter = self._payload()
        reporter.queue_file = tmp_path / "q.jsonl"
        result = reporter.send(reporter.build_payload("core", "t", "fail"))
        assert result.ok is False and result.action == "queued"
        assert len(reporter.pending()) == 1

    def test_queue_on_server_error(self, tmp_path, monkeypatch):
        """Satori 在但报 5xx：同样入队补发，不当场炸。"""
        import httpx as _httpx

        class FakeResp:
            status_code = 502
            text = "bad gateway"

            def json(self):
                raise json.JSONDecodeError("", "", 0)

        monkeypatch.setattr(_httpx, "post", lambda *a, **kw: FakeResp())
        reporter = self._payload()
        reporter.queue_file = tmp_path / "q2.jsonl"
        result = reporter.send(reporter.build_payload("core", "t", "fail"))
        assert result.ok is False and result.status_code == 502
        assert len(reporter.pending()) == 1

    def test_flush_queue_removes_delivered(self, tmp_path, monkeypatch):
        from satori_gateway.reporting import ReportResult

        reporter = self._payload()
        reporter.queue_file = tmp_path / "q.jsonl"
        for i in range(2):  # 预置两条队列
            reporter._enqueue({"trace_id": f"t{i}", "test_suite": "core",
                               "test_name": f"t{i}", "level": "L1",
                               "status": "fail"})
        monkeypatch.setattr(
            reporter, "send",
            lambda report: ReportResult(200, "recorded", ""))
        results = reporter.flush_queue()
        assert len(results) == 2 and all(r.ok for r in results)
        assert reporter.pending() == []  # 交付即出队


# ---- CLI 解析（junit / tap / json） ----

class TestReportCliParsers:
    JUNIT = """<?xml version="1.0"?>
    <testsuite name="core" tests="3">
      <testcase classname="core" name="test_a"/>
      <testcase classname="core" name="test_b">
        <failure message="boom">trace here</failure>
      </testcase>
      <testcase classname="core" name="test_c"><skipped/></testcase>
    </testsuite>"""

    TAP = """TAP version 13
1..3
ok 1 - test_a
not ok 2 - test_b
  ---
  message: 'expected 1 got 2'
  ...
ok 3 - test_c # SKIP 环境不支持
"""

    def test_junit(self, tmp_path):
        from satori_gateway.test_report_cli import parse_junit
        p = tmp_path / "r.xml"
        p.write_text(self.JUNIT, encoding="utf-8")
        cases = parse_junit(str(p))
        assert [c["name"] for c in cases] == ["test_a", "test_b", "test_c"]
        assert [c["status"] for c in cases] == ["pass", "fail", "skip"]
        assert "boom" in cases[1]["detail"] or "trace" in cases[1]["detail"]
        assert cases[0]["suite"] == "core"

    def test_tap(self, tmp_path):
        from satori_gateway.test_report_cli import parse_tap
        p = tmp_path / "r.tap"
        p.write_text(self.TAP, encoding="utf-8")
        cases = parse_tap(str(p))
        assert [c["name"] for c in cases] == ["test_a", "test_b", "test_c"]
        assert [c["status"] for c in cases] == ["pass", "fail", "skip"]
        assert "expected 1" in cases[1]["detail"]

    def test_json(self, tmp_path):
        from satori_gateway.test_report_cli import parse_json
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"tests": [
            {"name": "a", "status": "passed"},
            {"name": "b", "status": "failed", "detail": "x"},
        ]}), encoding="utf-8")
        cases = parse_json(str(p))
        assert [c["status"] for c in cases] == ["pass", "fail"]
        assert cases[1]["detail"] == "x"

    def test_detect_format(self, tmp_path):
        from satori_gateway.test_report_cli import detect_format
        x = tmp_path / "a.xml"; x.write_text("<?xml version='1.0'><testsuite/>")
        t = tmp_path / "a.tap"; t.write_text("1..1\nok 1 - x")
        j = tmp_path / "a.json"; j.write_text("[]")
        assert detect_format(str(x)) == "junit"
        assert detect_format(str(t)) == "tap"
        assert detect_format(str(j)) == "json"


# ---- mock server ----

class TestMockServer:
    def test_accepts_and_records(self, tmp_path):
        client = TestClient(create_app(store_file=tmp_path / "m.jsonl"))
        r = client.post("/satori/test/report",
                        json={"trace_id": "t", "test_name": "x",
                              "status": "fail", "level": "L0"})
        assert r.status_code == 200 and r.json()["accepted"] is True
        reports = client.get("/satori/test/reports").json()
        assert reports["count"] == 1

    def test_reject_mode_500(self):
        client = TestClient(create_app(reject=True))
        r = client.post("/satori/test/report", json={"x": 1})
        assert r.status_code == 500
        assert client.get("/satori/test/reports").json()["rejected"] == 1


# ---- pytest 插件 ----

class TestPytestPlugin:
    def test_decorator_sets_level(self):
        @satori_test(level="L0")
        def fn():
            pass
        assert _level_of(types.SimpleNamespace(obj=fn)) == "L0"

    def test_default_level_l1(self):
        fn = lambda: None  # noqa: E731
        assert _level_of(types.SimpleNamespace(obj=fn)) == "L1"

    def test_sessionfinish_sends_pending(self, monkeypatch):
        from satori_gateway.reporting import ReportResult

        reporter = SatoriReporter(url="http://mock/satori/test/report",
                                  reporter="bot", secret="s")
        sent: list[dict] = []
        monkeypatch.setattr(
            reporter, "send",
            lambda report: (sent.append(report),
                            ReportResult(200, "recorded", ""))[1])
        config = types.SimpleNamespace(
            _satori_reporter=reporter,
            _satori_pending=[reporter.build_payload("core", "t", "fail")])
        session = types.SimpleNamespace(config=config)
        pytest_sessionfinish(session, 0)
        assert len(sent) == 1 and sent[0]["status"] == "fail"
