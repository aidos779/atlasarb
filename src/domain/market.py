"""Normalized market-data structures written to the Market State Cache.

Adapters map raw venue payloads into these (Scanner §3.3 canonical model, §4, §5, §6).
All prices are Decimal to avoid float drift in financial math.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.enums import VenueType


@dataclass(frozen=True)
class CanonicalSymbol:
    """Scanner §3.3 canonical internal representation."""

    base_asset: str            # uppercased, alias-resolved
    quote_asset: str           # USDT | USDC
    venue_type: VenueType
    network: str | None = None  # required for DEX; None for CEX

    @property
    def pair(self) -> str:
        return f"{self.base_asset}/{self.quote_asset}"

    def key(self) -> tuple[str, str, str, str | None]:
        return (self.base_asset, self.quote_asset, self.venue_type.value, self.network)


@dataclass
class PriceQuote:
    """Top-of-book / ticker snapshot for one (venue, symbol)."""

    venue: str
    symbol: CanonicalSymbol
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: float = field(default_factory=time.time)   # source event time (epoch sec)
    received_at: float = field(default_factory=time.time)
    source: str = "WS"                              # WS | REST_RECONCILE | RPC | QUOTE

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread_bps(self) -> Decimal:
        if self.mid == 0:
            return Decimal(0)
        return (self.ask - self.bid) / self.mid * Decimal(10000)

    def staleness(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at


@dataclass
class BookLevel:
    price: Decimal
    qty: Decimal


@dataclass
class OrderBook:
    """L2 book for CEX, or synthesized reserve curve summary for DEX."""

    venue: str
    symbol: CanonicalSymbol
    bids: list[BookLevel]      # descending price
    asks: list[BookLevel]      # ascending price
    ts: float = field(default_factory=time.time)
    received_at: float = field(default_factory=time.time)
    sequence: int | None = None
    # DEX pool metadata (None for CEX)
    pool_address: str | None = None
    pool_fee_tier: Decimal | None = None
    reserve_base: Decimal | None = None
    reserve_quote: Decimal | None = None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def is_crossed(self) -> bool:
        return bool(self.best_bid and self.best_ask and self.best_bid >= self.best_ask)

    def is_empty_side(self) -> bool:
        # AMM/pool books express liquidity as constant-product reserves, not L2
        # levels — they legitimately carry no bids/asks. Treating them as "empty"
        # dropped every DEX book at the cache, killing CEX-DEX / DEX-DEX /
        # cross-chain detection entirely. A pool book is valid when both reserves
        # are present and positive.
        if self.reserve_base and self.reserve_quote:
            return self.reserve_base <= 0 or self.reserve_quote <= 0
        return not self.bids or not self.asks

    def staleness(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at


@dataclass
class FundingRate:
    """Scanner §6 funding data for one (venue, perp symbol)."""

    venue: str
    base_asset: str
    current_rate: Decimal
    predicted_rate: Decimal | None
    next_funding_time: float        # absolute UTC epoch sec (§6.2)
    interval_hours: int             # 1 | 4 | 8
    received_at: float = field(default_factory=time.time)

    def annualized(self) -> Decimal:
        periods_per_day = Decimal(24) / Decimal(self.interval_hours)
        return self.current_rate * periods_per_day * Decimal(365)

    def staleness(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at
