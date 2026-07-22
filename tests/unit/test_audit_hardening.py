"""Regression tests for the three Important production-audit findings:

1. the generator worker survives any exception in the detection/assembly/lifecycle path;
2. a raising gas provider is treated as "gas unavailable" (None), never propagated;
3. provider API keys/tokens are stripped from logged URLs.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.config import redact_url
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExchangeStatus, VenueType
from src.domain.ports import ExchangeAdapter
from src.domain.signal import Candidate, LegRef
from src.scanner.assembler import SignalAssembler
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.engine import ScanningEngine
from src.scanner.priority.scheduler import PriorityClassifier
from src.scanner.status.health_registry import HealthRegistry


class _Cex(ExchangeAdapter):
    venue_type = VenueType.CEX
    network = None

    def __init__(self, vid):
        self.id = vid
        self.display_name = vid

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_markets(self): return []
    async def subscribe_ticker(self, s): ...
    async def subscribe_order_book(self, s, d): ...
    async def get_funding_rate(self, a): return None
    async def get_pool_state(self, s): return None
    async def health_check(self): return True
    def get_status(self): return ExchangeStatus.ONLINE
    def taker_fee(self, s): return Decimal("0.001")
    def withdrawal_fee_usd(self, a, n): return Decimal("1")
    def withdrawals_enabled(self, a, n): return True


class _Q:
    def __init__(self): self.msgs = []
    async def publish(self, s, e): self.msgs.append((e, s))


class _H:
    async def archive(self, s): ...


class _OkGas:
    async def gas_price_usd(self, n, u): return Decimal("0")


class _RaisingGas:
    async def gas_price_usd(self, n, u):
        raise RuntimeError("gas provider down")


def _engine(gas=None):
    cfg = ScannerConfig()
    adapters = {"binance": _Cex("binance")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    health.register("binance")
    for _ in range(3):
        health.record_success("binance", stream=True)
    return ScanningEngine(cfg, adapters, _Q(), _H(), gas or _OkGas(), {"ETH"},
                          cache=cache, health=health)


# ── 1. generator worker isolation ──────────────────────────────────────────────────────
async def test_worker_survives_process_symbol_exception():
    eng = _engine()
    seen: list[tuple[str, str]] = []

    async def boom(base, quote, *a, **k):
        seen.append((base, quote))
        if base == "ETH":
            raise RuntimeError("bad candidate / raising adapter")

    eng._process_symbol = boom                       # type: ignore[method-assign]
    eng._on_cache_write("ETH", "USDT", "binance")    # this one raises
    eng._on_cache_write("BTC", "USDT", "binance")    # this one must still be processed

    task = asyncio.create_task(eng._generator_worker())
    for _ in range(50):                              # let both events drain
        await asyncio.sleep(0.005)
        if ("BTC", "USDT") in seen:
            break
    eng._stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert task.done() and task.exception() is None  # the raise did NOT kill the worker
    assert ("ETH", "USDT") in seen and ("BTC", "USDT") in seen  # kept processing


# ── 2. gas provider isolation ──────────────────────────────────────────────────────────
async def test_gas_provider_exception_becomes_unavailable():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Cex("binance")}
    asm = SignalAssembler(cfg, cache, health, adapters, _RaisingGas(), PriorityClassifier(cfg))
    cand = Candidate(
        arb_type=ArbitrageType.CEX_DEX, base_asset="ETH", quote_asset="USDT",
        buy_leg=LegRef("uni", "DEX", Decimal("3000"), network="ethereum"),
        sell_leg=LegRef("binance", "CEX", Decimal("3010")),
        gross_spread_pct=Decimal("0.3"))
    # A raising gas provider must resolve to None ("gas unavailable"), never propagate.
    assert await asm._gas_estimate(cand) is None


async def test_gas_provider_ok_path_unchanged():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Cex("binance")}
    asm = SignalAssembler(cfg, cache, health, adapters, _OkGas(), PriorityClassifier(cfg))
    cand = Candidate(
        arb_type=ArbitrageType.CEX_DEX, base_asset="ETH", quote_asset="USDT",
        buy_leg=LegRef("uni", "DEX", Decimal("3000"), network="ethereum"),
        sell_leg=LegRef("binance", "CEX", Decimal("3010")),
        gross_spread_pct=Decimal("0.3"))
    assert await asm._gas_estimate(cand) == Decimal("0")     # behavior preserved


# ── 3. URL redaction ───────────────────────────────────────────────────────────────────
def test_redact_url_strips_path_key_keeps_host():
    assert (redact_url("https://eth-mainnet.g.alchemy.com/v2/2jn-Qf6CjHOWXp6IqUSAx")
            == "https://eth-mainnet.g.alchemy.com/…")
    assert "2jn-Qf6CjHOWXp6IqUSAx" not in redact_url(
        "https://eth-mainnet.g.alchemy.com/v2/2jn-Qf6CjHOWXp6IqUSAx")


def test_redact_url_strips_query_secret():
    out = redact_url("https://api.zan.top/eth-mainnet?apikey=SECRETKEY")
    assert "SECRETKEY" not in out and out.startswith("https://api.zan.top")


def test_redact_url_keeps_keyless_host_unchanged():
    # No path/query → nothing to strip; host-only providers stay fully readable.
    assert redact_url("https://bsc-dataseed.binance.org") == "https://bsc-dataseed.binance.org"


def test_redact_url_passthrough_non_url():
    assert redact_url("eth.drpc.org") == "eth.drpc.org"     # bare host, no scheme
    assert redact_url("") == ""


def test_redact_url_marks_that_a_path_existed():
    # A '/…' marker signals a keyed endpoint without revealing the key.
    assert redact_url("https://1rpc.io/eth") == "https://1rpc.io/…"
