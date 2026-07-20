"""Hedged RPC failover (base_dex.rpc_call): a slow/timing-out provider must not set the
call's latency floor, and a single call must try at most rpc_max_providers_per_call
providers so the shared event loop is never blocked for len(providers) x timeout.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from src.config.scanner_config import ScannerConfig
from src.scanner.adapters.base_dex import BaseDexAdapter
from src.scanner.adapters.rpc_pool import RpcErrorKind


class _Dex(BaseDexAdapter):
    async def _list_pools(self):
        return []

    async def _read_pool(self, symbol):
        return None


class _Resp:
    def __init__(self, status=200, payload=None, text="", sleep=0.0):
        self.status = status
        self._payload = payload if payload is not None else {"result": "0x1"}
        self._text = text
        self._sleep = sleep

    async def __aenter__(self):
        if self._sleep:
            await asyncio.sleep(self._sleep)  # a losing racer is cancelled mid-sleep
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, handlers):
        self._handlers = handlers  # url -> () -> _Resp

    def post(self, url, json=None, timeout=None):
        return self._handlers[url]()


class _StubPool:
    def __init__(self, order):
        self._order = order
        self.successes: list[str] = []
        self.failures: list[tuple[str, RpcErrorKind]] = []

    def order(self):
        return list(self._order)

    def record_success(self, url, latency_ms=0.0):
        self.successes.append(url)

    def record_failure(self, url, kind):
        self.failures.append((url, kind))

    def ewma_latency_ms(self, url):
        return 0.0  # unknown latency -> transport keeps the base deadline

    def snapshot(self):
        return []


def _adapter(order, handlers, **cfg_kw):
    cfg = ScannerConfig(**cfg_kw)
    settings = SimpleNamespace(rpc_urls_for=lambda net: list(order),
                               rpc_primary_urls_for=lambda net: set())
    a = _Dex(settings, cfg, sink=SimpleNamespace(), network="ethereum")
    a._session = _FakeSession(handlers)
    a._rpc_pool = _StubPool(order)
    return a


def test_slow_provider_does_not_block_the_call():
    # "slow" hangs for 10s; "fast" answers instantly. With hedge=2 they race, so the call
    # returns via "fast" in well under the slow provider's delay.
    handlers = {
        "slow": lambda: _Resp(sleep=10.0),
        "fast": lambda: _Resp(payload={"result": "0xFAST"}),
    }
    a = _adapter(["slow", "fast"], handlers, rpc_hedge_factor=2)
    started = time.perf_counter()
    result = asyncio.run(a.rpc_call("eth_blockNumber", []))
    elapsed = time.perf_counter() - started
    assert result == "0xFAST"
    assert elapsed < 2.0, f"hedging did not overlap: took {elapsed:.1f}s"
    assert a._rpc_pool.successes == ["fast"]
    # The slow racer was cancelled, not counted as a provider failure.
    assert a._rpc_pool.failures == []


def test_all_providers_fail_returns_none_and_records_each():
    handlers = {u: (lambda: _Resp(status=500)) for u in ("a", "b", "c")}
    a = _adapter(["a", "b", "c"], handlers, rpc_hedge_factor=2)
    result = asyncio.run(a.rpc_call("eth_blockNumber", []))
    assert result is None
    assert {u for u, _ in a._rpc_pool.failures} == {"a", "b", "c"}


def test_call_is_capped_to_max_providers_per_call():
    order = [f"p{i}" for i in range(10)]
    handlers = {u: (lambda: _Resp(status=500)) for u in order}
    a = _adapter(order, handlers, rpc_hedge_factor=2, rpc_max_providers_per_call=6)
    result = asyncio.run(a.rpc_call("eth_blockNumber", []))
    assert result is None
    # Only the first 6 (best-ranked) providers are attempted, bounding worst-case latency.
    assert len(a._rpc_pool.failures) == 6


def test_pure_timeout_streak_trips_breaker_at_double_threshold():
    """Timeouts on a public node are usually latency spikes, not dead endpoints — a
    pure-timeout streak must survive fail_threshold strikes and only trip at 2x, while
    any harder failure in the streak keeps the fast threshold (the anti-flapping fix)."""
    from src.scanner.adapters.rpc_pool import RpcProviderPool

    pool = RpcProviderPool(urls=["u"], fail_threshold=5, network="ethereum")
    for _ in range(5):
        pool.record_failure("u", RpcErrorKind.TIMEOUT)
    assert pool.healthy_count() == (1, 1)          # still in rotation at base threshold
    for _ in range(5):
        pool.record_failure("u", RpcErrorKind.TIMEOUT)
    assert pool.healthy_count() == (0, 1)          # tripped at 2x

    # A hard block mixed into the streak keeps the fast threshold.
    pool2 = RpcProviderPool(urls=["u"], fail_threshold=5, network="ethereum")
    pool2.record_failure("u", RpcErrorKind.HTTP)
    for _ in range(4):
        pool2.record_failure("u", RpcErrorKind.TIMEOUT)
    assert pool2.healthy_count() == (0, 1)         # tripped at base threshold

    # A success fully resets the streak character.
    pool3 = RpcProviderPool(urls=["u"], fail_threshold=5, network="ethereum")
    pool3.record_failure("u", RpcErrorKind.HTTP)
    pool3.record_success("u", latency_ms=100)
    for _ in range(5):
        pool3.record_failure("u", RpcErrorKind.TIMEOUT)
    assert pool3.healthy_count() == (1, 1)         # pure-timeout streak again
