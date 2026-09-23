"""配置加载：third_eye.toml"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str  # 上游 API 根，管线在其上拼协议端点（/chat/completions、/messages）
    api_key: str
    models: list[str] = field(default_factory=list)
    # 上游协议：openai（默认，存量配置零改动）/ anthropic 原生等，由 pipelines/ 插件实现
    protocol: str = "openai"

    def resolve_key(self) -> str:
        if self.api_key.startswith("env:"):
            var = self.api_key[4:]
            key = os.environ.get(var, "")
            if not key:
                raise RuntimeError(f"upstream {self.name!r}: 环境变量 {var} 未设置")
            return key
        return self.api_key


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8400
    check_interval_seconds: int = 300
    # 前端页面来源（前后分离，页面独立部署）
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    # 流式请求自动补 stream_options.include_usage（usage 分词侧信道用）
    inject_usage: bool = True


@dataclass(frozen=True)
class FingerprintConfig:
    reference_dir: Path = Path("fingerprints")
    probe_prompt: str = "The capital of France is"
    top_logprobs: int = 10
    js_threshold: float = 0.15


@dataclass(frozen=True)
class CanaryCase:
    prompt: str
    expect: str


@dataclass(frozen=True)
class RulesConfig:
    path: Path = Path("rules.toml")
    # 可疑度累计达到该值触发告警/熔断（DEGRADED）
    suspicion_threshold: int = 50
    # 可疑度达到该值进入 WATCH（可能降级，提高关注）
    watch_threshold: int = 25
    # 账本衰减半衰期（秒）：孤立小错随时间归零，持续掺水照样积聚
    decay_half_life_seconds: int = 3600
    # 命中率通道：严重规则（score≥25）命中率超过该值告警
    hit_rate_threshold: float = 0.05
    hit_rate_min_samples: int = 50


@dataclass(frozen=True)
class RecordConfig:
    enabled: bool = False
    directory: Path = Path("records")


@dataclass(frozen=True)
class IdentityConfig:
    enabled: bool = False
    # 每轮从题组随机抽多少题（防特判）
    sample_size: int = 5
    max_tokens: int = 64


@dataclass(frozen=True)
class AnswerPrintConfig:
    enabled: bool = False
    # 与参考作答的平均相似度低于该值判异常
    similarity_threshold: float = 0.6
    max_tokens: int = 128


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    # 日志文件路径；空字符串 = 只输出控制台
    file: str = ""


@dataclass(frozen=True)
class BreakerConfig:
    # 可疑度越界后熔断：拦截该 上游×模型 的后续请求，人工复位前不放行
    enabled: bool = False


@dataclass(frozen=True)
class SecurityConfig:
    # 控制面封印授权（Gate 0 / Phase 6A+6B）。
    # enabled=false 时控制面不鉴权，启动打醒目警告——不安全必须显式选择（默认安全）
    enabled: bool = True
    # 签名模式：hmac（共享 secret）/ ed25519（非对称，多团队跨网推荐）
    mode: str = "hmac"
    # HMAC 时间戳窗口（秒），防重放
    timestamp_window_seconds: int = 300
    # SQLite 凭据库。HMAC 模式必须存 secret 原文（HMAC 验证需要 key），
    # 管好这个文件的权限；Ed25519 模式只存公钥，无此代价
    db: Path = Path("state/credentials.db")
    # Admin 接口口令，支持 env:VAR。回环地址下留空则首次启动生成一次性引导 token；
    # 非回环地址下留空将拒绝启动——拒绝带伤监听
    admin_secret: str = ""
    # v4 Phase 6F TLS 前置：None=按 host 联动（回环免 TLS、非回环强制）；
    # True/False 显式指定。只影响 /satori/* 控制面，代理路径不受影响
    require_tls: bool | None = None
    # 信任的反向代理 IP 段（CIDR）。只有这些来源的 X-Forwarded-Proto 才采信
    trusted_proxies: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in ("hmac", "ed25519"):
            raise ValueError(
                f"security.mode 必须是 'hmac' / 'ed25519'，收到 {self.mode!r}")


@dataclass(frozen=True)
class StateConfig:
    # 审计状态目录（凭据库也住这儿）。含账本/反馈/凭据，管好权限、别进 git
    directory: Path = Path("state")
    # ledger 防抖落盘间隔（秒）：突发变更合并写，重要迁移与关闭时强制 flush
    debounce_seconds: float = 1.0
    # 退役基线的归档目录（含 meta.json 留痕，replay 可翻旧账）
    archive_dir: Path = Path("archive/baselines")


@dataclass(frozen=True)
class TestingConfig:
    # 业务测试上报裁决（v4 Phase 5A）。enabled=false 时 report 端点 404 之外
    # 直接拒收——不开没有裁决的质量锚点
    enabled: bool = True
    # 负分地板：PASS 衰减不得把总分（quality+identity）压到这以下——
    # 质量信号不能把系统洗回"完全无罪"
    score_floor: float = 5.0


@dataclass(frozen=True)
class Config:
    gateway: GatewayConfig
    fingerprint: FingerprintConfig
    canary: list[CanaryCase]
    rules: RulesConfig
    record: RecordConfig
    identity: IdentityConfig
    answerprint: AnswerPrintConfig
    logging: LoggingConfig
    breaker: BreakerConfig
    upstreams: list[Upstream]
    security: SecurityConfig = field(default_factory=SecurityConfig)
    state: StateConfig = field(default_factory=StateConfig)
    testing: TestingConfig = field(default_factory=TestingConfig)

    def upstream_for(self, model: str) -> Upstream | None:
        for up in self.upstreams:
            if model in up.models:
                return up
        return self.upstreams[0] if self.upstreams else None


def load(path: str | Path = "third_eye.toml") -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))

    gw = raw.get("gateway", {})
    fp = raw.get("fingerprint", {})
    rl = raw.get("rules", {})
    rec = raw.get("record", {})
    ident = raw.get("identity", {})
    ap = raw.get("answerprint", {})
    lg = raw.get("logging", {})
    br = raw.get("breaker", {})
    sec = raw.get("security", {})
    st = raw.get("state", {})
    tst = raw.get("testing", {})

    return Config(
        gateway=GatewayConfig(
            host=gw.get("host", "127.0.0.1"),
            port=gw.get("port", 8400),
            check_interval_seconds=gw.get("check_interval_seconds", 300),
            cors_origins=gw.get("cors_origins", ["*"]),
            inject_usage=gw.get("inject_usage", True),
        ),
        fingerprint=FingerprintConfig(
            reference_dir=Path(fp.get("reference_dir", "fingerprints")),
            probe_prompt=fp.get("probe_prompt", "The capital of France is"),
            top_logprobs=fp.get("top_logprobs", 10),
            js_threshold=fp.get("js_threshold", 0.15),
        ),
        canary=[CanaryCase(**c) for c in raw.get("canary", {}).get("cases", [])],
        rules=RulesConfig(
            path=Path(rl.get("path", "rules.toml")),
            suspicion_threshold=rl.get("suspicion_threshold", 50),
            watch_threshold=rl.get("watch_threshold", 25),
            decay_half_life_seconds=rl.get("decay_half_life_seconds", 3600),
            hit_rate_threshold=rl.get("hit_rate_threshold", 0.05),
            hit_rate_min_samples=rl.get("hit_rate_min_samples", 50),
        ),
        record=RecordConfig(
            enabled=rec.get("enabled", False),
            directory=Path(rec.get("directory", "records")),
        ),
        identity=IdentityConfig(
            enabled=ident.get("enabled", False),
            sample_size=ident.get("sample_size", 5),
            max_tokens=ident.get("max_tokens", 64),
        ),
        answerprint=AnswerPrintConfig(
            enabled=ap.get("enabled", False),
            similarity_threshold=ap.get("similarity_threshold", 0.6),
            max_tokens=ap.get("max_tokens", 128),
        ),
        logging=LoggingConfig(
            level=lg.get("level", "INFO"),
            file=lg.get("file", ""),
        ),
        breaker=BreakerConfig(enabled=br.get("enabled", False)),
        upstreams=[Upstream(**u) for u in raw.get("upstreams", [])],
        security=SecurityConfig(
            enabled=sec.get("enabled", True),
            mode=sec.get("mode", "hmac"),
            timestamp_window_seconds=sec.get("timestamp_window_seconds", 300),
            db=Path(sec.get("db", "state/credentials.db")),
            admin_secret=sec.get("admin_secret", ""),
            require_tls=sec.get("require_tls"),
            trusted_proxies=list(sec.get("trusted_proxies", [])),
        ),
        state=StateConfig(
            directory=Path(st.get("directory", "state")),
            debounce_seconds=float(st.get("debounce_seconds", 1.0)),
            archive_dir=Path(st.get("archive_dir", "archive/baselines")),
        ),
        testing=TestingConfig(
            enabled=tst.get("enabled", True),
            score_floor=float(tst.get("score_floor", 5.0)),
        ),
    )
