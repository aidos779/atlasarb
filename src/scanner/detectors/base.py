"""Detector base + shared context (Scanner §7).

Detectors are stateless (read-only from the Market State Cache, §1.1 principle 3).
Each identifies which venues form an opportunity and its direction, emitting raw
Candidates; the shared profit engine (§8) and validator (§10) run downstream.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.enums import VenueType
from src.domain.signal import Candidate
from src.scanner.cache.market_state_cache import MarketStateCache
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

    def online_cex_prices(self, pair: str) -> list[tuple[str, object]]:
        out = []
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
        return out

    def online_dex_books(self, pair: str) -> list[tuple[str, object]]:
        out = []
        for venue in self.cache.venues_for_pair(pair):
            info = self.venues.get(venue)
            if not info or info.venue_type != VenueType.DEX:
                continue
            if not self.health.is_online(venue):
                continue
            book = self.cache.get_book(venue, pair)
            if book and book.reserve_base:
                out.append((venue, book))
        return out


class Detector(ABC):
    arb_type: str

    @abstractmethod
    def detect(self, ctx: DetectionContext, base_asset: str, quote_asset: str) -> list[Candidate]:
        ...

    @staticmethod
    def _gross_spread_pct(buy_price: Decimal, sell_price: Decimal) -> Decimal:
        if buy_price <= 0:
            return Decimal(0)
        return (sell_price - buy_price) / buy_price * Decimal(100)
