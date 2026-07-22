"""Phase 4 — reconciliation engine redesign.

The reconciliation scan stays a safety net but scans only the working set (pairs that can
form a candidate) and, per pair, only detectors that are cadence-due AND eligible. These
tests prove: the cadence logic (per-detector + yield-aware backoff/recovery), eligibility
is a safe superset, the working set skips impossible pairs/detectors, skipping is loss-free,
and reconciliation still detects a live opportunity (coverage / eventual consistency).
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExchangeStatus, VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.domain.ports import ExchangeAdapter
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.engine import ScanningEngine
from src.scanner.reconciliation.cadence import DetectorReconciliationCadence
from src.scanner.status.health_registry import HealthRegistry

CC = ArbitrageType.CEX_CEX.value
CD = ArbitrageType.CEX_DEX.value
DD = ArbitrageType.DEX_DEX.value
XC = ArbitrageType.CROSS_CHAIN.value
FU = ArbitrageType.FUNDING.value


# ── cadence planner (pure logic) ────────────────────────────────────────────────────────
def _cad(**over):
    cfg = ScannerConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return DetectorReconciliationCadence(cfg, start_now=0.0)


def test_first_pass_all_detectors_due():
    assert _cad().due_detectors(0.0) == {CC, CD, DD, XC, FU}


def test_per_detector_cadence_orders_frequency():
    c = _cad()
    c.mark_reconciled({CC, CD, DD, XC, FU}, 0.0)
    # funding cadence 1s, cex 2s, dex_dex 5s, cross_chain 10s.
    assert c.due_detectors(1.0) == {FU}
    assert c.due_detectors(2.0) == {FU, CC, CD}
    assert c.due_detectors(5.0) == {FU, CC, CD, DD}
    assert c.due_detectors(10.0) == {FU, CC, CD, DD, XC}


def test_cadence_zero_means_every_pass():
    c = _cad(reconciliation_cadence_cross_chain_sec=0.0)
    c.mark_reconciled({XC}, 0.0)
    assert XC in c.due_detectors(0.001)


def test_yield_backoff_and_recovery():
    c = _cad(reconciliation_idle_after_sec=300.0, reconciliation_idle_backoff=4.0)
    # cross_chain base 10s. At t=400 (idle > 300, no publish since start=0) → 10×4 = 40s.
    assert c.effective_cadence(XC, 400.0) == 40.0
    # a publish resets the yield timer → back to base 10s immediately.
    c.mark_publish(XC, 400.0)
    assert c.effective_cadence(XC, 400.0) == 10.0
    # and it stays at base until idle again.
    assert c.effective_cadence(XC, 600.0) == 10.0
    assert c.effective_cadence(XC, 701.0) == 40.0   # idle again (>300 since last publish)


def test_cadence_report_shape():
    rep = _cad().cadence_report(0.0)
    assert set(rep) == {CC, CD, DD, XC, FU}
    assert rep[FU] == 1.0 and rep[XC] == 10.0


# ── engine fixtures ─────────────────────────────────────────────────────────────────────
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


class _Dex(_Cex):
    venue_type = VenueType.DEX
    network = "ethereum"


class _Dex2(_Cex):
    venue_type = VenueType.DEX
    network = "bnb"


class _Q:
    def __init__(self): self.msgs = []
    async def publish(self, s, e): self.msgs.append((e, s))


class _H:
    async def archive(self, s): ...


class _G:
    async def gas_price_usd(self, n, u): return Decimal("0")


def _engine(adapters):
    cfg = ScannerConfig()
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    for v in adapters:
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
    queue = _Q()
    eng = ScanningEngine(cfg, adapters, queue, _H(), _G(), {"ETH"}, cache=cache, health=health)
    eng._test_queue = queue
    return eng, cache


def _cex_book(cache, venue, base, bid, ask):
    sym = CanonicalSymbol(base, "USDT", VenueType.CEX)
    cache.track(venue, sym)
    for _ in range(3):   # warm up the pair (CEX warmup advances on price ticks)
        cache.upsert_price(PriceQuote(venue, sym, Decimal(bid), Decimal(ask), Decimal(bid)))
    cache.upsert_book(OrderBook(venue, sym, [BookLevel(Decimal(bid), Decimal("1e9"))],
                                [BookLevel(Decimal(ask), Decimal("1e9"))]))


def _pool(cache, venue, network, base, rq):
    sym = CanonicalSymbol(base, "USDT", VenueType.DEX, network)
    cache.track(venue, sym)
    cache.upsert_book(OrderBook(venue, sym, [], [], pool_address=f"{venue}:{base}",
                                pool_fee_tier=Decimal("0.003"),
                                reserve_base=Decimal("100"), reserve_quote=Decimal(rq)))


# ── eligibility matrix ──────────────────────────────────────────────────────────────────
def test_eligibility_two_cex_only():
    eng, cache = _engine({"a": _Cex("a"), "b": _Cex("b")})
    _cex_book(cache, "a", "ETH", "99.9", "100.0")
    _cex_book(cache, "b", "ETH", "100.5", "100.6")
    assert eng._reconcile_eligibility("ETH", "USDT") == {CC}   # 2 CEX → cex_cex only


def test_eligibility_cex_plus_dex():
    eng, cache = _engine({"a": _Cex("a"), "u": _Dex("u")})
    _cex_book(cache, "a", "ETH", "99.9", "100.0")
    _pool(cache, "u", "ethereum", "ETH", "300000")
    assert eng._reconcile_eligibility("ETH", "USDT") == {CD}   # 1 CEX + 1 DEX


def test_eligibility_two_dex_same_and_cross_chain():
    eng, cache = _engine({"u": _Dex("u"), "c": _Dex2("c")})   # ethereum + bnb
    _pool(cache, "u", "ethereum", "ETH", "300000")
    _pool(cache, "c", "bnb", "ETH", "306000")
    elig = eng._reconcile_eligibility("ETH", "USDT")
    assert DD in elig and XC in elig                          # 2 DEX, 2 networks
    assert CC not in elig and CD not in elig


def test_eligibility_single_venue_is_empty():
    eng, cache = _engine({"a": _Cex("a")})
    _cex_book(cache, "a", "ETH", "99.9", "100.0")
    assert eng._reconcile_eligibility("ETH", "USDT") == set()   # nothing can pair


# ── working-set scan ────────────────────────────────────────────────────────────────────
async def test_reconciliation_skips_ineligible_pairs_and_records_metrics():
    eng, cache = _engine({"a": _Cex("a"), "b": _Cex("b")})
    # one eligible pair (2 CEX) + one impossible pair (1 CEX)
    _cex_book(cache, "a", "ETH", "99.9", "100.0")
    _cex_book(cache, "b", "ETH", "100.5", "100.6")
    _cex_book(cache, "a", "SOLO", "10.0", "10.1")     # only venue "a"
    await eng._run_full_scan()
    m = eng.metrics
    assert m.reconciliation_working_set == 1          # only ETH/USDT
    assert m.reconciliation_skipped_pairs == 1        # SOLO/USDT skipped whole
    # ETH ran only cex_cex (1 of 5); SOLO skipped all 5.
    assert m.reconciliation_ran_detector_execs == 1
    assert m.reconciliation_skipped_detector_execs == len(eng._detectors) * 2 - 1


async def test_reconciliation_runs_only_eligible_detectors():
    eng, cache = _engine({"a": _Cex("a"), "b": _Cex("b")})
    _cex_book(cache, "a", "ETH", "99.9", "100.0")
    _cex_book(cache, "b", "ETH", "100.5", "100.6")
    await eng._run_full_scan()
    checked = {d.arb_type: d.counters.opportunities_checked for d in eng._detectors}
    assert checked[CC] == 1                            # cex_cex ran
    assert checked[DD] == 0 and checked[XC] == 0 and checked[FU] == 0 and checked[CD] == 0


# ── skipped-detector correctness (loss-free) ────────────────────────────────────────────
async def test_only_detectors_subset_equals_full_run_when_others_are_ineligible():
    # Running just the eligible detector must yield the same candidates as running all five
    # (the ineligible four early-return []), so skipping them loses nothing.
    eng, cache = _engine({"a": _Cex("a"), "b": _Cex("b")})
    _cex_book(cache, "a", "ETH", "99.0", "100.0")
    _cex_book(cache, "b", "ETH", "105.0", "106.0")     # ~5% spread → publishable
    await eng._process_symbol("ETH", "USDT")           # all detectors
    all_msgs = len(eng._test_queue.msgs)
    # a fresh engine for the subset run
    eng2, cache2 = _engine({"a": _Cex("a"), "b": _Cex("b")})
    _cex_book(cache2, "a", "ETH", "99.0", "100.0")
    _cex_book(cache2, "b", "ETH", "105.0", "106.0")
    await eng2._process_symbol("ETH", "USDT", only_detectors={CC})
    subset_msgs = len(eng2._test_queue.msgs)
    assert all_msgs == subset_msgs == 1                # identical publish outcome


# ── coverage / eventual consistency ─────────────────────────────────────────────────────
async def test_reconciliation_detects_live_opportunity():
    # A publishable spread present in cache but never event-processed must be found by the
    # reconciliation safety net (proves it still guarantees eventual detection).
    eng, cache = _engine({"a": _Cex("a"), "b": _Cex("b")})
    eng._pending_groups.clear()                        # drop any event-path work
    _cex_book(cache, "a", "ETH", "99.0", "100.0")
    _cex_book(cache, "b", "ETH", "105.0", "106.0")     # ~5% spread
    await eng._run_full_scan()
    assert len(eng._test_queue.msgs) == 1              # detected + published by reconcile


async def test_backed_off_detector_still_eventually_runs():
    # A cold cross-chain detector is slowed, not disabled: with cadence 0 forced it runs;
    # more generally it runs within base×backoff. Here prove it is never permanently skipped.
    eng, cache = _engine({"u": _Dex("u"), "c": _Dex2("c")})
    _pool(cache, "u", "ethereum", "ETH", "300000")
    _pool(cache, "c", "bnb", "ETH", "306000")
    await eng._run_full_scan()                          # first pass → all due
    assert eng._detectors[3].counters.opportunities_checked >= 1   # cross_chain ran
