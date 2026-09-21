"""pytest-satori：测试执行结果上报插件（v4 Phase 5B/6E）。

启用：
    pip install -e ".[dev]"   # 或直接装 satori-ai-gateway
    SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci-bot \
    SATORI_SECRET=... pytest -p satori_gateway.pytest_plugin

标记级别：
    @satori_test(level="L0")     # 确定性断言，N=3 阈值，PASS 衰减 10
    @satori_test(level="L1")     # 语义等价（默认）
    L3 开放式测试不计分——别标，标了也是 recorded

设计（v4 容错验证）：会话内累积，sessionfinish 批量发送——执行阶段零阻塞；
发送失败进本地队列（.satori-queue.jsonl），下次运行 flush_queue 补发。
凭据全走环境变量：SATORI_URL / SATORI_REPORTER / SATORI_SECRET（hmac）或
SATORI_PRIVATE_KEY（ed25519，配 SATORI_METHOD=ed25519）。
"""

from __future__ import annotations

import os

from .reporting import SatoriReporter

_REPORTER_ATTR = "_satori_level"
_DEFAULT_LEVEL = "L1"


def satori_test(level: str = _DEFAULT_LEVEL):
    """标记一个测试的级别（L0 确定性 / L1 语义 / L2 复杂推理）。"""
    def deco(fn):
        setattr(fn, _REPORTER_ATTR, level)
        return fn
    return deco


def pytest_configure(config):
    reporter = SatoriReporter.from_env()
    config._satori_reporter = reporter  # noqa: SLF001 — pytest 惯例：挂 config 上
    config._satori_pending = []          # noqa: SLF001
    if reporter is None:
        return
    # 未配 upstream/model 时提示：report 端点会按配置校验目标存在性
    if not (reporter.upstream and reporter.model):
        import warnings
        warnings.warn(
            "satori: SATORI_URL 已配置，但缺 SATORI_UPSTREAM / SATORI_MODEL——"
            "上报会按当前配置里的上游×模型归属（留空则用 __main__ 的同名参数）",
            stacklevel=1,
        )


def _level_of(item) -> str:
    fn = getattr(item, "obj", None) or getattr(item, "function", None)
    return getattr(fn, _REPORTER_ATTR, os.environ.get("SATORI_LEVEL", _DEFAULT_LEVEL))


def pytest_runtest_makereport(item, call):
    """采集 call 阶段结果（+ setup/teardown 的 error）。"""
    outcome = yield
    rep = outcome.get_result()
    reporter = getattr(item.config, "_satori_reporter", None)
    if reporter is None:
        return
    # 只关心：call 阶段（pass/fail）、setup 的 error/skip
    if rep.when == "call":
        status = "pass" if rep.passed else "fail"
    elif rep.when == "setup" and not rep.passed:
        status = "fail"  # setup 就挂了，对裁决而言等同失败
    else:
        return
    longrepr = ""
    if rep.failed and rep.longrepr is not None:
        longrepr = str(rep.longrepr)[:2000]
    report = reporter.build_payload(
        test_suite=item.module.__name__ if item.module else "unknown",
        test_name=item.nodeid,
        status=status,
        level=_level_of(item),
        failure_diff=longrepr,
    )
    item.config._satori_pending.append(report)  # noqa: SLF001


def pytest_sessionfinish(session, exitstatus):
    """批量补发：执行阶段零阻塞的代价是结束这一刻的尾巴。"""
    reporter = getattr(session.config, "_satori_reporter", None)
    if reporter is None:
        return
    pending = getattr(session.config, "_satori_pending", [])
    for report in pending:
        result = reporter.send(report)
        if result.action and not result.ok:
            print(f"[satori] 上报失败（已入队补发）：{report['test_name']} "
                  f"→ {result.status_code} {result.detail}")
    # 队列里攒着旧账的，顺手尝试补发
    flushed = reporter.flush_queue()
    if flushed:
        ok = sum(1 for r in flushed if r.ok)
        print(f"[satori] 补发历史队列：{ok}/{len(flushed)} 成功")
