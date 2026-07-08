"""Tier entitlement matrix — single source of truth for all tier gating.

Encodes PRD §15.2 (feature comparison), §13.3 (alert caps/delay), BR-FAV-1
(favorite caps), §12.3 (filter availability), BR-SIG-1 (delay/cap). Every tier
check in the codebase resolves through this module — no duplicated tier logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.enums import ArbitrageType, SubscriptionTier

UNLIMITED = -1  # sentinel: no cap


@dataclass(frozen=True)
class Entitlements:
    tier: SubscriptionTier
    signal_delay_sec: int              # BR-SIG-1 / §13.3
    signals_per_refresh: int           # §15.2 (UNLIMITED for Pro)
    instant_alerts_per_hour: int       # §13.3
    max_favorite_coins: int            # BR-FAV-1
    max_favorite_exchanges: int
    max_favorite_signals: int
    allowed_arb_types: frozenset[ArbitrageType]  # §15.2
    # filter fields the tier may edit — §12.3
    filter_fields: frozenset[str]
    history_enabled: bool              # §15.2 /history
    details_advanced: bool             # Historical Performance & Trade Route §10.6/§10.7
    favorite_entity_alerts: bool       # Favorite Coin/Exchange alerts §13.1 (Basic+)

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


_BASE_FILTERS = frozenset({"min_profit", "coin", "exchange"})
_BASIC_FILTERS = _BASE_FILTERS | {"network", "liquidity", "arbitrage_type"}
_PRO_FILTERS = _BASIC_FILTERS | {"risk", "signal_age"}

_CEX_ONLY = frozenset({ArbitrageType.CEX_CEX})
_BASIC_TYPES = _CEX_ONLY | {ArbitrageType.CEX_DEX, ArbitrageType.DEX_DEX}
_PRO_TYPES = _BASIC_TYPES | {ArbitrageType.FUNDING, ArbitrageType.CROSS_CHAIN}

_MATRIX: dict[SubscriptionTier, Entitlements] = {
    SubscriptionTier.FREE: Entitlements(
        tier=SubscriptionTier.FREE,
        signal_delay_sec=60,
        signals_per_refresh=5,
        instant_alerts_per_hour=3,
        max_favorite_coins=3,
        max_favorite_exchanges=1,
        max_favorite_signals=5,
        allowed_arb_types=_CEX_ONLY,
        filter_fields=_BASE_FILTERS,
        history_enabled=False,
        details_advanced=False,
        favorite_entity_alerts=False,
    ),
    SubscriptionTier.BASIC: Entitlements(
        tier=SubscriptionTier.BASIC,
        signal_delay_sec=10,
        signals_per_refresh=20,
        instant_alerts_per_hour=20,
        max_favorite_coins=10,
        max_favorite_exchanges=3,
        max_favorite_signals=25,
        allowed_arb_types=_BASIC_TYPES,
        filter_fields=_BASIC_FILTERS,
        history_enabled=True,
        details_advanced=True,
        favorite_entity_alerts=True,
    ),
    SubscriptionTier.PRO: Entitlements(
        tier=SubscriptionTier.PRO,
        signal_delay_sec=0,
        signals_per_refresh=UNLIMITED,
        instant_alerts_per_hour=UNLIMITED,
        max_favorite_coins=UNLIMITED,
        max_favorite_exchanges=UNLIMITED,
        max_favorite_signals=UNLIMITED,
        allowed_arb_types=_PRO_TYPES,
        filter_fields=_PRO_FILTERS,
        history_enabled=True,
        details_advanced=True,
        favorite_entity_alerts=True,
    ),
}


def entitlements_for(tier: SubscriptionTier) -> Entitlements:
    # Dev-build override: any resolved tier maps to full PRO entitlements. Covers
    # the few call sites that pass a raw tier rather than effective_tier. Reverting
    # is a single flag flip (domain/dev_mode); the matrix itself is unchanged.
    from src.domain.dev_mode import unlimited_access_enabled
    if unlimited_access_enabled():
        return _MATRIX[SubscriptionTier.PRO]
    return _MATRIX[tier]


@dataclass(frozen=True)
class TierPricing:
    tier: SubscriptionTier
    monthly_usd: float
    features: list[str] = field(default_factory=list)
