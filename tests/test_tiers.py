"""三档动作固化测试（v4 Phase 1.3）：档次不是仪表读数，是油门。

STRICT 全通道 / STANDARD 身份通道半额记账 / BASIC 身份通道闭嘴。
重点覆盖 BASIC 的边缘漏洞：**参考文件还在但 trust 跌穿 0.4**（重度老化）
时，声纹通道必须显式闭嘴——不能依赖"参考文件刚好不在"的隐式跳过。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from satori_gateway.app import KomeijiSatori
from satori_gateway.baseline import (
    BaselineLevel,
    BaselineState,
    IDENTITY_CHECKER_WEIGHTS,
)
from satori_gateway.checkers import CheckResult

from test_phase1 import KEY, build, env, write_refs


class CountingFingerprint:
    """会数自己被调了几次的假声纹 checker（永远失败，score=0.5）。"""

    name = "fingerprint"

    def __init__(self):
        self.calls = 0

    async def check(self, client, upstream, model):
        self.calls += 1
        return CheckResult("fingerprint", upstream.name, model, False, 0.5,
                           "JS 散度 0.5000（阈值 0.15）")


def _identity_weight_unit():
    st = BaselineState(upstream="u", model="m")
    st.level = BaselineLevel.STRICT
    assert st.identity_weight("fingerprint") == 20.0
    st.level = BaselineLevel.STANDARD
    assert st.identity_weight("fingerprint") == 10.0
    st.level = BaselineLevel.BASIC
    assert st.identity_weight("fingerprint") == 0.0
    # answerprint 同理半价
    st.level = BaselineLevel.STANDARD
    assert st.identity_weight("answerprint") == 5.0
    # 非身份通道没有权重概念
    assert st.identity_weight("canary") == 0.0


def test_identity_weight_by_tier():
    _identity_weight_unit()


class TestTierActions:
    def test_strict_full_weight(self, env):
        satori, _ = env
        write_refs(satori, source="official", pressure="LOW")  # trust≈1
        satori.baselines.recompute(*KEY)
        assert satori.baselines.get(*KEY).level is BaselineLevel.STRICT
        fake = CountingFingerprint()
        satori.checkers = [fake]
        asyncio.run(satori.run_all_checks(None))
        assert fake.calls == 1
        assert satori._decayed_identity(KEY) == pytest.approx(20.0, abs=0.01)

    def test_standard_half_weight(self, env):
        """二手来源 → trust 0.5 → STANDARD：身份通道半额记账。"""
        satori, _ = env
        write_refs(satori, source="secondhand", pressure="LOW")
        satori.baselines.recompute(*KEY)
        assert satori.baselines.get(*KEY).level is BaselineLevel.STANDARD
        fake = CountingFingerprint()
        satori.checkers = [fake]
        asyncio.run(satori.run_all_checks(None))
        assert fake.calls == 1  # 照常探
        assert satori._decayed_identity(KEY) == pytest.approx(10.0, abs=0.01)

    def test_basic_aged_reference_present_stays_silent(self, env):
        """边缘漏洞固化：参考文件还在，但 trust 跌穿 0.4 → BASIC 显式闭嘴。"""
        satori, _ = env
        write_refs(satori, source="official", pressure="LOW",
                   collected_at=time.time() - 86400 * 20)  # 20/30 天 → trust≈0.19
        satori.baselines.recompute(*KEY)
        st = satori.baselines.get(*KEY)
        assert st.level is BaselineLevel.BASIC and st.reference == "fingerprint"
        fake = CountingFingerprint()
        satori.checkers = [fake]
        asyncio.run(satori.run_all_checks(None))
        assert fake.calls == 0  # 连探针都不发——不诬告，也不花 token
        assert satori._decayed(KEY) == 0.0

    def test_basic_after_retirement_stays_silent(self, env):
        satori, _ = env
        write_refs(satori)
        satori.baselines.recompute(*KEY)
        satori.baselines.retire("openai", "gpt-4o", reason="official_update",
                                reporter="op")
        fake = CountingFingerprint()
        satori.checkers = [fake]
        asyncio.run(satori.run_all_checks(None))
        assert fake.calls == 0
        assert satori._decayed(KEY) == 0.0

    def test_no_reference_stays_silent(self, env):
        """无参考（从未采集）：原版行为不变——checker 自己会报无参考。"""
        satori, _ = env
        satori.baselines.recompute(*KEY)
        fake = CountingFingerprint()
        satori.checkers = [fake]
        asyncio.run(satori.run_all_checks(None))
        assert fake.calls == 0  # BASIC 门控在 checker 之前就拦了
        assert satori._decayed(KEY) == 0.0

    def test_quality_channels_run_in_basic(self, env):
        """BASIC 只闭嘴身份通道——黑盒 checker（canary 等）照常在岗。"""
        satori, _ = env
        write_refs(satori, collected_at=time.time() - 86400 * 20)
        satori.baselines.recompute(*KEY)

        class FakeCanary:
            name = "canary"

            async def check(self, client, upstream, model):
                return CheckResult("canary", upstream.name, model, True, 0.0, "ok")

        satori.checkers = [CountingFingerprint(), FakeCanary()]
        asyncio.run(satori.run_all_checks(None))
        # canary 跑过并留痕；声纹被 BASIC 拦在门外
        assert satori.results[("canary", "openai", "gpt-4o")].ok is True
        assert ("fingerprint", "openai", "gpt-4o") not in satori.results

    def test_weights_single_source_of_truth(self):
        """app 的别名与 baseline 的常量同源——防止两边岁久走偏。"""
        from satori_gateway.app import _IDENTITY_CHECKERS
        assert _IDENTITY_CHECKERS is IDENTITY_CHECKER_WEIGHTS
