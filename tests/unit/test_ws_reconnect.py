"""BaseCexAdapter WS reconnect / offline-escalation regression tests.

Reproduces the MEXC production flap: once the fast-reconnect budget was spent the
old loop re-fired API_OFFLINE + ws_offline_slow_retry on *every* retry tick and
never reset the budget after a successful reconnect.
"""
import asyncio

import pytest
from structlog.testing import capture_logs

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


async def test_offline_rotates_host_every_cycle_and_alerts():
    """An offline shard must rotate its WS host on EVERY slow-retry cycle (so it escapes a
    blocked mirror instead of sticking for tens of minutes) and escalate a long-offline
    alert once the offline duration crosses the threshold."""
    async def always_fail(idx=0):
        raise ConnectionError()

    a, hard, soft, cfg = _make(always_fail)
    cfg.ws_offline_alert_sec = 0.001          # tiny → alert after the first over-threshold cycle
    rotations = {"n": 0}
    a._rotate_ws = lambda: rotations.__setitem__("n", rotations["n"] + 1)

    with capture_logs() as logs:
        task = asyncio.create_task(a._shard_loop(0))
        await asyncio.sleep(0.05)
        a._stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    # Rotated on more than one offline cycle — not just once at escalation.
    assert rotations["n"] > 1
    # Escalated exactly once, and the sustained-offline alert fired.
    assert sum(1 for e in logs if e["event"] == "ws_offline_slow_retry") == 1
    assert any(e["event"] == "ws_shard_offline" for e in logs)


# ── P2.6: proactive connection recycle before the server's forced 24h close ──
class _IdleWs:
    """A WS that only ever times out (idle) — never delivers data or closes itself."""
    closed = False

    async def receive(self, **_kw):  # accepts the timeout kwarg base_cex passes
        raise TimeoutError

    async def send_json(self, x):
        pass

    async def send_str(self, x):
        pass


class _StepLoop:
    """Fake event loop whose clock jumps `step` seconds on every .time() call, so a few
    receive iterations deterministically cross a small recycle deadline."""
    def __init__(self, step: float):
        self._t = 0.0
        self._step = step

    def time(self) -> float:
        self._t += self._step
        return self._t


def _recv_adapter(max_conn_sec: float):
    cfg = ScannerConfig()
    cfg.ws_max_connection_sec = max_conn_sec
    cfg.ws_idle_timeout_sec = 60.0
    a = _Adapter(Settings(), cfg, sink=None, rest_url="http://x", ws_url="ws://x",
                 rate_per_sec=100, burst=100)
    a._shard_connected = [False]
    return a


async def test_planned_recycle_returns_cleanly_not_as_failure():
    """Once a connection passes ws_max_connection_sec the receive loop RETURNS (so the
    shard loop reconnects with a reset budget) instead of raising — a healthy rotation that
    pre-empts Binance's 24h server-forced 1006 close, recorded as no failure."""
    a = _recv_adapter(max_conn_sec=50.0)  # tiny cap → crossed within a few clock steps
    result = await asyncio.wait_for(
        a._ws_receive_loop(_IdleWs(), _StepLoop(step=100.0), idx=0), timeout=1.0)
    assert result is None  # clean return, no ConnectionError raised


async def test_recycle_disabled_falls_through_to_idle_reconnect():
    """With recycling disabled (0) the same idle stream instead trips the idle-timeout
    guard and raises to trigger a normal reconnect — proving the clean return above is the
    recycle path, not idle detection."""
    a = _recv_adapter(max_conn_sec=0.0)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(
            a._ws_receive_loop(_IdleWs(), _StepLoop(step=100.0), idx=0), timeout=1.0)
