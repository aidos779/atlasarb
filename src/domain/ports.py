"""Ports (interfaces) — the boundaries the core depends on (Dependency Inversion).

The scanning engine and services depend only on these abstractions, never on a
concrete adapter/DB/telegram implementation (ARCH-1: engine has no venue-specific
branching; Clean Architecture).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Protocol

from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook, PriceQuote
from src.domain.signal import Signal


class ExchangeAdapter(ABC):
    """Scanner §2.1 adapter contract. Every CEX/DEX integration implements this.

    Adapters own their own connectivity, rate-limiting, reconnect, normalization
    and health — the engine consumes only this interface (ARCH-1/ARCH-2).
    """

    id: str
    display_name: str
    venue_type: VenueType

    @abstractmethod
    async def connect(self) -> None:
        """Establish REST session + WS connection(s)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Graceful teardown."""

    @abstractmethod
    async def get_markets(self) -> list[CanonicalSymbol]:
        """List tradable USDT-quoted symbols/pools (§3.6)."""

    @abstractmethod
    async def subscribe_ticker(self, symbols: list[CanonicalSymbol]) -> None:
        """Start streaming best bid/ask + last price."""

    @abstractmethod
    async def subscribe_order_book(self, symbols: list[CanonicalSymbol], depth: int) -> None:
        """Start streaming order-book updates."""

    @abstractmethod
    async def get_funding_rate(self, base_asset: str) -> FundingRate | None:
        """Perp venues only — current + predicted funding."""

    @abstractmethod
    async def get_pool_state(self, symbol: CanonicalSymbol) -> OrderBook | None:
        """DEX-only — reserves/liquidity, fee tier, price."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Lightweight liveness probe."""

    @abstractmethod
    def get_status(self) -> ExchangeStatus:
        """Current adapter status enum (§14)."""

    # ── fee / withdrawal metadata (§8.3/§8.4) ──
    @abstractmethod
    def taker_fee(self, symbol: CanonicalSymbol) -> Decimal:
        """Taker fee rate for a leg (MVP default per §8.3)."""

    @abstractmethod
    def withdrawal_fee_usd(self, base_asset: str, network: str | None) -> Decimal | None:
        """Flat withdrawal fee in USD per (asset, network); None if unknown (§8.4)."""

    @abstractmethod
    def withdrawals_enabled(self, base_asset: str, network: str | None) -> bool:
        """Whether withdrawals are currently open (§7.1 validation)."""


class MarketDataSink(Protocol):
    """Where collectors write normalized data (the Market State Cache)."""

    def upsert_price(self, quote: PriceQuote) -> None: ...
    def upsert_book(self, book: OrderBook) -> None: ...
    def upsert_funding(self, funding: FundingRate) -> None: ...


class NotificationQueue(Protocol):
    """Hand-off boundary to Part 2 bot layer (Scanner §1.2 / §16)."""

    async def publish(self, signal: Signal, event: str) -> None: ...


class SignalHistoryStore(Protocol):
    """Persist expired signals with lifecycle trace (Scanner §12.5)."""

    async def archive(self, signal: Signal) -> None: ...


class GasPriceProvider(Protocol):
    """Live per-network gas price in USD terms (Scanner §8.5)."""

    async def gas_price_usd(self, network: str, gas_units: int) -> Decimal | None: ...


class FxRateProvider(Protocol):
    """USD -> display-currency conversion, <=5min stale (FR-LOC-02)."""

    async def convert(self, amount_usd: Decimal, currency: str) -> Decimal: ...
