"""Signal domain entities — the canonical Signal object handed to Notification.

Implements PRD §3.9 canonical Signal Data Schema plus Scanner §8.12 sizing fields.
Split into: LegRef (venue leg), Candidate (raw detector output before profit/rank),
ProfitBreakdown (§8 output), Signal (validated, ranked, published object).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.enums import (
    ArbitrageType,
    RankingLevel,
    RiskScore,
    SignalStatus,
)

# Stable namespace for deterministic route → signal-id derivation (uuid5). Fixed value:
# changing it would reshuffle every signal id, so it must never change.
_ROUTE_ID_NAMESPACE = uuid.UUID("a7c1f2e0-9b3d-5e64-8f21-0d4c6b8a1e37")


def _route_id(dedup_key: tuple) -> str:
    """Deterministic uuid5 of a route dedup key — stable across the process and restarts."""
    return str(uuid.uuid5(_ROUTE_ID_NAMESPACE, repr(dedup_key)))


@dataclass
class LegRef:
    """One side of an arbitrage (buy or sell venue)."""

    venue: str
    venue_type: str                    # CEX | DEX
    price: Decimal
    network: str | None = None
    pool_address: str | None = None
    fee_rate: Decimal = Decimal(0)     # trading/swap fee rate for the leg


@dataclass
class ProfitBreakdown:
    """Scanner §8 output — every fee category resolved (never assumed-zero, §10)."""

    size_usd: Decimal
    gross_profit_usd: Decimal
    trading_fees_usd: Decimal
    withdrawal_fees_usd: Decimal
    gas_fees_usd: Decimal
    bridge_fees_usd: Decimal
    slippage_cost_usd: Decimal
    conversion_cost_usd: Decimal       # §8.9 stablecoin cross-quote
    net_profit_usd: Decimal
    roi_pct: Decimal
    gross_spread_pct: Decimal
    net_profit_pct: Decimal
    # Which fee categories were actually resolved (§10 Fees gate / §11.5 completeness)
    resolved_categories: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SizingProfile:
    """Scanner §8.11 + §8.12 sizing outputs."""

    recommended_size_usd: Decimal
    max_capital_utilization_usd: Decimal
    max_recommended_position_usd: Decimal
    liquidity_saturation_usd: Decimal
    profit_decay: list[tuple[Decimal, Decimal]]  # (size%, roi%) at 25/50/75/100


@dataclass(frozen=True)
class FundingSnapshot:
    """Immutable funding-data snapshot for one leg, captured by the detector at the
    instant the candidate is formed, so the funding confidence model (§11.5) scores
    exactly the data that produced the opportunity — no re-read race, no default value."""

    received_at: float                  # when the funding sample was received
    current_rate: Decimal
    has_predicted: bool                 # predicted_rate present
    has_next_time: bool                 # next_funding_time present/valid
    history: tuple[Decimal, ...]        # recent funding-rate samples (volatility input)


@dataclass
class Candidate:
    """Raw opportunity emitted by a detector (Scanner §7), pre-validation."""

    arb_type: ArbitrageType
    base_asset: str
    quote_asset: str
    buy_leg: LegRef
    sell_leg: LegRef
    gross_spread_pct: Decimal
    network: str | None = None
    # optional per-type extras
    atomic_execution: bool = False          # §7.3
    bridge_name: str | None = None          # §7.5
    bridge_fee_usd: Decimal | None = None
    bridge_time_sec: int | None = None
    funding_annualized_spread: Decimal | None = None  # §7.4
    funding_next_time: float | None = None
    # Funding-leg data snapshots (§7.4 → §11.5 confidence). low = long/buy leg (lower
    # funding), high = short/sell leg (higher funding). Captured at detection time.
    funding_low: FundingSnapshot | None = None
    funding_high: FundingSnapshot | None = None
    detected_at: float = field(default_factory=time.time)

    def dedup_key(self) -> tuple:
        """Scanner §13.1 order-independent venue-pair key."""
        venues = tuple(sorted((self.buy_leg.venue, self.sell_leg.venue)))
        return (self.arb_type.value, self.base_asset, self.quote_asset, venues, self.network)

    def route_id(self) -> str:
        """Deterministic signal identity for this execution route (§13.1).

        The same underlying opportunity — same arbitrage type, asset, trading pair,
        network and venue pair — always maps to the same id, so price/spread/profit
        updates reuse it and a re-appearance after expiry keeps it. A new id is minted
        only when the route itself changes."""
        return _route_id(self.dedup_key())


@dataclass
class Signal:
    """Validated, ranked signal — the object published to the Notification Queue.

    Carries the full PRD §3.9 canonical field set at all times; UI surfaces subsets.
    """

    # identity / classification
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    arb_type: ArbitrageType = ArbitrageType.CEX_CEX
    coin: str = ""
    trading_pair: str = ""
    network: str | None = None

    # venues / prices
    buy_exchange: str = ""
    sell_exchange: str = ""
    buy_price: Decimal = Decimal(0)
    sell_price: Decimal = Decimal(0)
    buy_venue_type: str = "CEX"
    sell_venue_type: str = "CEX"
    pool_address: str | None = None

    # economics (§3.9)
    spread_pct: Decimal = Decimal(0)
    gross_profit_pct: Decimal = Decimal(0)
    net_profit_pct: Decimal = Decimal(0)
    net_profit_usd: Decimal = Decimal(0)
    trading_fees: Decimal = Decimal(0)
    withdrawal_fees: Decimal = Decimal(0)
    network_fees: Decimal = Decimal(0)
    estimated_slippage: Decimal = Decimal(0)
    conversion_cost: Decimal = Decimal(0)
    liquidity_usd: Decimal = Decimal(0)
    recommended_trade_size_usd: Decimal = Decimal(0)
    roi_pct: Decimal = Decimal(0)

    # sizing (§8.12)
    sizing: SizingProfile | None = None
    profit_breakdown: ProfitBreakdown | None = None

    # scoring
    risk_score: RiskScore = RiskScore.MEDIUM
    confidence_score: int = 0
    ranking: RankingLevel = RankingLevel.LOW
    composite_score: float = 0.0

    # lifecycle (§3.9 / §12)
    signal_lifetime_sec: int = 0
    status: SignalStatus = SignalStatus.ACTIVE
    timestamp: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    expires_at: float = 0.0
    expired_at: float | None = None
    expiry_reason: str | None = None

    # per-type extras
    bridge_name: str | None = None
    bridge_time_sec: int | None = None
    spread_decay_risk: str | None = None
    atomic_execution: bool = False
    funding_annualized_spread: Decimal | None = None
    funding_next_time: float | None = None

    # provenance for confidence/history (§11.5, §12.5)
    warmed_up: bool = True
    price_source_count: int = 1
    input_snapshot: dict = field(default_factory=dict)

    def dedup_key(self) -> tuple:
        venues = tuple(sorted((self.buy_exchange, self.sell_exchange)))
        return (self.arb_type.value, self.coin, self.trading_pair.split("/")[-1],
                venues, self.network)

    def route_id(self) -> str:
        """Deterministic signal identity for this execution route (§13.1).

        The same underlying opportunity — same arbitrage type, asset, trading pair,
        network and venue pair — always maps to the same id, so price/spread/profit
        updates reuse it and a re-appearance after expiry keeps it. A new id is minted
        only when the route itself changes."""
        return _route_id(self.dedup_key())

    def age_sec(self, now: float | None = None) -> int:
        return int((now or time.time()) - self.timestamp)

    def remaining_sec(self, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (now or time.time())))

    def is_low_confidence(self, threshold: float) -> bool:
        """R-SCHEMA-2 — flag for card badge / alert-eligibility."""
        return self.confidence_score < threshold
