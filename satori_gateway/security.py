"""控制面封印授权——Gate 0 / Phase 6A+6B（v4 交付顺序）。

信任链的第一轴：**谁的 PASS/FAIL 有分量、谁有资格动审计结论**。
    - TrustLevel：只作用于 test/report 打分权重（TRUSTED 可衰减/可熔断）
    - Role：控制面操作权限（reporter/operator/admin），与 trust_level 正交

两轴必须分开：信任分级解决"信不过的 reporter 不许说话"，
维度隔离（Phase 5A）解决"信得过的 reporter 也不许就质量维度给身份维度作证"。

Gate 0 范围（先于 Phase 1 与 Phase 5A 落地）：
    HMAC 模式 + SQLite 凭据存储 + Admin 签发/吊销 + 端点授权矩阵接线。
Ed25519 非对称签名与 TLS 强制留给后续波次——接口按可扩展设计，
ControlAuth.verify 的返回类型不暴露底层认证方式。

HMAC 模式的存储代价必须说清：HMAC 验证需要 key 原文，故 SQLite 里
存的是 secret 本身（0600 + 不进 list/get 响应）。Ed25519 模式只存公钥，
没有这个代价——这也是跨网场景推荐它的原因之一。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from fastapi import HTTPException, Request

log = logging.getLogger("satori")

# 控制面请求头约定（签名覆盖原始请求字节，防 canonical 化差异埋雷）
H_REPORTER = "X-Satori-Reporter"
H_TIMESTAMP = "X-Satori-Timestamp"
H_SIGNATURE = "X-Satori-Signature"
H_NONCE = "X-Satori-Nonce"
H_ADMIN = "X-Satori-Admin-Secret"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    reporter_id   TEXT PRIMARY KEY,
    trust_level   TEXT NOT NULL,
    roles         TEXT NOT NULL,          -- JSON 数组
    method        TEXT NOT NULL DEFAULT 'hmac',
    secret        TEXT NOT NULL,          -- HMAC 模式必须存原文；Ed25519 存公钥
    status        TEXT NOT NULL DEFAULT 'active',
    issued_at     REAL NOT NULL,
    last_seen_at  REAL,
    last_seen_ip  TEXT,
    revoked_at    REAL
)
"""


# ---- 6A 凭据模型 ----

class TrustLevel(str, Enum):
    """report 通道的打分权重（不控制操作权限）。"""

    TRUSTED = "trusted"
    NORMAL = "normal"
    UNVERIFIED = "unverified"

    @property
    def can_decay_suspicion(self) -> bool:
        """PASS 可否衰减嫌疑分（仍受 Phase 5A 信号轴约束）。"""
        return self is TrustLevel.TRUSTED

    @property
    def can_trigger_breaker(self) -> bool:
        """连续 FAIL 可否直接触发熔断。"""
        return self is TrustLevel.TRUSTED


class Role(str, Enum):
    """控制面操作权限（与 trust_level 正交）。"""

    REPORTER = "reporter"    # 提交 report / feedback 建议
    OPERATOR = "operator"    # feedback 确认/退役、breaker reset、ttl override
    ADMIN = "admin"          # 凭据签发/吊销/查看


# 角色是等级，不是平行标签：operator 天然是 reporter，admin 天然是一切。
# 否则"能退役基线的人反而不能提交触发退役的 feedback"——荒谬。
_ROLE_IMPLIES: dict[Role, frozenset[Role]] = {
    Role.REPORTER: frozenset({Role.REPORTER}),
    Role.OPERATOR: frozenset({Role.REPORTER, Role.OPERATOR}),
    Role.ADMIN: frozenset({Role.REPORTER, Role.OPERATOR, Role.ADMIN}),
}


def roles_satisfy(held: frozenset[Role], required: Role) -> bool:
    return any(required in _ROLE_IMPLIES[r] for r in held)


@dataclass(frozen=True)
class ControlIdentity:
    """认证通过后的调用方身份。调用方不感知底层是 HMAC 还是 Ed25519。"""

    reporter_id: str
    trust_level: TrustLevel
    roles: frozenset[Role]
    method: str


@dataclass
class Credential:
    """凭据记录。secret 只在签发响应里出现一次，list/get 永不返回。"""

    reporter_id: str
    trust_level: TrustLevel
    roles: tuple[Role, ...]
    method: str
    secret: str
    status: str = "active"
    issued_at: float = field(default_factory=time.time)
    last_seen_at: float | None = None
    last_seen_ip: str | None = None
    revoked_at: float | None = None

    def to_meta(self) -> dict:
        """元数据视图——绝不包含 secret。"""
        return {
            "reporter_id": self.reporter_id,
            "trust_level": self.trust_level.value,
            "roles": sorted(r.value for r in self.roles),
            "method": self.method,
            "status": self.status,
            "issued_at": self.issued_at,
            "last_seen_at": self.last_seen_at,
            "last_seen_ip": self.last_seen_ip,
            "revoked_at": self.revoked_at,
        }


def _parse_roles(raw: str) -> tuple[Role, ...]:
    return tuple(Role(v) for v in json.loads(raw))


def _parse_trust_level(raw: str) -> TrustLevel:
    return TrustLevel(raw)


# ---- 6B 凭据存储 ----

class CredentialStore:
    """SQLite 凭据库：签发、吊销、查询、touch。服务端只此一份真相。"""

    def __init__(self, db: Path) -> None:
        self.db = db
        db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row_to_cred(self, row: sqlite3.Row) -> Credential:
        return Credential(
            reporter_id=row["reporter_id"],
            trust_level=_parse_trust_level(row["trust_level"]),
            roles=_parse_roles(row["roles"]),
            method=row["method"],
            secret=row["secret"],
            status=row["status"],
            issued_at=row["issued_at"],
            last_seen_at=row["last_seen_at"],
            last_seen_ip=row["last_seen_ip"],
            revoked_at=row["revoked_at"],
        )

    def issue(
        self, reporter_id: str, trust_level: TrustLevel,
        roles: tuple[Role, ...], method: str = "hmac",
        public_key: str = "",
    ) -> tuple[Credential, str]:
        """签发凭据，返回 (记录, 明文)。

        hmac 模式：明文本质是共享 secret（遗失只能吊销重签）。
        ed25519 模式：secret 列存的是**公钥**（私钥从不离开 reporter），
        返回的"明文"是该公钥文本——仅供核对，泄露无损。
        """
        if method == "ed25519":
            secret = public_key
        else:
            secret = secrets.token_urlsafe(32)
        cred = Credential(
            reporter_id=reporter_id, trust_level=trust_level,
            roles=tuple(roles), method=method, secret=secret,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO credentials"
                " (reporter_id, trust_level, roles, method, secret, status, issued_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (reporter_id, trust_level.value, json.dumps([r.value for r in roles]),
                 method, secret, cred.issued_at),
            )
            self._conn.commit()
        return cred, secret

    def get(self, reporter_id: str) -> Credential | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials WHERE reporter_id = ?", (reporter_id,)
            ).fetchone()
        return self._row_to_cred(row) if row else None

    def get_active(self, reporter_id: str) -> Credential | None:
        cred = self.get(reporter_id)
        return cred if cred is not None and cred.status == "active" else None

    def list(self, status: str | None = None) -> list[Credential]:
        sql = "SELECT * FROM credentials"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY issued_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_cred(r) for r in rows]

    def revoke(self, reporter_id: str) -> bool:
        """吊销：保留记录供审计追溯，立即失效。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE credentials SET status = 'revoked', revoked_at = ?"
                " WHERE reporter_id = ? AND status = 'active'",
                (time.time(), reporter_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def touch(self, reporter_id: str, ip: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET last_seen_at = ?, last_seen_ip = ?"
                " WHERE reporter_id = ?",
                (time.time(), ip, reporter_id),
            )
            self._conn.commit()


# ---- 6B 验证器 ----

class HMACVerifier:
    """HMAC-SHA256(secret, f"{timestamp}.{body}") + 时间窗防重放。

    body 取原始请求字节——反序列化后再序列化会因 JSON canonical 差异埋雷。
    """

    def __init__(self, window_seconds: int = 300) -> None:
        self.window_seconds = window_seconds

    def verify(self, secret: str, ts: str, signature: str, body: bytes) -> bool:
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > self.window_seconds:
            return False
        expected = hmac.new(
            secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


def sign_request(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    """生成控制面请求头（供 CLI / pytest-satori 插件 / 测试复用）。"""
    ts = ts if ts is not None else int(time.time())
    signature = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        H_TIMESTAMP: str(ts),
        H_SIGNATURE: signature,
    }


# ---- 6G Ed25519 非对称模式 ----

class Ed25519Verifier:
    """ed25519 验签 + 时间窗 + nonce 一次性缓存。

    签名内容：canonical_json({reporter_id, timestamp, nonce, body_hash})。
    非对称的性质：**服务端只存公钥**——HMAC 模式必须存 secret 原文
    （验证需要 key），Ed25519 只存公钥，私钥泄露是 reporter 侧的事，
    吊销即从注册表移除，不牵动服务端存储的对称材料。
    """

    _NONCE_CACHE_MAX = 10_000

    def __init__(self, window_seconds: int = 300) -> None:
        self.window_seconds = window_seconds
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def canonical(reporter_id: str, ts: str, nonce: str, body: bytes) -> bytes:
        payload = {
            "reporter_id": reporter_id,
            "timestamp": ts,
            "nonce": nonce,
            "body_hash": hashlib.sha256(body).hexdigest(),
        }
        return json.dumps(payload, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def _nonce_fresh(self, nonce: str) -> bool:
        with self._lock:
            if nonce in self._nonces:
                return False
            if len(self._nonces) >= self._NONCE_CACHE_MAX:
                self._nonces.clear()  # 简单粗暴的有界缓存：窗口外的重放无意义
            self._nonces[nonce] = time.time()
            return True

    def verify(self, public_key_pem: str, reporter_id: str, ts: str,
               nonce: str, signature: str, body: bytes) -> bool:
        if not (public_key_pem and reporter_id and ts and nonce and signature):
            return False
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > self.window_seconds:
            return False
        if not self._nonce_fresh(nonce):
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            from cryptography.hazmat.primitives.serialization import (
                load_pem_public_key,
            )
            key = load_pem_public_key(public_key_pem.encode("utf-8"))
            if not isinstance(key, Ed25519PublicKey):
                return False
            key.verify(bytes.fromhex(signature),
                       self.canonical(reporter_id, ts, nonce, body))
            return True
        except Exception:
            return False


def generate_keypair() -> tuple[str, str]:
    """生成 Ed25519 密钥对 (私钥 PEM, 公钥 PEM)。私钥只在 reporter 侧。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("utf-8")
    public_pem = private.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    return private_pem, public_pem


def sign_request_ed25519(
    private_pem: str, reporter_id: str, body: bytes, ts: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Ed25519 模式的控制面请求头（插件 / CLI / 测试复用）。"""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    ts = ts if ts is not None else int(time.time())
    nonce = nonce or secrets.token_urlsafe(16)
    private = load_pem_private_key(private_pem.encode("utf-8"), password=None)
    message = Ed25519Verifier.canonical(reporter_id, str(ts), nonce, body)
    signature = private.sign(message).hex()
    return {
        H_TIMESTAMP: str(ts),
        H_NONCE: nonce,
        H_SIGNATURE: signature,
    }


# ---- 6B 统一认证入口 ----

def _resolve_secret(raw: str) -> str:
    """env:VAR 前缀从环境变量读取（与 Upstream.resolve_key 同一约定）。"""
    if raw.startswith("env:"):
        import os
        return os.environ.get(raw[4:], "")
    return raw


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "")


class ControlAuth:
    """控制面认证/授权总入口。verify 对调用方只给 ControlIdentity。"""

    def __init__(self, db: Path, enabled: bool = True, mode: str = "hmac",
                 admin_secret: str = "", window_seconds: int = 300,
                 host: str = "127.0.0.1", require_tls: bool | None = None,
                 trusted_proxies: list[str] | None = None) -> None:
        self.enabled = enabled
        self.mode = mode
        self.host = host
        self.verifier = HMACVerifier(window_seconds)
        self.ed25519_verifier = Ed25519Verifier(window_seconds)
        self.store = CredentialStore(db)
        self._admin_secret = _resolve_secret(admin_secret)
        self._bootstrap_secret = ""  # 回环 + 未配置 admin_secret 时首次启动生成
        # v4 Phase 6F：require_tls=None 按 host 联动——回环免 TLS，非回环强制
        self.require_tls = require_tls
        self.trusted_proxies = trusted_proxies or []

    # ---- 6F TLS 前置检查 ----

    def _tls_required(self) -> bool:
        if self.require_tls is not None:
            return self.require_tls
        return not _is_loopback(self.host)

    @staticmethod
    def _from_trusted_proxy(client_ip: str, trusted: list[str]) -> bool:
        if not client_ip or not trusted:
            return False
        if client_ip in trusted:
            return True  # 精确匹配（测试客户端/主机名场景）
        try:
            import ipaddress
            addr = ipaddress.ip_address(client_ip)
            return any(addr in ipaddress.ip_network(net, strict=False)
                       for net in trusted)
        except ValueError:
            return False

    def ensure_transport(self, request: Request) -> None:
        """TLS 前置：非 HTTPS 一律 403。回环默认豁免（本地单人场景），
        非回环默认强制；只有信任代理列表里的来源，其 X-Forwarded-Proto 才采信。"""
        if not self._tls_required():
            return
        client_ip = request.client.host if request.client else ""
        if self._from_trusted_proxy(client_ip, self.trusted_proxies):
            scheme = request.headers.get("x-forwarded-proto", "")
        else:
            scheme = request.url.scheme
        if scheme != "https":
            raise HTTPException(
                status_code=403,
                detail="控制面要求 HTTPS（security.require_tls）——"
                       "或经可信反向代理以 X-Forwarded-Proto 转发",
            )

    # ---- 启动自检 ----

    def startup_check(self) -> str | None:
        """返回需要向操作员展示的一次性提示（如引导 token），None 表示无。"""
        if not self.enabled:
            log.warning(
                "[security] 控制面鉴权已关闭（security.enabled = false）——"
                "feedback/breaker reset 等端点对任何能访问端口的人生效"
            )
            return None
        if self._admin_secret:
            return None
        if _is_loopback(self.host):
            self._bootstrap_secret = secrets.token_urlsafe(32)
            log.warning(
                "[security] 未配置 security.admin_secret，已生成一次性引导 token"
                "（仅打印这一次，重启失效）：\n"
                "    %s\n"
                "用它 POST /satori/admin/credentials 签发第一个 operator 凭据。",
                self._bootstrap_secret,
            )
            return self._bootstrap_secret
        raise RuntimeError(
            f"security.admin_secret 未配置且 host={self.host!r} 不是回环地址——"
            "拒绝带着无鉴权的控制面监听非回环地址。"
            "请配置 env:SATORI_ADMIN_SECRET，或显式设置 security.enabled = false"
        )

    # ---- 验证 ----

    async def verify(self, request: Request) -> ControlIdentity | None:
        """从请求头验证调用方身份；失败返回 None（由调用方决定 401/403）。
        按凭据的 method 分派：hmac 走共享 secret，ed25519 走公钥验签。"""
        if not self.enabled:
            return _ANONYMOUS
        reporter_id = request.headers.get(H_REPORTER, "")
        ts = request.headers.get(H_TIMESTAMP, "")
        signature = request.headers.get(H_SIGNATURE, "")
        if not (reporter_id and ts and signature):
            return None
        cred = self.store.get_active(reporter_id)
        if cred is None:
            return None
        body = await request.body()
        if cred.method == "ed25519":
            nonce = request.headers.get(H_NONCE, "")
            ok = self.ed25519_verifier.verify(
                cred.secret, reporter_id, ts, nonce, signature, body)
        else:
            ok = self.verifier.verify(cred.secret, ts, signature, body)
        if not ok:
            return None
        self.store.touch(reporter_id, request.client.host if request.client else None)
        return ControlIdentity(reporter_id, cred.trust_level,
                               frozenset(cred.roles), cred.method)

    def _admin_ok(self, provided: str) -> bool:
        if not provided:
            return False
        expected = self._admin_secret or self._bootstrap_secret
        if not expected:
            return False
        return hmac.compare_digest(provided, expected)

    # ---- FastAPI 依赖 ----

    def require(self, role: Role | None = None):
        """端点依赖：TLS 前置 + 认证 + （可选）角色校验。"""

        async def dependency(request: Request) -> ControlIdentity:
            self.ensure_transport(request)  # 6F：先问传输，再问身份
            identity = await self.verify(request)
            if identity is None:
                raise HTTPException(
                    status_code=401,
                    detail="控制面需要凭据：X-Satori-Reporter/Timestamp/Signature 三枚请求头",
                )
            if role is not None and not roles_satisfy(identity.roles, role):
                raise HTTPException(
                    status_code=403,
                    detail=f"该操作需要 {role.value} 角色（当前："
                           f"{sorted(r.value for r in identity.roles)}）",
                )
            # 挂到 request.state：handler 走路由级 dependency 时从此取身份
            # （方法签名默认值在类定义时求值，碰不到 self——别在那儿写 Depends）
            request.state.identity = identity
            return identity

        return dependency

    def require_admin(self):
        """Admin 端点依赖：TLS 前置 + admin_secret / 引导 token。"""

        async def dependency(request: Request) -> None:
            self.ensure_transport(request)
            if not self.enabled:
                return
            if not self._admin_ok(request.headers.get(H_ADMIN, "")):
                raise HTTPException(
                    status_code=401,
                    detail="Admin 接口需要 X-Satori-Admin-Secret 请求头",
                )

        return dependency

    # ---- 签发辅助 ----

    def issue_for_request(
        self, reporter_id: str, trust_level: str, roles: list[str],
        method: str | None = None, public_key: str = "",
    ) -> tuple[dict, str]:
        """校验入参并签发。非法入参抛 ValueError / 已存在抛 KeyError。"""
        method = method or self.mode
        if method not in ("hmac", "ed25519"):
            raise ValueError(f"未知 method: {method!r}（可选：hmac / ed25519）")
        if method == "ed25519" and not public_key.strip():
            raise ValueError("ed25519 模式必须提供 public_key（PEM）")
        try:
            level = TrustLevel(trust_level)
        except ValueError:
            raise ValueError(f"未知 trust_level: {trust_level!r}"
                             f"（可选：{[t.value for t in TrustLevel]}）")
        parsed: list[Role] = []
        for r in roles:
            try:
                parsed.append(Role(r))
            except ValueError:
                raise ValueError(f"未知角色: {r!r}"
                                 f"（可选：{[x.value for x in Role]}）")
        if not reporter_id or not reporter_id.strip():
            raise ValueError("reporter_id 不能为空")
        if self.store.get_active(reporter_id) is not None:
            raise KeyError(f"reporter_id {reporter_id!r} 已有有效凭据，先吊销再重签")
        if method == "ed25519":
            # 公钥必须能加载，否则签发的是一张废证
            try:
                from cryptography.hazmat.primitives.serialization import (
                    load_pem_public_key,
                )
                load_pem_public_key(public_key.encode("utf-8"))
            except Exception as exc:
                raise ValueError(f"public_key 不是合法的 PEM 公钥: {exc}") from None
        cred, secret = self.store.issue(reporter_id, level, tuple(parsed),
                                        method, public_key=public_key)
        return cred.to_meta(), secret


# 鉴权关闭时的匿名身份：显式放弃鉴权的开发模式才有，
# 启动日志会醒目警告（默认安全：不安全必须显式选择）
_ANONYMOUS = ControlIdentity(
    "anonymous", TrustLevel.TRUSTED,
    frozenset({Role.REPORTER, Role.OPERATOR, Role.ADMIN}), "disabled",
)
