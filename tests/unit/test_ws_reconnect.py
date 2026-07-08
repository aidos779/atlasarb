"""BaseCexAdapter WS reconnect / offline-escalation regression tests.

Reproduces the MEXC production flap: once the fast-reconnect budget was spent the
old loop re-fired API_OFFLINE + ws_offline_slow_retry on *every* retry tick and
never reset the budget after a successful reconnect.
"""
import asyncio

import pytest

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.scanner.adapters.base_cex import BaseCexAdapter

pytestmark = pytest.mark.asyncio


class _Adapter(BaseCexAdapter):
    id = "fake"
    display_name = "fake"

    async def _fetch_markets(self, session):
        return []

    def _subscribe_frames(self, symbols):
        return []

    def _parse_message(self, message):
        return []

    async def _fetch_funding(self, session, base_asset):
        return None


def _make(behavior):
    cfg = ScannerConfig()
    cfg.ws_fast_reconnect_max = 3
    cfg.ws_backoff_base_sec = 0.0
    cfg.ws_backoff_cap_sec = 0.0
    cfg.ws_slow_retry_interval_sec = 0.0
    hard: list[int] = []
    soft: list[int] = []
    a = _Adapter(Settings(), cfg, sink=None, rest_url="http://x", ws_url="ws://x",
                 rate_per_sec=100, burst=100)

    def on_failure(hard: bool = False) -> None:  # noqa: A002 — matches adapter hook
        (hard_calls if hard else soft).append(1)

    hard_calls = hard

    a._on_failure = on_failure
    # single shard (index 0) driven by the injected behavior
    a._shards = [set()]
    a._shard_ws = [None]
    a._shard_tasks = [None]
    a._shard_connected = [False]
    a._run_shard = behavior
    return a, hard, soft, cfg


async def test_offline_escalates_once_not_every_tick():
    calls = {"n": 0}

    async def always_fail(idx=0):
        calls["n"] += 1
        raise ConnectionError()  # empty str -> also exercises describe_exc

    a, hard, soft, cfg = _make(always_fail)
    task = asyncio.create_task(a._shard_loop(0))
    await asyncio.sleep(0.05)
    a._stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    # Escalated to hard-offline exactly once despite many retry ticks.
    assert calls["n"] > cfg.ws_fast_reconnect_max
    assert sum(hard) == 1, f"hard offline fired {sum(hard)}x, expected 1"


async def test_successful_reconnect_resets_budget():
    # Connect ok a few times, then fail: failures after a good connect must not
    # immediately escalate (budget was reset on connect).
    state = {"phase": 0}

    async def flap(idx=0):
        state["phase"] += 1
        a._shard_connected[0] = True  # simulate a real connect
        if state["phase"] <= 5:
            raise ConnectionError("drop after connect")
        await asyncio.sleep(0.001)

    a, hard, soft, cfg = _make(flap)
    task = asyncio.create_task(a._shard_loop(0))
    await asyncio.sleep(0.05)
    a._stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    # Every drop followed a successful connect -> budget reset each time ->
    # never escalated to hard offline.
    assert sum(hard) == 0
