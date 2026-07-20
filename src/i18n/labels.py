"""Localized display labels for domain enums.

The enums carry English values because they are also wire/DB identifiers ("Low",
"Active", "CEX_CEX"). Rendering those directly is how English leaked into a Russian UI —
a signal card read "Risk: 🟢 Low" no matter the language. Handlers and formatters must
use these helpers instead of ``enum.value`` / ``enum.label`` for anything a user sees.

Filter field names get the same treatment: they were rendered with
``field.replace("_", " ").title()``, which is English-only string manipulation and can
never localize.
"""
from __future__ import annotations

from src.domain.enums import ArbitrageType, RiskScore, SignalStatus, SubscriptionTier
from src.domain.purchase import PurchaseStatus
from src.i18n.translator import t

_RISK_KEY = {
    RiskScore.LOW: "risk.low",
    RiskScore.MEDIUM: "risk.medium",
    RiskScore.HIGH: "risk.high",
}
_RISK_EXPLAIN_KEY = {
    RiskScore.LOW: "risk.explain.low",
    RiskScore.MEDIUM: "risk.explain.medium",
    RiskScore.HIGH: "risk.explain.high",
}
_STATUS_KEY = {
    SignalStatus.ACTIVE: "status.active",
    SignalStatus.EXPIRED: "status.expired",
    SignalStatus.HIDDEN: "status.hidden",
}
_ARB_KEY = {
    ArbitrageType.CEX_CEX: "arb.cex_cex",
    ArbitrageType.CEX_DEX: "arb.cex_dex",
    ArbitrageType.DEX_DEX: "arb.dex_dex",
    ArbitrageType.FUNDING: "arb.funding",
    ArbitrageType.CROSS_CHAIN: "arb.cross_chain",
}
_TIER_KEY = {
    SubscriptionTier.FREE: "tier.free",
    SubscriptionTier.PRO_LIFETIME: "tier.pro_lifetime",
}
_PURCHASE_STATUS_KEY = {
    PurchaseStatus.CREATED: "purchase.created",
    PurchaseStatus.PENDING: "purchase.pending",
    PurchaseStatus.PAID: "purchase.paid",
    PurchaseStatus.FAILED: "purchase.failed",
    PurchaseStatus.CANCELLED: "purchase.cancelled",
    PurchaseStatus.EXPIRED: "purchase.expired",
}
#: Filter field id -> catalog key. Keys mirror the ids used in callback data.
_FILTER_KEY = {
    "min_profit": "filters.min_profit",
    "coin": "filters.coin",
    "exchange": "filters.exchange",
    "network": "filters.network",
    "liquidity": "filters.liquidity",
    "risk": "filters.risk",
    "signal_age": "filters.signal_age",
    "arbitrage_type": "filters.arbitrage_type",
}
#: Language display names, each written in its own language — a user scanning for their
#: language recognises "Русский", not a translation of it. Not catalog keys for that
#: reason: these must render identically regardless of the current UI language.
_LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}

_FAVORITE_KIND_KEY = {
    "signal": "favorites.kind.signal",
    "coin": "favorites.kind.coin",
    "exchange": "favorites.kind.exchange",
}


def risk_label(risk: RiskScore, lang: str) -> str:
    return t(_RISK_KEY[risk], lang)


def risk_explanation(risk: RiskScore, lang: str) -> str:
    return t(_RISK_EXPLAIN_KEY[risk], lang)


def status_label(status: SignalStatus, lang: str) -> str:
    return t(_STATUS_KEY[status], lang)


def arb_type_label(arb_type: ArbitrageType, lang: str) -> str:
    return t(_ARB_KEY[arb_type], lang)


def tier_label(tier: SubscriptionTier | str, lang: str) -> str:
    if isinstance(tier, str):
        tier = SubscriptionTier(tier)
    return t(_TIER_KEY[tier], lang)


def purchase_status_label(status: PurchaseStatus | str, lang: str) -> str:
    if isinstance(status, str):
        status = PurchaseStatus(status)
    return t(_PURCHASE_STATUS_KEY[status], lang)


def filter_label(field: str, lang: str) -> str:
    """Localized name of a filter field; unknown ids degrade to the raw id."""
    key = _FILTER_KEY.get(field)
    return t(key, lang) if key else field


def favorite_kind_label(kind: str, lang: str) -> str:
    key = _FAVORITE_KIND_KEY.get(kind)
    return t(key, lang) if key else kind


def language_name(code: str) -> str:
    """Display name for a language code, in that language."""
    return _LANGUAGE_NAMES.get(code, code)
