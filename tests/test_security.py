"""Gate 0 / Phase 6A+6B：控制面封印授权测试。

覆盖信任链第一轴：谁能发言（HMAC 验签）、谁能动手（角色矩阵）、
凭据全生命周期（签发一次明文/吊销即失效/重放窗口）、两轴正交性
（trust_level 不赋予操作权限）。信号轴（Phase 5A 维度隔离）是另一组测试的事。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time

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
    Upstream,
)
from satori_gateway.security import (
    _ANONYMOUS,
    H_ADMIN,
    H_NONCE,
    H_REPORTER,
    H_SIGNATURE,
    H_TIMESTAMP,
    ControlAuth,
    CredentialStore,
    NonceCache,
    Role,
    TrustLevel,
    sign_request,
)

ADMIN = "test-admin-secret"


@pytest.fixture()
def caplog(caplog):
    """loguru 直连后 stdlib 的 "satori" logger 收不到消息——把 loguru 桥到 caplog。"""
    from loguru import logger

    handler_id = logger.add(caplog.handler, format="{message}")
    yield caplog
    logger.remove(handler_id)


def make_config(
    db, admin_secret: str = ADMIN, enabled: bool = True, host: str = "127.0.0.1"
) -> Config:
    return Config(
        gateway=GatewayConfig(host=host),
        fingerprint=FingerprintConfig(),
        canary=[CanaryCase(prompt="p", expect="e")],
        rules=RulesConfig(),
        record=RecordConfig(),
        identity=IdentityConfig(),
        answerprint=AnswerPrintConfig(),
        logging=LoggingConfig(),
        breaker=BreakerConfig(enabled=True),
        upstreams=[
            Upstream(
                name="openai", base_url="http://x/v1", api_key="k", models=["gpt-4o"]
            )
        ],
        security=SecurityConfig(enabled=enabled, db=db, admin_secret=admin_secret),
    )


@pytest.fixture()
def env(tmp_path):
    """带控制面鉴权的网关。不走 lifespan——核验循环会真的去打上游。"""
    satori = KomeijiSatori(make_config(tmp_path / "state" / "credentials.db"), [], None)
    yield satori, TestClient(satori.app)
    satori.security.store.close()


def issue(
    client: TestClient,
    reporter_id: str,
    trust: str = "normal",
    roles: list[str] | None = None,
    admin: str = ADMIN,
) -> str:
    """签发凭据，返回明文 secret。"""
    r = client.post(
        "/satori/admin/credentials",
        json={
            "reporter_id": reporter_id,
            "trust_level": trust,
            "roles": roles if roles is not None else ["reporter"],
        },
        headers={H_ADMIN: admin},
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert secret  # 明文只此一次
    return secret


def signed_post(
    client: TestClient,
    url: str,
    secret: str,
    reporter_id: str,
    body: dict | None = None,
    ts: int | None = None,
):
    """带 HMAC 三枚请求头的 POST。"""
    payload = body if body is not None else {}
    raw = json.dumps(payload).encode()
    return client.post(
        url,
        content=raw,
        headers={
            H_REPORTER: reporter_id,
            **sign_request(secret, raw, ts=ts),
        },
    )


RESET_BODY = {"upstream": "openai", "model": "gpt-4o"}


# ---- 凭据签发（6C） ----


class TestAdminIssue:
    def test_issue_returns_plaintext_once(self, env):
        _, client = env
        r = client.post(
            "/satori/admin/credentials",
            json={
                "reporter_id": "alice",
                "trust_level": "trusted",
                "roles": ["operator"],
            },
            headers={H_ADMIN: ADMIN},
        )
        assert r.status_code == 201
        assert r.headers["X-Satori-Credential-Notice"]
        meta = r.json()
        assert meta["secret"] and meta["trust_level"] == "trusted"
        assert meta["roles"] == ["operator"] and meta["status"] == "active"

    def test_list_never_returns_secret(self, env):
        _, client = env
        secret = issue(client, "alice", roles=["operator"])
        r = client.get("/satori/admin/credentials", headers={H_ADMIN: ADMIN})
        assert r.status_code == 200
        metas = r.json()["credentials"]
        assert len(metas) == 1
        assert "secret" not in json.dumps(metas)
        assert secret not in r.text

    def test_get_single_and_filters(self, env):
        _, client = env
        issue(client, "alice", roles=["operator"])
        r = client.get("/satori/admin/credentials/alice", headers={H_ADMIN: ADMIN})
        assert r.status_code == 200
        assert r.json()["reporter_id"] == "alice"
        # 过滤：不存在的 trust_level 给出空列表而非报错
        r = client.get(
            "/satori/admin/credentials?trust_level=trusted", headers={H_ADMIN: ADMIN}
        )
        assert r.json()["credentials"] == []
        r = client.get(
            "/satori/admin/credentials?status=revoked", headers={H_ADMIN: ADMIN}
        )
        assert r.json()["credentials"] == []

    def test_admin_endpoints_require_admin_secret(self, env):
        _, client = env
        body = {"reporter_id": "x", "trust_level": "normal", "roles": ["reporter"]}
        assert client.post("/satori/admin/credentials", json=body).status_code == 401
        assert (
            client.post(
                "/satori/admin/credentials", json=body, headers={H_ADMIN: "wrong"}
            ).status_code
            == 401
        )
        assert client.get("/satori/admin/credentials").status_code == 401
        assert client.get("/satori/admin/credentials/x").status_code == 401
        assert client.delete("/satori/admin/credentials/x").status_code == 401

    def test_issue_validates_input(self, env):
        _, client = env
        bad_cases = [
            ({"reporter_id": "a", "trust_level": "root", "roles": ["reporter"]}, 400),
            ({"reporter_id": "a", "trust_level": "normal", "roles": ["sudo"]}, 400),
            ({"reporter_id": "", "trust_level": "normal", "roles": ["reporter"]}, 400),
        ]
        for body, code in bad_cases:
            r = client.post(
                "/satori/admin/credentials", json=body, headers={H_ADMIN: ADMIN}
            )
            assert r.status_code == code, body
        # 重复签发：先吊销再重签
        issue(client, "dup")
        r = client.post(
            "/satori/admin/credentials",
            json={"reporter_id": "dup", "trust_level": "normal", "roles": ["reporter"]},
            headers={H_ADMIN: ADMIN},
        )
        assert r.status_code == 409


# ---- HMAC 验签（6B） ----


class TestHMACVerification:
    def test_reset_without_credentials_401(self, env):
        _, client = env
        r = client.post("/satori/breaker/reset", json=RESET_BODY)
        assert r.status_code == 401

    def test_reset_with_bad_signature_401(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        raw = json.dumps(RESET_BODY).encode()
        headers = {
            H_REPORTER: "op",
            H_TIMESTAMP: str(int(time.time())),
            H_SIGNATURE: "0" * 64,
        }
        r = client.post("/satori/breaker/reset", content=raw, headers=headers)
        assert r.status_code == 401
        assert secret  # 签名错与 secret 无关

    def test_reset_with_stale_timestamp_401(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        r = signed_post(
            client,
            "/satori/breaker/reset",
            secret,
            "op",
            RESET_BODY,
            ts=int(time.time()) - 3600,
        )
        assert r.status_code == 401  # 超出 5 分钟窗口，防重放

    def test_reset_with_unknown_reporter_401(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        r = signed_post(client, "/satori/breaker/reset", secret, "mallory", RESET_BODY)
        assert r.status_code == 401

    def test_reset_with_tampered_body_401(self, env):
        """签名覆盖原始请求体：改一个字节就必须验废。"""
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        signed = sign_request(secret, json.dumps(RESET_BODY).encode())
        tampered = json.dumps({"upstream": "openai", "model": "gpt-4o-mini"}).encode()
        r = client.post(
            "/satori/breaker/reset",
            content=tampered,
            headers={H_REPORTER: "op", **signed},
        )
        assert r.status_code == 401

    def test_signature_covers_raw_body_not_canonical(self, env):
        """客户端按自己的序列化签名，服务端按原始字节验——键序不同也要过。"""
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        raw = json.dumps(
            {"model": RESET_BODY["model"], "upstream": RESET_BODY["upstream"]}
        ).encode()  # 键序颠倒
        r = client.post(
            "/satori/breaker/reset",
            content=raw,
            headers={H_REPORTER: "op", **sign_request(secret, raw)},
        )
        assert r.status_code == 200
        assert r.json()["reset"] is False


# ---- HMAC nonce（可选，与 Ed25519 共用一次性缓存机制） ----


class TestHMACNonce:
    def test_nonce_signed_request_accepted_then_replay_rejected(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        raw = json.dumps(RESET_BODY).encode()
        headers = {H_REPORTER: "op", **sign_request(secret, raw, nonce="n-1")}
        r = client.post("/satori/breaker/reset", content=raw, headers=headers)
        assert r.status_code == 200
        # 同一 nonce 逐字节重放 → 401（nonce 一次性）
        r = client.post("/satori/breaker/reset", content=raw, headers=headers)
        assert r.status_code == 401

    def test_nonce_header_with_legacy_signature_rejected(self, env):
        """带了 nonce 头却按旧格式签名 → 验不过（不许降级回窗口重放）。"""
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        raw = json.dumps(RESET_BODY).encode()
        headers = {
            H_REPORTER: "op",
            H_NONCE: "n-x",
            **sign_request(secret, raw),
        }  # 旧格式签名
        r = client.post("/satori/breaker/reset", content=raw, headers=headers)
        assert r.status_code == 401

    def test_failed_signature_does_not_consume_nonce(self, env):
        """验签失败不占坑：废签名灌缓存 DoS 不了 nonce 防护。"""
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        raw = json.dumps(RESET_BODY).encode()
        bad = {
            H_REPORTER: "op",
            H_TIMESTAMP: str(int(time.time())),
            H_NONCE: "n-keep",
            H_SIGNATURE: "0" * 64,
        }
        assert (
            client.post("/satori/breaker/reset", content=raw, headers=bad).status_code
            == 401
        )
        good = {H_REPORTER: "op", **sign_request(secret, raw, nonce="n-keep")}
        assert (
            client.post("/satori/breaker/reset", content=raw, headers=good).status_code
            == 200
        )

    def test_cache_full_evicts_oldest_not_clear(self):
        """缓存满按最旧淘汰——整体清空等于给窗口期旧 nonce 重新放行。"""
        cache = NonceCache(max_size=3)
        for n in ("a", "b", "c"):
            cache.consume(n)
        cache.consume("d")  # 超限 → 淘汰最旧的 a
        assert cache.is_fresh("a") is True  # a 被淘汰，可再用
        assert cache.is_fresh("b") is False  # b/c/d 仍在册——不是全清
        assert cache.is_fresh("c") is False
        assert cache.is_fresh("d") is False
        assert cache.is_fresh("e") is True


# ---- 凭据库文件权限 ----


class TestCredentialStorePermissions:
    def test_db_chmod_0600(self, tmp_path):
        db = tmp_path / "c.db"
        store = CredentialStore(db)
        store.close()
        if os.name != "nt":  # Windows 的 chmod 语义有限，只验 POSIX
            assert stat.S_IMODE(db.stat().st_mode) == 0o600

    def test_chmod_failure_warns_but_store_works(self, tmp_path, monkeypatch, caplog):
        import satori_gateway.security as sec

        def boom(*args, **kwargs):
            raise OSError("read-only fs")

        monkeypatch.setattr(sec.os, "chmod", boom)
        with caplog.at_level(logging.WARNING, logger="satori"):
            store = CredentialStore(tmp_path / "c.db")
        assert any("0600" in r.getMessage() for r in caplog.records)
        cred, secret = store.issue("x", TrustLevel.NORMAL, (Role.REPORTER,))
        assert store.get_active("x").reporter_id == "x"
        store.close()


# ---- 引导 token 与配置校验 ----


class TestBootstrapAndConfig:
    def test_bootstrap_token_to_stderr_not_log(self, tmp_path, capsys, caplog):
        """引导 token 是明文凭据：只写控制台，不落持久化日志文件。"""
        auth = ControlAuth(tmp_path / "c.db", admin_secret="")
        with caplog.at_level(logging.WARNING, logger="satori"):
            token = auth.startup_check()
        assert token
        assert token in capsys.readouterr().err  # 控制台可见
        assert token not in caplog.text  # 日志里绝不留
        auth.store.close()

    def test_security_mode_validated(self, tmp_path):
        from satori_gateway.config import SecurityConfig, load

        with pytest.raises(ValueError, match="security.mode"):
            SecurityConfig(mode="md5")
        bad = tmp_path / "bad.toml"
        bad.write_text('[security]\nmode = "md5"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="security.mode"):
            load(bad)


# ---- 端点授权矩阵（6A） ----


class TestRoleMatrix:
    def test_reporter_role_cannot_reset_403(self, env):
        _, client = env
        secret = issue(client, "bob")  # 默认 reporter
        r = signed_post(client, "/satori/breaker/reset", secret, "bob", RESET_BODY)
        assert r.status_code == 403
        assert "operator" in r.json()["detail"]

    def test_trust_level_does_not_grant_roles(self, env):
        """两轴正交：TRUSTED 只影响 report 打分权重，不赋予 operator 权限。"""
        _, client = env
        secret = issue(client, "t", trust="trusted", roles=["reporter"])
        r = signed_post(client, "/satori/breaker/reset", secret, "t", RESET_BODY)
        assert r.status_code == 403

    def test_operator_role_implies_reporter(self, env):
        """角色是等级不是平行标签：operator 天然是 reporter。"""
        from satori_gateway.security import roles_satisfy

        assert roles_satisfy(frozenset({Role.OPERATOR}), Role.REPORTER)
        assert roles_satisfy(frozenset({Role.ADMIN}), Role.OPERATOR)
        assert not roles_satisfy(frozenset({Role.REPORTER}), Role.OPERATOR)

    def test_operator_can_reset_open_breaker(self, env):
        satori, client = env
        secret = issue(client, "op", roles=["operator"])
        key = ("openai", "gpt-4o")
        satori.breakers[key] = time.time()
        satori.suspicion[key] = 42.0
        r = signed_post(client, "/satori/breaker/reset", secret, "op", RESET_BODY)
        assert r.status_code == 200
        assert r.json() == {"reset": True, "upstream": "openai", "model": "gpt-4o"}
        assert key not in satori.breakers
        assert key not in satori.suspicion

    def test_reset_audit_log_records_operator(self, env, caplog):
        """复位是高危操作——审计日志必须留下操作者（对齐 ttl/identity 格式）。"""
        satori, client = env
        secret = issue(client, "op", roles=["operator"])
        satori.breakers[("openai", "gpt-4o")] = time.time()
        with caplog.at_level(logging.INFO, logger="satori"):
            r = signed_post(client, "/satori/breaker/reset", secret, "op", RESET_BODY)
        assert r.status_code == 200
        assert any(
            "复位" in rec.getMessage() and "op" in rec.getMessage()
            for rec in caplog.records
        )

    def test_revoked_credential_rejected_401(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        r = client.delete("/satori/admin/credentials/op", headers={H_ADMIN: ADMIN})
        assert r.status_code == 204
        # 吊销保留记录供审计，但立即失效
        r = client.get("/satori/admin/credentials/op", headers={H_ADMIN: ADMIN})
        assert r.status_code == 200
        assert r.json()["status"] == "revoked" and r.json()["revoked_at"]
        r = signed_post(client, "/satori/breaker/reset", secret, "op", RESET_BODY)
        assert r.status_code == 401
        # 重复吊销 404
        assert (
            client.delete(
                "/satori/admin/credentials/op", headers={H_ADMIN: ADMIN}
            ).status_code
            == 404
        )

    def test_touch_updates_last_seen(self, env):
        _, client = env
        secret = issue(client, "op", roles=["operator"])
        signed_post(client, "/satori/breaker/reset", secret, "op", RESET_BODY)
        r = client.get("/satori/admin/credentials/op", headers={H_ADMIN: ADMIN})
        assert r.json()["last_seen_at"] and r.json()["last_seen_ip"]


# ---- 分级语义与启动自检 ----


class TestTrustLevelAndStartup:
    def test_trust_level_capabilities(self):
        assert TrustLevel.TRUSTED.can_decay_suspicion is True
        assert TrustLevel.TRUSTED.can_trigger_breaker is True
        assert TrustLevel.NORMAL.can_decay_suspicion is False
        assert TrustLevel.NORMAL.can_trigger_breaker is False
        assert TrustLevel.UNVERIFIED.can_decay_suspicion is False
        assert TrustLevel.UNVERIFIED.can_trigger_breaker is False

    def test_bootstrap_token_on_loopback(self, tmp_path):
        auth = ControlAuth(tmp_path / "c.db", admin_secret="")
        token = auth.startup_check()
        assert token  # 回环 + 未配置 → 一次性引导 token
        assert auth._admin_ok(token) and not auth._admin_ok("nope")
        auth.store.close()

    def test_non_loopback_without_admin_secret_refuses(self, tmp_path):
        auth = ControlAuth(tmp_path / "c.db", admin_secret="", host="0.0.0.0")
        with pytest.raises(RuntimeError, match="admin_secret"):
            auth.startup_check()
        auth.store.close()

    def test_anonymous_identity_has_all_roles_when_disabled(self):
        # 鉴权关闭时的匿名身份：开发模式全放行——不安全必须显式选择
        assert Role.OPERATOR in _ANONYMOUS.roles
        assert Role.ADMIN in _ANONYMOUS.roles

    def test_disabled_mode_skips_auth(self, tmp_path):
        satori = KomeijiSatori(
            make_config(tmp_path / "state" / "credentials.db", enabled=False),
            [],
            None,
        )
        client = TestClient(satori.app)
        r = client.post("/satori/breaker/reset", json=RESET_BODY)
        assert r.status_code == 200  # 鉴权关闭 = 显式放弃防护
        r = client.get("/satori/status")
        assert r.json()["security"]["enabled"] is False
        satori.security.store.close()


# ---- 状态透出 ----


class TestStatusSurface:
    def test_status_exposes_security_section(self, env):
        _, client = env
        r = client.get("/satori/status")
        assert r.status_code == 200
        assert r.json()["security"] == {"enabled": True, "mode": "hmac"}
