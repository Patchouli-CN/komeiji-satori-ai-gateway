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
缺凭据不炸收尾：secret 缺失时警告 + 落盘入队，测试照常收尾。
"""

from __future__ import annotations

import os
import warnings

import pytest

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
    config._satori_pending = []  # noqa: SLF001
    if reporter is None:
        return
    # 未配 upstream/model 时提示：report 端点会按配置校验目标存在性
    if not (reporter.upstream and reporter.model):
        warnings.warn(
            "satori: SATORI_URL 已配置，但缺 SATORI_UPSTREAM / SATORI_MODEL——"
            "上报会按当前配置里的上游×模型归属（留空则用 __main__ 的同名参数）",
            stacklevel=1,
        )
    # 缺签名材料的报注定发不出去（401/异常）——提前喊，别等收尾才发现
    if reporter.method == "ed25519" and not reporter.private_key:
        warnings.warn(
            "satori: ed25519 模式缺 SATORI_PRIVATE_KEY——上报将直接入队补发",
            stacklevel=1,
        )
    elif reporter.method != "ed25519" and not reporter.secret:
        warnings.warn(
            "satori: hmac 模式缺 SATORI_SECRET——上报将直接入队补发",
            stacklevel=1,
        )


def _level_of(item) -> str:
    fn = getattr(item, "obj", None) or getattr(item, "function", None)
    return getattr(fn, _REPORTER_ATTR, os.environ.get("SATORI_LEVEL", _DEFAULT_LEVEL))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """采集 call 阶段的 pass/fail 与 setup 阶段的 error。

    skip/xfail 不上报（与 satori-test-report 一致：跳过不是质量信号）；
    teardown 不采集——收尾阶段的 error 不归咎于被测上游。
    必须是新式 wrapper hook（pluggy 1.x / pytest 9 下旧式 yield hook 会
    INTERNALERROR）。
    """
    rep = yield
    reporter = getattr(item.config, "_satori_reporter", None)
    if reporter is None:
        return rep
    if rep.when == "call":
        if rep.skipped:
            return rep  # xfail/skip：不进滑窗
        status = "pass" if rep.passed else "fail"
    elif rep.when == "setup":
        if rep.skipped:
            return rep  # skip 在 setup 就定了，同样不上报
        if not rep.passed:
            status = "fail"  # setup error：环境炸了，对裁决而言等同失败
        else:
            return rep
    else:
        return rep
    longrepr = ""
    if rep.failed and rep.longrepr is not None:
        longrepr = str(rep.longrepr)[:2000]
    # doctest / 自定义 collector 的 item 未必有 module 属性
    module = getattr(item, "module", None)
    report = reporter.build_payload(
        test_suite=module.__name__ if module else "unknown",
        test_name=item.nodeid,
        status=status,
        level=_level_of(item),
        failure_diff=longrepr,
    )
    item.config._satori_pending.append(report)  # noqa: SLF001
    return rep


def pytest_sessionfinish(session, exitstatus):
    """批量补发：执行阶段零阻塞的代价是结束这一刻的尾巴。

    任何发送异常都只警告——Satori 不可达/缺凭据不许把测试收尾炸掉。
    """
    reporter = getattr(session.config, "_satori_reporter", None)
    if reporter is None:
        return
    pending = getattr(session.config, "_satori_pending", [])
    failed_this_round: list[dict] = []
    for report in pending:
        try:
            result = reporter.send(report)
        except Exception as exc:  # 签名缺材料等——入队兜底，绝不穿透收尾
            warnings.warn(
                f"satori: 上报 {report.get('test_name')} 异常（{exc!r}），已入队补发",
                stacklevel=1,
            )
            reporter._enqueue(report)  # noqa: SLF001
            failed_this_round.append(report)
            continue
        if result.action and not result.ok:
            # rejected（4xx 毒丸）由 send() 打印过警告且未入队；这里只处理入队的
            if result.action != "rejected":
                print(
                    f"[satori] 上报失败（已入队补发）：{report['test_name']} "
                    f"→ {result.status_code} {result.detail}"
                )
                failed_this_round.append(report)
    # 队列里攒着旧账的，顺手尝试补发；本轮刚入队的跳过——别同轮双发
    flushed = reporter.flush_queue(skip=failed_this_round)
    if flushed:
        ok = sum(1 for r in flushed if r.ok)
        print(f"[satori] 补发历史队列：{ok}/{len(flushed)} 成功")
