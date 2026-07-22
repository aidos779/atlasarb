"""Phase 1.5 — lightweight CPU-attribution instrumentation.

Verifies the new cumulative timers without changing any behavior:
  * detection time splits by call path (event vs reconciliation);
  * per-detector cumulative time is attributed by arb_type;
  * assemble time splits pre-gate vs full-pipeline (driven by AssemblyResult.pregate_hit);
  * reconciliation pass duration is recorded;
  * timing_snapshot()/snapshot() expose all of it.
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate
from src.domain.ports import ExchangeAdapter
from src.domain.signal import Candidate, FundingSnapshot, LegRef
from src.scanner.assembler import SignalAssembler
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.engine import ScanningEngine
from src.scanner.monitoring.metrics import Metrics
from src.scanner.priority.scheduler import PriorityClassifier
from src.scanner.status.health_registry import HealthRegistry


# ── Metrics unit ────────────────────────────────────────────────────────────────────────
def test_detection_time_splits_by_path():
    m = Metrics()
    m.record_detection(4.0, from_reconciliation=False)
    m.record_detection(6.0, from_reconciliation=False)
    m.record_detection(10.0, from_reconciliation=True)
    assert m.event_detection_ms_total == 10.0
    assert m.event_detection_ticks == 2
    assert m.reconciliation_detection_ms_total == 10.0
    assert m.reconciliation_detection_ticks == 1


def test_detection_default_path_is_event():
    m = Metrics()
    m.record_detection(3.0)
    assert m.event_detection_ticks == 1 and m.reconciliation_detection_ticks == 0


def test_per_detector_time_accumulates_by_type():
    m = Metrics()
    m.record_detector_time("CEX_CEX", 1.5)
    m.record_detector_time("CEX_CEX", 2.5)
    m.record_detector_time("FUNDING", 4.0)
    assert m.detector_ms_total["CEX_CEX"] == 4.0
    assert m.detector_calls["CEX_CEX"] == 2
    assert m.detector_ms_total["FUNDING"] == 4.0
    assert m.detector_calls["FUNDING"] == 1


def test_generation_splits_pregate_vs_full():
    m = Metrics()
    m.record_generation(1.0, pregate=True)
    m.record_generation(2.0, pregate=True)
    m.record_generation(9.0, pregate=False)
    assert m.assemble_pregate_ms_total == 3.0 and m.assemble_pregate_count == 2
    assert m.assemble_full_ms_total == 9.0 and m.assemble_full_count == 1
    assert m.generation_ms_total == 12.0


def test_reconciliation_pass_recorded():
    m = Metrics()
    m.record_reconciliation_pass(120.0)
    m.record_reconciliation_pass(80.0)
    assert m.reconciliation_pass_ms_total == 200.0
    assert m.reconciliation_pass_count == 2


def test_timing_snapshot_shape_and_snapshot_includes_timing():
    m = Metrics()
    m.record_detection(5.0, from_reconciliation=True)
    m.record_detector_time("DEX_DEX", 2.0)
    m.record_generation(1.0, pregate=True)
    ts = m.timing_snapshot()
    for key in ("event_detection_ms_total", "reconciliation_detection_ms_total",
                "event_detection_ticks", "reconciliation_detection_ticks",
                "detector_ms_total", "detector_calls", "generation_ms_total",
                "assemble_pregate_ms_total", "assemble_pregate_count",
                "assemble_full_ms_total", "assemble_full_count",
                "reconciliation_pass_ms_total", "reconciliation_pass_count"):
        assert key in ts
    assert ts["detector_ms_total"]["DEX_DEX"] == 2.0
    assert "timing" in m.snapshot()


# ── assembler pregate flag ────────────────────────────────────────────────────────────
class _Ad(ExchangeAdapter):
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
    def taker_fee(self, s): return Decimal("0.001")   # floor = 0.2% round-trip
    def withdrawal_fee_usd(self, a, n): return Decimal("1")
    def withdrawals_enabled(self, a, n): return True


class _Gas:
    async def gas_price_usd(self, n, u): return Decimal("0")


def _assembler():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    return SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))


async def test_spot_pregate_reject_sets_pregate_hit():
    asm = _assembler()
    # gross 0.1% <= 0.2% fee floor → pre-gate UNPROFITABLE_AFTER_FEES.
    cand = Candidate(arb_type=ArbitrageType.CEX_CEX, base_asset="ETH", quote_asset="USDT",
                     buy_leg=LegRef("binance", "CEX", Decimal("3000")),
                     sell_leg=LegRef("okx", "CEX", Decimal("3003")),
                     gross_spread_pct=Decimal("0.1"))
    res = await asm.assemble(cand)
    assert res.signal is None
    assert res.pregate_hit is True


async def test_funding_path_does_not_set_pregate_hit():
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    adapters = {"binance": _Ad("binance"), "okx": _Ad("okx")}
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    now = time.time()
    cache.upsert_funding(FundingRate("binance", "ETH", Decimal("0"), None, now + 3600, 8))
    cache.upsert_funding(FundingRate("okx", "ETH", Decimal("0"), None, now + 3600, 8))
    asm = SignalAssembler(cfg, cache, health, adapters, _Gas(), PriorityClassifier(cfg))
    snap = FundingSnapshot(received_at=now, current_rate=Decimal("0"),
                           has_predicted=False, has_next_time=True, history=(Decimal("0"),))
    cand = Candidate(arb_type=ArbitrageType.FUNDING, base_asset="ETH", quote_asset="USDT",
                     buy_leg=LegRef("binance", "CEX", Decimal(1)),
                     sell_leg=LegRef("okx", "CEX", Decimal(1)),
                     gross_spread_pct=Decimal("0"),
                     funding_annualized_spread=Decimal("0.45"),
                     funding_next_time=now + 3600, funding_low=snap, funding_high=snap)
    res = await asm.assemble(cand)
    assert res.signal is not None            # a real funding publish
    assert res.pregate_hit is False          # funding has no spot pre-gate


# ── engine path attribution (behavior unchanged, timers populated) ──────────────────────
class _Cex(_Ad):
    pass


class _Dex(_Ad):
    venue_type = VenueType.DEX
    network = "ethereum"


class _Queue:
    def __init__(self): self.msgs = []
    async def publish(self, signal, event): self.msgs.append((event, signal))


class _History:
    async def archive(self, signal): ...


def _engine():
    cfg = ScannerConfig()
    adapters = {"binance": _Cex("binance"), "uni": _Dex("uni")}
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    eng = ScanningEngine(cfg, adapters, _Queue(), _History(), _Gas(), {"ETH"},
                         cache=cache, health=health)
    return eng


async def test_event_path_attributes_to_event_timer():
    eng = _engine()
    await eng._process_symbol("ETH", "USDT")   # default: event path
    m = eng.metrics
    assert m.event_detection_ticks == 1
    assert m.reconciliation_detection_ticks == 0
    # Every detector that ran was timed (per-detector attribution).
    assert m.detector_calls[ArbitrageType.CEX_CEX.value] == 1
    assert m.detector_calls[ArbitrageType.FUNDING.value] == 1


async def test_reconciliation_flag_attributes_to_reconciliation_timer():
    eng = _engine()
    await eng._process_symbol("ETH", "USDT", from_reconciliation=True)
    m = eng.metrics
    assert m.reconciliation_detection_ticks == 1
    assert m.event_detection_ticks == 0


async def test_full_scan_records_a_pass():
    eng = _engine()
    eng.cache.track("binance", CanonicalSymbol("ETH", "USDT", VenueType.CEX))
    await eng._run_full_scan()
    assert eng.metrics.reconciliation_pass_count == 1
    assert eng.metrics.reconciliation_pass_ms_total >= 0.0
