"""Regression tests for the interrupted production-readiness refactor.

Covers the four completed workstreams and the inconsistencies that had to be reconciled:

1. Deterministic signal identity (Signal/Candidate.route_id, §13.1).
2. Candidate-generation optimization that preserves identical detection *and* lifecycle
   results — the per-venue spot/network pre-computation, and the removal of the
   duplicated detector-level fee-floor prune (which had broken §12.4 spread-collapse
   expiry).
3. Funding confidence derived entirely from live metrics — no constant default.
4. RPC provider pool circuit-breaker: reachable HALF_OPEN state and permanent
   retirement of unauthorized endpoints; plus the discovery/quote liveness split and
   auth-aware Ankr resolution that shipped alongside them.
"""
from __future__ import annotations

from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.domain.enums import ArbitrageType, VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.domain.signal import Candidate, LegRef, Signal
from src.scanner.adapters.base_dex import _auth_body
from src.scanner.adapters.rpc_pool import BreakerState, RpcErrorKind, RpcProviderPool
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.detectors.base import DetectionContext, VenueInfo
from src.scanner.detectors.cex_cex import CexCexDetector
from src.scanner.detectors.dex_dex import DexDexDetector
from src.scanner.ranking.confidence import FundingLegQuality, funding_confidence
from src.scanner.status.health_registry import HealthRegistry

# ─────────────────────────── 1. Deterministic signal identity ───────────────────────────

def _cand(buy="binance", sell="okx", base="BTC", quote="USDT", net=None) -> Candidate:
    return Candidate(
        arb_type=ArbitrageType.CEX_CEX, base_asset=base, quote_asset=quote,
        buy_leg=LegRef(buy, "CEX", Decimal("100")),
        sell_leg=LegRef(sell, "CEX", Decimal("101")),
        gross_spread_pct=Decimal("1"), network=net,
    )


def test_route_id_is_deterministic_and_stable_across_rederivation():
    c = _cand()
    # Same route → same id every call (and, being a pure uuid5, across process restarts).
    assert c.route_id() == c.route_id()
    assert c.route_id() == _cand().route_id()
    # Fixed, known value — a change here means the id namespace/derivation drifted, which
    # would reshuffle every signal id in production.
    assert c.route_id() == "1d16ba63-d1d1-5e7b-ad49-5f284ae5d273"


def test_route_id_is_direction_independent():
    # buy/sell swapped is the same underlying opportunity → identical id.
    assert _cand("binance", "okx").route_id() == _cand("okx", "binance").route_id()


def test_route_id_changes_with_route():
    base = _cand().route_id()
    assert _cand(base="ETH").route_id() != base          # different asset
    assert _cand(sell="bybit").route_id() != base        # different venue
    assert _cand(net="ethereum").route_id() != base      # different network


def test_signal_and_candidate_route_ids_match():
    c = _cand()
    s = Signal(id=c.route_id(), arb_type=ArbitrageType.CEX_CEX, coin="BTC",
               trading_pair="BTC/USDT", network=None, buy_exchange="binance",
               sell_exchange="okx", buy_price=Decimal("100"), sell_price=Decimal("101"),
               buy_venue_type="CEX", sell_venue_type="CEX")
    # The id the assembler stamps (cand.route_id) equals the signal's own route identity
    # and its dedup key — so lifecycle updates for the same route reuse the id.
    assert s.id == s.route_id() == c.route_id()
    assert s.dedup_key() == c.dedup_key()


# ──────────────── 2. Candidate generation: identical detection + lifecycle ────────────────

def _cex_ctx(cfg: ScannerConfig):
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    venues = {}
    for v in ("binance", "okx"):
        health.register(v)
        for _ in range(3):
            health.record_success(v)
        venues[v] = VenueInfo(id=v, venue_type=VenueType.CEX)
    ctx = DetectionContext(cache=cache, health=health, venues=venues,
                           max_plausible_cex_spread_pct=Decimal(str(
                               cfg.max_plausible_cex_spread_pct)))
    return ctx, cache


def _cex(cache, venue, base, bid, ask):
    sym = CanonicalSymbol(base, "USDT", VenueType.CEX)
    cache.track(venue, sym)
    for _ in range(3):
        cache.upsert_price(PriceQuote(venue, sym, Decimal(bid), Decimal(ask), Decimal(bid)))
    cache.upsert_book(OrderBook(venue, sym, [BookLevel(Decimal(bid), Decimal("1000000"))],
                                [BookLevel(Decimal(ask), Decimal("1000000"))]))


def test_detector_still_emits_below_fee_floor_candidate():
    """The detector-level fee-floor prune was removed because it duplicated the assembler
    pre-gate and suppressed §12.4 spread-collapse expiry. A tiny (sub-fee) but positive
    spread must therefore STILL produce a candidate — the assembler, not the detector,
    decides it is unprofitable, and that reject is what expires a stale active signal."""
    cfg = ScannerConfig()
    ctx, cache = _cex_ctx(cfg)
    # ~0.04% spread — below any realistic round-trip taker fee, but strictly positive.
    _cex(cache, "binance", "BTC", "100.00", "100.01")
    _cex(cache, "okx", "BTC", "100.04", "100.05")
    out = CexCexDetector().detect(ctx, "BTC", "USDT")
    assert len(out) == 1
    assert Decimal("0") < out[0].gross_spread_pct < Decimal("1")


def _dex_ctx(cfg: ScannerConfig):
    cache = MarketStateCache(cfg)
    health = HealthRegistry(cfg)
    venues = {}
    for v in ("uni", "sushi", "pancake"):
        health.register(v)
        for _ in range(3):
            health.record_success(v, stream=True)
        venues[v] = VenueInfo(id=v, venue_type=VenueType.DEX, network="ethereum")
    ctx = DetectionContext(cache=cache, health=health, venues=venues,
                           verified_tokens={"ETH"})
    return ctx, cache


def _pool(cache, venue, reserve_base, reserve_quote):
    sym = CanonicalSymbol("ETH", "USDT", VenueType.DEX, "ethereum")
    cache.track(venue, sym)
    cache.upsert_book(OrderBook(venue, sym, bids=[], asks=[], pool_address=f"{venue}:p",
                                pool_fee_tier=Decimal("0.003"),
                                reserve_base=Decimal(reserve_base),
                                reserve_quote=Decimal(reserve_quote)))


def test_dex_precompute_skips_zero_reserve_and_pairs_the_rest():
    """The O(n²) loop now iterates a pre-priced list. A pool with zero base reserve
    (spot undefined) must be dropped once, up front, and never paired — same result the
    old per-pair `if p<=0: continue` produced, with the pricing done once per pool."""
    cfg = ScannerConfig()
    ctx, cache = _dex_ctx(cfg)
    _pool(cache, "uni", "100", "300000")      # spot 3000
    _pool(cache, "sushi", "100", "303000")    # spot 3030 → ~1% vs uni
    _pool(cache, "pancake", "0", "500000")    # zero base reserve → spot undefined, skipped
    out = DexDexDetector().detect(ctx, "ETH", "USDT")
    # Only the uni/sushi pair survives; nothing references the zero-reserve pool.
    assert len(out) == 1
    venues = {out[0].buy_leg.venue, out[0].sell_leg.venue}
    assert venues == {"uni", "sushi"}
    assert out[0].buy_leg.venue == "uni"        # cheaper pool is the buy leg
    assert out[0].sell_leg.venue == "sushi"


# ───────────────────── 3. Funding confidence — entirely from live metrics ─────────────────────

def _fq(age=1.0, health=Decimal(90), history=None, has_predicted=True, has_next=True):
    return FundingLegQuality(
        age_sec=age, max_age_sec=120.0, exchange_health=health,
        history=history if history is not None else [Decimal("0.01")] * 5,
        has_predicted=has_predicted, has_next_time=has_next,
    )


def test_funding_confidence_rewards_freshness():
    fresh, _ = funding_confidence(_fq(age=1.0), _fq(age=1.0))
    stale, _ = funding_confidence(_fq(age=115.0), _fq(age=115.0))
    assert fresh > stale


def test_funding_confidence_rewards_rate_stability():
    stable = [Decimal("0.010")] * 6
    volatile = [Decimal("0.01"), Decimal("0.09"), Decimal("-0.05"),
                Decimal("0.12"), Decimal("-0.08"), Decimal("0.11")]
    calm, _ = funding_confidence(_fq(history=stable), _fq(history=stable))
    wild, _ = funding_confidence(_fq(history=volatile), _fq(history=volatile))
    assert calm > wild


def test_funding_confidence_tracks_exchange_health_and_weakest_leg():
    healthy, _ = funding_confidence(_fq(health=Decimal(95)), _fq(health=Decimal(95)))
    degraded, _ = funding_confidence(_fq(health=Decimal(20)), _fq(health=Decimal(95)))
    # The weaker leg drives the reliability factor → one bad leg drags the score down.
    assert healthy > degraded


def test_funding_confidence_freshness_clamped_on_clock_skew():
    """A negative age (received_at momentarily ahead of `now` under clock skew) must
    clamp the freshness factor to 100, never above it."""
    _, bd = funding_confidence(_fq(age=-5.0), _fq(age=-5.0))
    assert bd["freshness"] == 100.0


def test_funding_confidence_breakdown_has_no_constant_default():
    """Every factor is computed from the inputs; a fully-degraded input set must not
    floor at some baseline like the old constant 75 / 60."""
    score, bd = funding_confidence(
        _fq(age=120.0, health=Decimal(0), history=[Decimal("1"), Decimal("-1"), Decimal("1")],
            has_predicted=False, has_next=False),
        _fq(age=120.0, health=Decimal(0), history=[Decimal("1"), Decimal("-1"), Decimal("1")],
            has_predicted=False, has_next=False),
    )
    assert set(bd) == {"freshness", "stability", "reliability", "completeness", "score"}
    assert score < 40  # nowhere near a 60/75 default — driven fully by the poor inputs
    assert bd["freshness"] == 0.0 and bd["reliability"] == 0.0


# ─────────────────────────── 4. RPC pool circuit breaker ───────────────────────────

def test_half_open_state_is_reachable_after_cooldown():
    """Regression: with `healthy()` returning true the instant the cooldown elapsed, the
    old `state()` could only ever report CLOSED/OPEN — HALF_OPEN was dead. It must be
    reachable: cooldown elapsed, but no success has re-proven the endpoint yet."""
    import time
    pool = RpcProviderPool(urls=["u1", "u2"], fail_threshold=1,
                           cooldown_base_sec=20.0, network="ethereum")
    pool.record_failure("u1", RpcErrorKind.HTTP)  # trips the breaker → OPEN
    future = time.monotonic() + 1000.0            # well past the 20s cooldown
    snap = {p["url"]: p for p in pool.snapshot(now=future)}
    assert snap["u1"]["state"] == BreakerState.HALF_OPEN.value
    # A half-open provider is offered back into rotation as a probe.
    assert "u1" in pool.order(now=future)
    # One success fully closes it.
    pool.record_success("u1", latency_ms=50)
    closed = {p["url"]: p for p in pool.snapshot()}
    assert closed["u1"]["state"] == BreakerState.CLOSED.value


def test_unauthorized_endpoint_is_permanently_retired():
    pool = RpcProviderPool(urls=["dead", "live"], fail_threshold=5, network="ethereum")
    pool.record_failure("dead", RpcErrorKind.UNAUTHORIZED)  # one strike → retired
    snap = {p["url"]: p for p in pool.snapshot()}
    assert snap["dead"]["permanent"] is True
    assert snap["dead"]["state"] == BreakerState.OPEN.value
    # Retired endpoint is never tried and is excluded from usable-capacity accounting.
    assert pool.order() == ["live"]
    assert pool.healthy_count() == (1, 1)


def test_auth_body_detection():
    assert _auth_body("401 Unauthorized")
    assert _auth_body('{"error":"api key required"}')
    assert _auth_body("Must be authenticated")
    assert not _auth_body("rate limit exceeded")
    assert not _auth_body(None)


# ─────────────────── discovery/quote liveness split & Ankr resolution ───────────────────

def test_discovery_freshness_is_independent_of_status():
    cfg = ScannerConfig()
    health = HealthRegistry(cfg)
    health.register("uni")
    for _ in range(3):
        health.record_success("uni", stream=True)     # quote-fresh + online
    health.record_discovery("uni")
    rep = health.venue_report("uni", quote_stale_sec=60.0)
    assert rep["quote_fresh"] is True
    assert rep["discovery_age_sec"] is not None
    # Discovery is reported but not part of the gating signal set.
    assert "connected" not in rep and "quote_fresh" in rep


def test_ankr_resolution_requires_key():
    no_key = Settings(ankr_api_key="")
    assert no_key._resolve_ankr("https://rpc.ankr.com/eth", "ethereum") is None
    assert no_key._resolve_ankr("https://eth.llamarpc.com", "ethereum") == \
        "https://eth.llamarpc.com"  # non-Ankr passes through
    keyed = Settings(ankr_api_key="SECRET")
    assert keyed._resolve_ankr("https://rpc.ankr.com/eth", "ethereum") == \
        "https://rpc.ankr.com/eth/SECRET"
    # An endpoint that already carries a key is left untouched.
    assert keyed._resolve_ankr("https://rpc.ankr.com/eth/OTHER", "ethereum") == \
        "https://rpc.ankr.com/eth/OTHER"


def test_no_paid_keys_keeps_public_only_behaviour():
    """A deployment with no paid keys has an empty primary tier — pool ordering is
    unchanged from the public-only baseline."""
    s = Settings(ethereum_rpc_urls="https://eth.example")
    assert s._primary_rpc_urls("ethereum") == []
    assert s.rpc_primary_urls_for("ethereum") == set()
    assert s.rpc_urls_for("ethereum")[0] == "https://eth.example"


def test_paid_providers_lead_the_pool_and_are_marked_primary():
    s = Settings(
        alchemy_api_key="AKEY",
        quicknode_ethereum_url="https://name.quiknode.pro/TOKEN/",
        ankr_api_key="ANKEY",
        ethereum_rpc_urls="https://eth.public",
    )
    urls = s.rpc_urls_for("ethereum")
    # QuickNode, then Alchemy, then authenticated Ankr — all ahead of any public node.
    assert urls[:3] == [
        "https://name.quiknode.pro/TOKEN/",
        "https://eth-mainnet.g.alchemy.com/v2/AKEY",
        "https://rpc.ankr.com/eth/ANKEY",
    ]
    assert "https://eth.public" in urls[3:]
    # The primary set is exactly the three paid endpoints (used to tier the pool).
    assert s.rpc_primary_urls_for("ethereum") == set(urls[:3])


def test_alchemy_bnb_endpoint_built_for_bnb():
    s = Settings(alchemy_api_key="AKEY")
    assert s.rpc_urls_for("bnb")[0] == "https://bnb-mainnet.g.alchemy.com/v2/AKEY"


def test_pool_prefers_primary_tier_then_falls_back_when_unhealthy():
    """A healthy primary is always tried before a public fallback; when the primary is
    disabled the fallback takes over automatically, and the recovered primary leads again."""
    pool = RpcProviderPool(urls=["paid", "public"], fail_threshold=2, network="ethereum",
                           primary_urls={"paid"})
    pool.record_success("paid", latency_ms=200)
    pool.record_success("public", latency_ms=50)   # faster, but a fallback
    assert pool.order()[0] == "paid"               # tier wins over raw latency
    # Primary goes down → fallback serves alone.
    for _ in range(2):
        pool.record_failure("paid", RpcErrorKind.TIMEOUT)
    assert pool.order() == ["public"]
    # Primary recovers → it leads again.
    pool.record_success("paid", latency_ms=200)
    assert pool.order()[0] == "paid"
