"""Product catalogue — resolves a ProductCode to its current Product.

In-code rather than a table: there is one product, no admin UI to manage it, and a
price that changes by deploy. The amount comes from configuration
(``PRICE_PRO_LIFETIME_USD``), so changing the price is one environment value and
nothing else — no migration, no data fix-up, no redeploy of business logic.

Should products ever need runtime editing, this class is the seam to swap for a
repository-backed one; every caller already goes through it.
"""
from __future__ import annotations

from decimal import Decimal

from src.config.settings import Settings
from src.domain.enums import SubscriptionTier
from src.domain.product import Product, ProductCode


class UnknownProduct(KeyError):
    """Raised for a product code the catalogue does not define."""


class ProductCatalog:
    def __init__(self, settings: Settings) -> None:
        self._products: dict[ProductCode, Product] = {
            ProductCode.PRO_LIFETIME: Product(
                code=ProductCode.PRO_LIFETIME,
                display_name="AtlasArb Pro Lifetime",
                description=(
                    "One-time purchase. Unlimited arbitrage signals, forever, "
                    "with every future update included."
                ),
                currency="USD",
                amount=Decimal(str(settings.price_pro_lifetime_usd)).quantize(
                    Decimal("0.01")),
                grants_tier=SubscriptionTier.PRO_LIFETIME,
                active=True,
            ),
        }

    def get(self, code: ProductCode) -> Product:
        try:
            return self._products[code]
        except KeyError as exc:
            raise UnknownProduct(str(code)) from exc

    def pro_lifetime(self) -> Product:
        """The only purchasable product today — a named accessor so callers do not
        reach for a code constant to ask the obvious question."""
        return self.get(ProductCode.PRO_LIFETIME)

    def active(self) -> list[Product]:
        return [p for p in self._products.values() if p.active]
