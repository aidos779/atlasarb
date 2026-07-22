"""Detector base + shared context (Scanner §7).

Detectors are stateless (read-only from the Market State Cache, §1.1 principle 3).
Each identifies which venues form an opportunity and its direction, emitting raw
Candidates; the shared profit engine (§8) and validator (§10) run downstream.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.enums import VenueType
from src.domain.signal import Candidate
from src.scanner.cache.market_state_cache import MarketStateCache
from src.scanner.monitoring.detector_stats import DetectorCounters
from src.scanner.status.health_registry import HealthRegistry


@dataclass
class VenueInfo:
    id: str
    venue_type: VenueType
    network: str | None = None
    is_perp: bool = False


@dataclass
class DetectionContext:
    cache: MarketStateCache
    health: HealthRegistry
    venues: dict[str, VenueInfo] = field(default_factory=dict)
    verified_tokens: set[str] = field(default_factory=set)  # §3.4 (base assets)
    # Price-spread plausibility ceiling for CEX↔CEX (ticker-collision / bad-tick guard).
    # 0 disables the guard. Set from ScannerConfig.max_plausible_cex_spread_pct.
    max_plausible_cex_spread_pct: Decimal = Decimal(10)
    # Minimum annualized funding differential for a funding candidate (§7.4 noise floor).
    # Set from ScannerConfig.funding_min_annualized_spread.
    funding_min_annualized_spread: Decimal = Decimal("0.05")
    # Base assets whose ticker collides with a different token across venues — CEX↔CEX
    # detection is skipped for them (identity-level guard). From ScannerConfig.
    ambiguous_tickers: frozenset[str] = frozenset()
    # ── detector-side economic floor (Phase 3) ──
    # Per-CEX-venue taker-fee rate (fraction), sourced from the adapters — the same rates
    # the assembler charges. DEX legs charge no flat taker (pool fee is inside the fill).
    cex_taker_rates: dict[str, Decimal] = field(default_factory=dict)
    economic_floor_enabled: bool = True
    min_roi_pct: Decimal = Decimal("0.15")
    stablecoin_crossquote_bps: Decimal = Decimal("3")
    cost_amortization_usd: Decimal = Decimal("1000000")
    gas_estimate_usd: Decimal = Decimal("1")
    # Funding economic-breakeven inputs (replace the fixed annualized floor).
    min_net_profit_usd: Decimal = Decimal("5")
    funding_position_size_usd: Decimal = Decimal("1000")
    funding_hold_hours: Decimal = Decimal("168")
    # Dedup keys of currently-active published signals (a live view of the lifecycle's
    # active set). A route in here is EXEMPT from the economic prune so its (now sub-fee)
    # candidate still reaches the assembler, whose BELOW_MIN_PROFIT reject drives §12.4
    # prompt spread-collapse expiry. Usually empty → the prune short-circuits at zero cost.
    active_routes: Collection[tuple] = frozenset()
    # ── per-tick scan memo (see begin_tick) ──
    _memo_active: bool = False
    _memo_cex: dict[str, list[tuple[str, object]]] = field(default_factory=dict)
    _memo_dex: dict[str, list[tuple[str, object]]] = field(default_factory=dict)

    def begin_tick(self) -> None:
        """Open a new detection tick, invalidating the scan memo.

        The five detectors run back-to-back for one symbol with no await between them
        (see ScanningEngine._process_symbol), so the cache cannot change mid-tick and
        the two scan helpers below are guaranteed to return the same result for the
        same pair within a tick. They were being recomputed 2x (CEX) and 3x (DEX) per
        symbol, each recomputation re-walking every venue with a freshness-checked
        get_price/get_book — the single largest cost in the detection path.

        Memoization is only active between begin_tick() calls: a context that never
        gets one (a detector driven directly, as in the unit tests) recomputes exactly
        as before, so the memo can never serve a stale scan to an un-ticked caller.
        """
        self._memo_active = True
        self._memo_cex.clear()
        self._memo_dex.clear()

    def online_cex_prices(self, pair: str) -> list[tuple[str, object]]:
        if self._memo_active:
            hit = self._memo_cex.get(pair)
            if hit is not None:
                return hit
        out: list[tuple[str, object]] = []
        for venue in self.cache.venues_for_pair(pair):
            info = self.venues.get(venue)
            if not info or info.venue_type != VenueType.CEX:
                continue
            if not self.health.is_online(venue):
                continue
            quote = self.cache.get_price(venue, pair)
            book = self.cache.get_book(venue, pair)
            if quote and book:
                out.append((venue, quote))
        if self._memo_active:
            self._memo_cex[pair] = out
        return out

    def online_dex_books(self, pair: str) -> list[tuple[str, object]]:
        if self._memo_active:
            hit = self._memo_dex.get(pair)
            if hit is not None:
                return hit
        out: list[tuple[str, object]] = []
        for venue in self.cache.venues_for_pair(pair):
            info = self.venues.get(venue)
            if not info or info.venue_type != VenueType.DEX:
                continue
            if not self.health.is_online(venue):
                continue
            book = self.cache.get_book(venue, pair)
            if book and book.reserve_base:
                out.append((venue, book))
        if self._memo_active:
            self._memo_dex[pair] = out
        return out

    # ── economic floor (Phase 3) ──────────────────────────────────────────────────────
    def clears_economic_floor(
        self, gross_pct: Decimal, buy_venue: str, buy_type: str,
        sell_venue: str, sell_type: str, quote_asset: str,
        fixed_cost_usd: Decimal = Decimal(0),
    ) -> bool:
        """Whether ``gross_pct`` can clear the size-independent cost floor + min ROI.

        Mirrors the assembler pre-gate (_fee_floor_pct + min_roi) exactly for the size-
        independent terms (round-trip CEX taker rate + stablecoin conversion), and adds
        fixed USD costs (gas/bridge) amortized over ``cost_amortization_usd`` — chosen
        large so that term is a strict LOWER bound on the real %-cost. Every term here is
        <= the cost the full pipeline charges, so ``gross_pct < floor + min_roi`` proves
        the candidate is a downstream reject at every size: skipping it never drops a
        publishable opportunity. Returns True (keep) when the floor is disabled.
        """
        if not self.economic_floor_enabled:
            return True
        floor = Decimal(0)
        if buy_type != "DEX":
            floor += self.cex_taker_rates.get(buy_venue, Decimal(0)) * Decimal(100)
        if sell_type != "DEX":
            floor += self.cex_taker_rates.get(sell_venue, Decimal(0)) * Decimal(100)
        if quote_asset == "cross":
            floor += self.stablecoin_crossquote_bps / Decimal(100)
        if fixed_cost_usd > 0 and self.cost_amortization_usd > 0:
            floor += fixed_cost_usd / self.cost_amortization_usd * Decimal(100)
        return gross_pct >= floor + self.min_roi_pct

    def funding_breakeven_annualized(self, buy_venue: str, sell_venue: str) -> Decimal:
        """Economically-derived minimum annualized funding differential (replaces the fixed
        noise floor). Equals the assembler's funding publish condition solved for the
        annualized rate: a carry of ``size × annualized × hold_fraction`` must clear the
        round-trip taker fee AND the minimum net profit. Uses the same taker rates, size
        and horizon the assembler uses, so it drops only candidates the assembler rejects.
        Never falls below ``funding_min_annualized_spread`` (absolute noise floor)."""
        if not self.economic_floor_enabled:
            return self.funding_min_annualized_spread
        hold_fraction = self.funding_hold_hours / Decimal(24 * 365)
        if hold_fraction <= 0 or self.funding_position_size_usd <= 0:
            return self.funding_min_annualized_spread
        taker = (self.cex_taker_rates.get(buy_venue, Decimal(0))
                 + self.cex_taker_rates.get(sell_venue, Decimal(0)))
        breakeven = (self.min_net_profit_usd / self.funding_position_size_usd
                     + taker) / hold_fraction
        return max(breakeven, self.funding_min_annualized_spread)

    def is_active_route(self, arb_type: str, base: str, quote: str,
                        buy_venue: str, sell_venue: str, network: str | None) -> bool:
        """Whether this route currently has an active published signal (§12.4). Detectors
        must NOT economically prune such a route — the candidate has to reach the assembler
        so a collapsed spread promptly expires the live signal. Short-circuits when no
        signals are active (the usual case), so the hot path pays nothing."""
        if not self.active_routes:
            return False
        venues = tuple(sorted((buy_venue, sell_venue)))
        return (arb_type, base, quote, venues, network) in self.active_routes


class Detector(ABC):
    arb_type: str

    def __init__(self) -> None:
        # Per-detector runtime counters (§17 diagnostics). Owned by the detector so the
        # hot path is a plain in-place int increment; the engine aggregates and resets
        # them once a minute via DetectorStats. Subclasses with their own __init__ must
        # call super().__init__().
        self.counters = DetectorCounters()

    @abstractmethod
    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        ...

    @staticmethod
    def _gross_spread_pct(buy_price: Decimal, sell_price: Decimal) -> Decimal:
        if buy_price <= 0:
            return Decimal(0)
        return (sell_price - buy_price) / buy_price * Decimal(100)
