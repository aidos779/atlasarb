"""Domain enums — single source of truth for constrained value sets.

Cross-references: PRD §3.8 (arb types), §3.9 (status/risk), §5.1 (roles),
§15 (tiers), §19/§11 (ranking), §20/§14 (exchange status).
"""
from __future__ import annotations

from enum import StrEnum


class ArbitrageType(StrEnum):
    """PRD §3.8 authoritative list."""

    CEX_CEX = "CEX_CEX"
    CEX_DEX = "CEX_DEX"
    DEX_DEX = "DEX_DEX"
    FUNDING = "FUNDING"
    CROSS_CHAIN = "CROSS_CHAIN"

    @property
    def label(self) -> str:
        return {
            "CEX_CEX": "CEX ↔ CEX",
            "CEX_DEX": "CEX ↔ DEX",
            "DEX_DEX": "DEX ↔ DEX",
            "FUNDING": "Funding Rate",
            "CROSS_CHAIN": "Cross Chain",
        }[self.value]


class SignalStatus(StrEnum):
    """PRD §3.9. Hidden is always per-user (R-SCHEMA-1)."""

    ACTIVE = "Active"
    EXPIRED = "Expired"
    HIDDEN = "Hidden"


class RiskScore(StrEnum):
    """PRD §10.5 three-level classification."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"

    @property
    def emoji(self) -> str:
        return {"Low": "🟢", "Medium": "🟡", "High": "🔴"}[self.value]

    @property
    def numeric(self) -> int:
        """1..5 mapping for the Risk filter slider (PRD §12.3)."""
        return {"Low": 1, "Medium": 3, "High": 5}[self.value]


class RankingLevel(StrEnum):
    """PRD §19 / Scanner §11."""

    TOP = "TOP"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def emoji(self) -> str:
        return {"TOP": "⭐", "HIGH": "🟢", "MEDIUM": "🟡", "LOW": "⚪"}[self.value]


class ExchangeStatus(StrEnum):
    """PRD §20 / Scanner §14."""

    ONLINE = "Online"
    MAINTENANCE = "Maintenance"
    API_OFFLINE = "API Offline"
    UNKNOWN = "Unknown"

    @property
    def emoji(self) -> str:
        return {
            "Online": "🟢",
            "Maintenance": "🟡",
            "API Offline": "🔴",
            "Unknown": "⚪",
        }[self.value]

    @property
    def signal_allowed(self) -> bool:
        """Scanner §14.1 — only Online permits signal generation."""
        return self is ExchangeStatus.ONLINE


class VenueType(StrEnum):
    CEX = "CEX"
    DEX = "DEX"


class UserRole(StrEnum):
    """PRD §5.1."""

    VISITOR = "visitor"
    FREE = "free"
    PAID = "paid"
    SUPPORT = "support"
    ADMIN = "admin"


class SubscriptionTier(StrEnum):
    """PRD §15."""

    FREE = "free"
    BASIC = "basic"
    PRO = "pro"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Network(StrEnum):
    """PRD §3.5 supported networks."""

    ETHEREUM = "ethereum"
    BNB = "bnb"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    BASE = "base"
    POLYGON = "polygon"
    SOLANA = "solana"

    @property
    def display(self) -> str:
        return {
            "ethereum": "Ethereum",
            "bnb": "BNB Chain",
            "arbitrum": "Arbitrum",
            "optimism": "Optimism",
            "base": "Base",
            "polygon": "Polygon",
            "solana": "Solana",
        }[self.value]


class QuoteAsset(StrEnum):
    """PRD §3.6 — MVP scans USDT/USDC only."""

    USDT = "USDT"
    USDC = "USDC"


class Language(StrEnum):
    EN = "en"
    RU = "ru"
    KK = "kk"


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    KZT = "KZT"
    RUB = "RUB"
    USDT = "USDT"


class RejectReason(StrEnum):
    """Scanner §10 validation reject reasons + §15.5 / §3.2."""

    BELOW_MIN_PROFIT = "BELOW_MIN_PROFIT"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    UNPROFITABLE_AFTER_FEES = "UNPROFITABLE_AFTER_FEES"
    RISK_TOO_HIGH = "RISK_TOO_HIGH"
    STALE_DATA = "STALE_DATA"
    EXCHANGE_NOT_ONLINE = "EXCHANGE_NOT_ONLINE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    IMPLAUSIBLE_SPREAD = "IMPLAUSIBLE_SPREAD"
    UNVERIFIED_TOKEN = "UNVERIFIED_TOKEN"
    NO_BRIDGE_ROUTE = "NO_BRIDGE_ROUTE"
    BRIDGE_TIME_EXCEEDED = "BRIDGE_TIME_EXCEEDED"
    GAS_EXCEEDS_LIMIT = "GAS_EXCEEDS_LIMIT"
    NOT_WARMED_UP = "NOT_WARMED_UP"
    MISSING_FEE_DATA = "MISSING_FEE_DATA"
    NO_VIABLE_SIZE = "NO_VIABLE_SIZE"


class ExpiryReason(StrEnum):
    """Scanner §12.4."""

    TTL_EXCEEDED = "TTL_EXCEEDED"
    SPREAD_CLOSED = "SPREAD_CLOSED"
    VENUE_OFFLINE = "VENUE_OFFLINE"
    MARKET_DELISTED = "MARKET_DELISTED"
    BRIDGE_CAPACITY = "BRIDGE_CAPACITY"
    FUNDING_SETTLED = "FUNDING_SETTLED"
