"""User domain aggregate: profile, settings, filters — and filter-matching logic.

Filter matching (PRD BR-FILTER-1) is pure domain logic reused by both the Signal
List query and Instant Alert eligibility (§12.1) — one implementation, no drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.enums import (
    ArbitrageType,
    Currency,
    Language,
    SubscriptionStatus,
    SubscriptionTier,
    UserRole,
)
from src.domain.signal import Signal


@dataclass
class UserFilter:
    """PRD §12 persisted per-user filter set."""

    min_profit_pct: Decimal = Decimal("0.3")
    coins: frozenset[str] = field(default_factory=frozenset)          # empty = Any
    exchanges: frozenset[str] = field(default_factory=frozenset)
    networks: frozenset[str] = field(default_factory=frozenset)
    min_liquidity_usd: Decimal = Decimal(0)
    max_risk_numeric: int = 5                                          # 1..5, Pro+
    max_signal_age_sec: int = 0                                        # 0 = no limit
    arb_types: frozenset[ArbitrageType] = field(default_factory=frozenset)  # empty=all allowed
    scan_all_assets: bool = False                                     # BR-ASSET-4

    def matches(self, signal: Signal) -> bool:
        """AND across categories, OR within a multi-select (BR-FILTER-1)."""
        if signal.net_profit_pct < self.min_profit_pct:
            return False
        if self.coins and signal.coin not in self.coins:
            return False
        if self.exchanges and not (
            signal.buy_exchange in self.exchanges or signal.sell_exchange in self.exchanges
        ):
            return False
        if self.networks and (signal.network or "") not in self.networks:
            return False
        if self.min_liquidity_usd and signal.liquidity_usd < self.min_liquidity_usd:
            return False
        if signal.risk_score.numeric > self.max_risk_numeric:
            return False
        if self.max_signal_age_sec and signal.age_sec() > self.max_signal_age_sec:
            return False
        if self.arb_types and signal.arb_type not in self.arb_types:
            return False
        return True


@dataclass
class UserSettings:
    """PRD §14."""

    language: Language = Language.EN
    timezone: str = "UTC"
    currency: Currency = Currency.USD
    daily_summary_enabled: bool = False
    daily_summary_time: str = "08:00"          # HH:MM in user's timezone
    instant_alerts_enabled: bool = True
    favorite_coin_alerts: bool = False
    favorite_exchange_alerts: bool = False


@dataclass
class Subscription:
    tier: SubscriptionTier = SubscriptionTier.FREE
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    period_end: float | None = None            # epoch sec
    auto_renew: bool = True
    retries_used: int = 0

    @property
    def is_paid_active(self) -> bool:
        """R-ROLE-3 derived Paid state."""
        return self.tier != SubscriptionTier.FREE and self.status == SubscriptionStatus.ACTIVE


@dataclass
class UserProfile:
    """Aggregate root for a bot user."""

    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    role: UserRole = UserRole.VISITOR
    subscription: Subscription = field(default_factory=Subscription)
    settings: UserSettings = field(default_factory=UserSettings)
    filter: UserFilter = field(default_factory=UserFilter)
    onboarding_step: int = 0                   # 0=none,1=lang,2=tz,3=done
    created_at: float | None = None
    suspended: bool = False
    pending_deeplink: str | None = None

    @property
    def effective_tier(self) -> SubscriptionTier:
        # Dev-build override: unlock full PRO for everyone (see domain/dev_mode).
        # Production never enables this, so real tier logic is untouched.
        from src.domain.dev_mode import unlimited_access_enabled
        if unlimited_access_enabled():
            return SubscriptionTier.PRO
        if self.subscription.is_paid_active:
            return self.subscription.tier
        return SubscriptionTier.FREE

    @property
    def onboarding_complete(self) -> bool:
        return self.onboarding_step >= 3

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def is_support(self) -> bool:
        return self.role == UserRole.SUPPORT

    def is_staff(self) -> bool:
        return self.role in (UserRole.ADMIN, UserRole.SUPPORT)
