"""配置加载：third_eye.toml"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str  # OpenAI 兼容的 /v1 根
    api_key: str
    models: list[str] = field(default_factory=list)

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
    )
