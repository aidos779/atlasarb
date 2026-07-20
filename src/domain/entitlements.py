"""Tier entitlement matrix — single source of truth for all tier gating.

Under the two-plan model every *feature* is open on both tiers: Free users get the
full bot (filters, history, favorites, alerts, all arbitrage types, real-time
delivery). The single axis that separates the plans is ``signal_quota`` — how many
arbitrage signals may be delivered to the user in total, ever.

The per-feature fields below are therefore identical across tiers. They are kept
(rather than deleted) because they remain the enforcement seam every handler already
calls through: adding a future capped plan is a matrix edit, not a code change.
Every tier check in the codebase still resolves through this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.enums import ArbitrageType, SubscriptionTier

UNLIMITED = -1  # sentinel: no cap

#: Signals a Free user may be delivered before the paywall closes (business rule).
FREE_SIGNAL_QUOTA = 5


@dataclass(frozen=True)
class Entitlements:
    tier: SubscriptionTier
    signal_quota: int                  # total deliverable signals (UNLIMITED for Pro)
    signal_delay_sec: int
    signals_per_refresh: int
    instant_alerts_per_hour: int
    max_favorite_coins: int
    max_favorite_exchanges: int
    max_favorite_signals: int
    allowed_arb_types: frozenset[ArbitrageType]
    # filter fields the tier may edit
    filter_fields: frozenset[str]
    history_enabled: bool
    details_advanced: bool
    favorite_entity_alerts: bool

    @property
    def unlimited_signals(self) -> bool:
        return self.signal_quota == UNLIMITED

    def arb_type_allowed(self, arb_type: ArbitrageType) -> bool:
        return arb_type in self.allowed_arb_types

    def filter_allowed(self, field_name: str) -> bool:
        return field_name in self.filter_fields

    def can_add_favorite(self, kind: str, current_count: int) -> bool:
        cap = {
            "coin": self.max_favorite_coins,
            "exchange": self.max_favorite_exchanges,
            "signal": self.max_favorite_signals,
        }[kind]
        return cap == UNLIMITED or current_count < cap


_ALL_FILTERS = frozenset({"min_profit", "coin", "exchange", "network", "liquidity",
                          "arbitrage_type", "risk", "signal_age"})
# Annotated explicitly: ArbitrageType is a StrEnum, so frozenset(ArbitrageType) infers
# as frozenset[str] and would silently widen the field's element type.
_ALL_ARB_TYPES: frozenset[ArbitrageType] = frozenset(ArbitrageType)


def _full_access(tier: SubscriptionTier, signal_quota: int) -> Entitlements:
    """Every feature open; plans differ only in how many signals may be delivered."""
    return Entitlements(
        tier=tier,
        signal_quota=signal_quota,
        signal_delay_sec=0,
        signals_per_refresh=UNLIMITED,
        instant_alerts_per_hour=UNLIMITED,
        max_favorite_coins=UNLIMITED,
        max_favorite_exchanges=UNLIMITED,
        max_favorite_signals=UNLIMITED,
        allowed_arb_types=_ALL_ARB_TYPES,
        filter_fields=_ALL_FILTERS,
        history_enabled=True,
        details_advanced=True,
        favorite_entity_alerts=True,
    )


_MATRIX: dict[SubscriptionTier, Entitlements] = {
    SubscriptionTier.FREE: _full_access(SubscriptionTier.FREE, FREE_SIGNAL_QUOTA),
    SubscriptionTier.PRO_LIFETIME: _full_access(SubscriptionTier.PRO_LIFETIME, UNLIMITED),
}


def distinct_signal_delays() -> tuple[int, ...]:
    """Every delay the dispatcher needs a delivery pass for, ascending.

    Derived from the matrix rather than hardcoded: both current plans deliver in
    real time, so this is a single 0s pass. A hardcoded (0, 10, 60) would run two
    passes that can never match a user — each one re-loading every candidate user
    from the database, per signal.
    """
    return tuple(sorted({e.signal_delay_sec for e in _MATRIX.values()}))


def entitlements_for(tier: SubscriptionTier) -> Entitlements:
    # Dev-build override: any resolved tier maps to full Pro entitlements. Covers
    # the few call sites that pass a raw tier rather than effective_tier. Reverting
    # is a single flag flip (domain/dev_mode); the matrix itself is unchanged.
    from src.domain.dev_mode import unlimited_access_enabled
    if unlimited_access_enabled():
        return _MATRIX[SubscriptionTier.PRO_LIFETIME]
    return _MATRIX[tier]


@dataclass(frozen=True)
class TierPricing:
    tier: SubscriptionTier
    price_usd: float
    one_time: bool = True
    features: list[str] = field(default_factory=list)
