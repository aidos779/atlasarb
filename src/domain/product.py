"""Purchasable products — the single definition of what can be bought and for how much.

Prices live here (fed by configuration), never inline in a handler or a service. A
price change is one value in one place; adding a second product is one entry in the
catalogue plus the tier it grants.

``grants_tier`` is what keeps the purchase flow product-driven: activation asks the
product which entitlement it confers rather than assuming Pro, so a future product
(a bundle, a promo SKU) needs no branching in the purchase machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from src.domain.enums import SubscriptionTier


class ProductCode(StrEnum):
    PRO_LIFETIME = "PRO_LIFETIME"


@dataclass(frozen=True)
class Product:
    code: ProductCode
    display_name: str
    description: str
    currency: str
    amount: Decimal
    grants_tier: SubscriptionTier
    active: bool = True

    @property
    def amount_float(self) -> float:
        """For the Numeric DB columns and the provider APIs, which speak float."""
        return float(self.amount)

    def format_amount(self) -> str:
        """Bare number for display; the currency is rendered by the caller's copy.

        Drops meaningless cents ("20.00" -> "20") while keeping real ones ("49.50" ->
        "49.5"). Done on the Decimal rather than via float, and guarding the exponent —
        ``Decimal("20.00").normalize()`` is ``2E+1``, which would render as "2E+1".
        """
        normalized = self.amount.normalize()
        # as_tuple().exponent is int for finite values but 'n'/'N'/'F' for NaN/Infinity,
        # which a configured price can never be — the guard keeps the checker honest.
        exponent = normalized.as_tuple().exponent
        if isinstance(exponent, int) and exponent > 0:
            normalized = normalized.quantize(Decimal(1))
        return f"{normalized:f}"
