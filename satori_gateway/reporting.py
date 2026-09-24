"""测试上报的客户端侧核心（v4 Phase 5B/6E）：fire-and-forget + 本地队列。

三方共享这一层：pytest-satori 插件、satori-test-report CLI、以及集成测试。
设计要点（v4 容错验证）：
- 凭据从环境变量读，代码零配置——Satori 不可达时测试不中断
- HMAC / Ed25519 双模式签名；**重试时重新签名**（时间戳/nonce 会变）
- 5xx / 网络异常 / 缺签名材料 → 进本地队列文件，下次运行/后台重试时补发；
  4xx（校验失败/凭据吊销）是客户端问题——丢弃并警告，毒丸不入队
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from .security import sign_request, sign_request_ed25519

DEFAULT_QUEUE_FILE = ".satori-queue.jsonl"


@dataclass
class ReportResult:
    status_code: int
    action: str
    detail: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class SatoriReporter:
    """往运行中的 Satori 上报业务测试结果。"""

    def __init__(
        self,
        url: str,
        reporter: str,
        secret: str = "",
        method: str = "hmac",
        private_key: str = "",
        queue_file: str | Path | None = None,
        timeout: float = 10.0,
        upstream: str = "",
        model: str = "",
    ) -> None:
        self.url = url
        self.reporter = reporter
        self.secret = secret
        self.method = method
        self.private_key = private_key
        self.queue_file = Path(queue_file or DEFAULT_QUEUE_FILE)
        self.timeout = timeout
        self.upstream = upstream
        self.model = model

    # ---- 环境装配 ----

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SatoriReporter | None":
        """没配 SATORI_URL 就返回 None——调用方静默跳过（不阻塞测试）。"""
        e = os.environ if env is None else env
        url = e.get("SATORI_URL", "")
        if not url:
            return None
        method = e.get("SATORI_METHOD", "hmac").lower()
        private_key = e.get("SATORI_PRIVATE_KEY", "")
        key_file = e.get("SATORI_PRIVATE_KEY_FILE", "")
        if not private_key and key_file:
            try:
                private_key = Path(key_file).read_text(encoding="utf-8")
            except OSError:
                private_key = ""
        return cls(
            url=url.rstrip("/") + "/satori/test/report",
            reporter=e.get("SATORI_REPORTER", "pytest"),
            secret=e.get("SATORI_SECRET", ""),
            method=method,
            private_key=private_key,
            queue_file=e.get("SATORI_QUEUE", DEFAULT_QUEUE_FILE),
            upstream=e.get("SATORI_UPSTREAM", ""),
            model=e.get("SATORI_MODEL", ""),
        )

    # ---- 签名与发送 ----

    def sign(self, body: bytes) -> dict[str, str]:
        if self.method == "ed25519":
            if not self.private_key:
                raise RuntimeError("ed25519 模式需要 SATORI_PRIVATE_KEY")
            return sign_request_ed25519(self.private_key, self.reporter, body)
        if not self.secret:
            raise RuntimeError("hmac 模式需要 SATORI_SECRET")
        return sign_request(self.secret, body)

    def build_payload(
        self,
        test_suite: str,
        test_name: str,
        status: str,
        level: str = "L1",
        trace_id: str | None = None,
        failure_diff: str = "",
        attempt: int = 1,
        max_attempts: int = 1,
    ) -> dict:
        return {
            "trace_id": trace_id or f"{test_suite}::{test_name}::{attempt}",
            "test_suite": test_suite,
            "test_name": test_name,
            "level": level,
            "status": status,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "failure_diff": failure_diff[:2000],
            "model_claimed": self.model,
            "upstream": self.upstream,
        }

    def send(self, report: dict) -> ReportResult:
        """同步单发。失败不抛——进队列，返回结果由调用方决定如何处理。"""
        body = json.dumps(report, ensure_ascii=False).encode()
        try:
            headers = {"X-Satori-Reporter": self.reporter, **self.sign(body)}
        except RuntimeError as exc:
            # 缺 secret / 私钥：签名都做不了——入队兜底，绝不穿透调用方收尾
            self._enqueue(report)
            return ReportResult(0, "queued", str(exc))
        try:
            r = httpx.post(
                self.url, content=body, headers=headers, timeout=self.timeout
            )
            if 400 <= r.status_code < 500:
                # 4xx 是客户端问题（400 校验失败 / 401 凭据吊销）——
                # 重试永远不会成功，毒丸不入队，直接丢弃并警告
                detail = r.text[:200]
                print(
                    f"[satori] 上报被服务端拒绝（{r.status_code}，"
                    f"不入队补发）：{detail}",
                    file=sys.stderr,
                )
                return ReportResult(r.status_code, "rejected", detail)
            if r.status_code >= 500:
                detail = r.text[:200]
                self._enqueue(report)
                return ReportResult(r.status_code, "error", detail)
            payload = {}
            try:
                payload = r.json()
            except json.JSONDecodeError:
                pass
            return ReportResult(
                r.status_code, payload.get("action", ""), payload.get("detail", "")
            )
        except httpx.HTTPError as exc:
            self._enqueue(report)  # Satori 不可达：不阻塞，排队补发
            return ReportResult(0, "queued", repr(exc))

    # ---- 本地队列 ----

    def _enqueue(self, report: dict) -> None:
        try:
            with self.queue_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(report, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 队列也写不了（磁盘满/只读）——只能丢，测试照跑

    def pending(self) -> list[dict]:
        if not self.queue_file.exists():
            return []
        out = []
        for line in self.queue_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def flush_queue(self, skip: list[dict] | None = None) -> list[ReportResult]:
        """补发队列。成功的项目从队列文件移除（重写剩余）。

        skip：本轮刚处理过（且已重新入队）的条目——补发只针对历史旧账，
        别把刚失败的项目同轮再发一遍（双发）。
        """
        pending = self.pending()
        if not pending:
            return []
        skip = skip or []
        skipped = [r for r in pending if r in skip]
        todo = [r for r in pending if r not in skip]
        results, failed = [], []
        for report in todo:
            result = self.send(report)
            results.append(result)
            if not result.ok:
                failed.append(report)
        self._rewrite_queue(skipped + failed)
        return results

    def _rewrite_queue(self, remaining: list[dict]) -> None:
        try:
            with self.queue_file.open("w", encoding="utf-8") as f:
                for report in remaining:
                    f.write(json.dumps(report, ensure_ascii=False) + "\n")
        except OSError:
            pass
