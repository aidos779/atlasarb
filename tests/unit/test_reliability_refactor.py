"""Regression tests for the production-readiness refactor:

- LogThrottle aggregates repeated identical events;
- RestError raised on non-200 without any JSON decoding (no rest_json_decode_failed);
- RPC provider pool: isolated failures never disable a provider, ranking prefers
  the healthiest endpoint, and cooled-down providers recover on one success;
- Binance proxy chain rotation.
"""
from __future__ import annotations

import pytest

from src.config.logging import LogThrottle
from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.scanner.adapters.cex.binance import BinanceAdapter
from src.scanner.adapters.errors import RestError
from src.scanner.adapters.rpc_pool import RpcErrorKind, RpcProviderPool

# ── LogThrottle ──

def test_log_throttle_first_emits_then_aggregates():
    th = LogThrottle(interval_sec=60.0)
    emit, suppressed = th.allow("k", now=0.0)
    assert emit and suppressed == 0
    # Within the window: suppressed.
    for i in range(5):
        emit, _ = th.allow("k", now=10.0 + i)
        assert not emit
    # Window elapsed: emits again and reports how many were swallowed.
    emit, suppressed = th.allow("k", now=61.0)
    assert emit and suppressed == 5


def test_log_throttle_keys_are_independent():
    th = LogThrottle(interval_sec=60.0)
    assert th.allow("a", now=0.0)[0]
    assert th.allow("b", now=1.0)[0]
    assert not th.allow("a", now=2.0)[0]


def test_ws_flap_is_collapsed_but_distinct_close_codes_pass():
    """A flapping shard (many 1006 reconnects in one window) logs once with a
    suppressed count, while a rare policy/auth close code is never hidden."""
    th = LogThrottle(interval_sec=60.0)
    # First 1006 close emits; the next dozen within the window are collapsed.
    emit, _ = th.allow(("mexc", "ws_closed", 1006), now=0.0)
    assert emit
    for i in range(12):
        assert not th.allow(("mexc", "ws_closed", 1006), now=1.0 + i)[0]
    # A different close code in the same window is a distinct key → still emitted.
    emit, _ = th.allow(("mexc", "ws_closed", 4004), now=5.0)
    assert emit
    # Repeated connects for the same venue collapse independently of closes.
    assert th.allow(("mexc", "ws_connected"), now=0.0)[0]
    assert not th.allow(("mexc", "ws_connected"), now=3.0)[0]


# ── HTTP error handling (no JSON parse on non-200) ──

class _FakeResp:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self):
        return self._body.decode()

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.closed = False

    def get(self, url, params=None):
        return self._resp


def _binance() -> BinanceAdapter:
    return BinanceAdapter(Settings(), ScannerConfig(), sink=None)


async def test_non_200_raises_rest_error_without_json_decode():
    adapter = _binance()
    session = _FakeSession(_FakeResp(451, b"<html>Unavailable For Legal Reasons</html>"))
    with pytest.raises(RestError) as ei:
        await adapter._get_json_logged(session, "https://x/api")
    err = ei.value
    assert err.status == 451
    assert err.blocked
    assert "451" in str(err)


async def test_forbidden_is_structured_and_blocked():
    adapter = _binance()
    session = _FakeSession(_FakeResp(403, b"CloudFront block"))
    with pytest.raises(RestError) as ei:
        await adapter._get_json_logged(session, "https://x/api")
    assert ei.value.status == 403
    assert ei.value.blocked


async def test_200_non_json_raises_rest_error():
    adapter = _binance()
    session = _FakeSession(_FakeResp(200, b"<html>interstitial</html>"))
    with pytest.raises(RestError) as ei:
        await adapter._get_json_logged(session, "https://x/api")
    assert ei.value.status == 200


async def test_200_json_parses():
    adapter = _binance()
    session = _FakeSession(_FakeResp(200, b'{"ok": true}'))
    assert await adapter._get_json_logged(session, "https://x/api") == {"ok": True}


# ── Binance proxy chain ──

def test_binance_proxy_chain_from_settings():
    s = Settings(binance_proxy="socks5://user:pw@p1:1080",
                 binance_proxy_fallback="http://p2:8080")
    a = BinanceAdapter(s, ScannerConfig(), sink=None)
    assert a._active_proxy == "socks5://user:pw@p1:1080"
    assert "pw" not in (a._proxy_log or "")  # password never logged
    a._proxy_idx += 1
    assert a._active_proxy == "http://p2:8080"


def test_binance_no_proxy_is_direct():
    a = _binance()
    assert a._active_proxy is None
    assert a._proxy_log is None


# ── RPC provider pool ──

def test_isolated_failures_do_not_disable_provider():
    pool = RpcProviderPool(urls=["u1", "u2"], fail_threshold=5, network="ethereum")
    # Alternating failure/success (short-lived blips) never trips the disable gate.
    for _ in range(10):
        pool.record_failure("u1", RpcErrorKind.TIMEOUT)
        pool.record_success("u1", latency_ms=100)
    snap = {p["url"]: p for p in pool.snapshot()}
    assert snap["u1"]["state"] == "closed"


def test_consecutive_failures_disable_then_recover():
    pool = RpcProviderPool(urls=["u1", "u2"], fail_threshold=3,
                           cooldown_base_sec=20.0, network="ethereum")
    for _ in range(3):
        pool.record_failure("u1", RpcErrorKind.HTTP)
    snap = {p["url"]: p for p in pool.snapshot()}
    assert snap["u1"]["state"] == "open"  # tripped, cooling down
    # A disabled provider is excluded from rotation while u2 is healthy.
    assert pool.order() == ["u2"]
    # One success after the cooldown probe fully restores it (breaker closes).
    pool.record_success("u1", latency_ms=50)
    snap = {p["url"]: p for p in pool.snapshot()}
    assert snap["u1"]["state"] == "closed"
    assert snap["u1"]["fail_streak"] == 0


def test_ranking_prefers_healthiest_provider():
    pool = RpcProviderPool(urls=["bad", "good"], fail_threshold=10, network="bnb")
    for _ in range(6):
        pool.record_failure("bad", RpcErrorKind.RATE_LIMIT)
        pool.record_success("good", latency_ms=80)
    # "bad" is not disabled (threshold 10) but must sink below "good".
    order = pool.order()
    assert order[0] == "good"
    assert set(order) == {"bad", "good"}


def test_all_disabled_still_probes_one():
    pool = RpcProviderPool(urls=["u1"], fail_threshold=1, network="base")
    pool.record_failure("u1", RpcErrorKind.HTTP)
    assert pool.order() == ["u1"]  # never empty — probes the nearest-recovery one


def test_healthy_count_for_summary():
    pool = RpcProviderPool(urls=["u1", "u2", "u3"], fail_threshold=1, network="eth")
    pool.record_failure("u2", RpcErrorKind.HTTP)
    healthy, total = pool.healthy_count()
    assert (healthy, total) == (2, 3)
