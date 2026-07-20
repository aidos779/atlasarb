"""Product catalogue and the purchase lifecycle.

The lifecycle is the part a crypto provider will drive, so these cover the transitions
it will actually exercise — including the ones that must *not* be possible.
"""
import pytest

from src.domain.enums import SubscriptionTier
from src.domain.product import ProductCode
from src.domain.purchase import (
    IllegalPurchaseTransition,
    PurchaseStatus,
    can_transition,
)
from src.services.admin_service import AdminService
from src.services.payments import PlaceholderPaymentProvider
from src.services.payments.provider import Invoice, PaymentReceipt
from src.services.product_catalog import ProductCatalog, UnknownProduct
from src.services.purchase_service import PurchaseService
from src.services.subscription_service import SubscriptionService
from tests.integration.test_subscription import _make_user, _reload

pytestmark = pytest.mark.asyncio  # async tests; sync ones below are unaffected


class _StubProvider:
    """A provider that issues invoices — stands in for CryptoBot/NOWPayments."""

    id = "stub"

    def __init__(self) -> None:
        self.invoices: list[str] = []

    async def create_invoice(self, user_id: int, amount_usd: float, payload: str) -> Invoice:
        self.invoices.append(payload)
        return Invoice(payment_id=f"stub-{payload}", pay_url=f"https://pay.test/{payload}",
                       amount_usd=amount_usd)

    async def verify(self, raw_body: bytes, signature: str) -> PaymentReceipt | None:
        return None


def _service(database, settings, catalog, provider=None) -> PurchaseService:
    subscriptions = SubscriptionService(database, settings, catalog)
    return PurchaseService(database, subscriptions,
                           provider or PlaceholderPaymentProvider())


# ── product catalogue ──

def test_catalog_defines_pro_lifetime(catalog):
    product = catalog.pro_lifetime()
    assert product.code == ProductCode.PRO_LIFETIME
    assert product.currency == "USD"
    assert str(product.amount) == "20.00"
    assert product.format_amount() == "20"
    assert product.grants_tier == SubscriptionTier.PRO_LIFETIME
    assert product.active is True


def test_price_comes_from_configuration(settings):
    """Changing the price is one config value — no code change anywhere."""
    repriced = settings.model_copy(update={"price_pro_lifetime_usd": 49.5})
    assert ProductCatalog(repriced).pro_lifetime().format_amount() == "49.5"


def test_unknown_product_raises(catalog):
    with pytest.raises(UnknownProduct):
        catalog.get("NOT_A_PRODUCT")


def test_pricing_screen_quotes_the_catalog_price(database, settings, catalog):
    plans = SubscriptionService(database, settings, catalog).pricing()
    pro = next(p for p in plans if p.tier == SubscriptionTier.PRO_LIFETIME)
    assert pro.price_usd == catalog.pro_lifetime().amount_float


# ── state machine ──

def test_terminal_states_are_terminal():
    for terminal in (PurchaseStatus.PAID, PurchaseStatus.FAILED,
                     PurchaseStatus.CANCELLED, PurchaseStatus.EXPIRED):
        for target in PurchaseStatus:
            assert not can_transition(terminal, target)


def test_legal_forward_transitions():
    assert can_transition(PurchaseStatus.CREATED, PurchaseStatus.PENDING)
    assert can_transition(PurchaseStatus.PENDING, PurchaseStatus.PAID)
    assert can_transition(PurchaseStatus.CREATED, PurchaseStatus.CANCELLED)
    assert not can_transition(PurchaseStatus.PAID, PurchaseStatus.PENDING)


# ── checkout with no provider ──

async def test_placeholder_provider_cannot_open_checkout(database, settings, catalog):
    """The stub must never fake a payment: no invoice, no grant, purchase closed out."""
    await _make_user(database)
    service = _service(database, settings, catalog)
    checkout = await service.start_checkout(1, catalog.pro_lifetime())

    assert checkout.available is False
    assert checkout.pay_url is None
    settled = await service.get(checkout.purchase.id)
    assert settled.status == PurchaseStatus.CANCELLED
    assert (await _reload(database)).subscription.is_pro is False


# ── full provider flow ──

async def test_full_lifecycle_grants_pro(database, settings, catalog):
    await _make_user(database)
    provider = _StubProvider()
    service = _service(database, settings, catalog, provider)

    checkout = await service.start_checkout(1, catalog.pro_lifetime())
    assert checkout.available and checkout.pay_url.endswith(checkout.purchase.id)
    pending = await service.get(checkout.purchase.id)
    assert pending.status == PurchaseStatus.PENDING
    assert pending.external_payment_id == f"stub-{checkout.purchase.id}"
    # Still not Pro — an issued invoice is not a payment.
    assert (await _reload(database)).subscription.is_pro is False

    paid = await service.mark_paid(checkout.purchase.id,
                                   external_payment_id="chain-tx-0x1")
    assert paid.status == PurchaseStatus.PAID
    assert paid.paid_at is not None
    assert (await _reload(database)).subscription.is_pro is True


async def test_settle_from_a_verified_receipt(database, settings, catalog):
    """The webhook path: receipt payload carries our purchase id."""
    await _make_user(database)
    service = _service(database, settings, catalog, _StubProvider())
    checkout = await service.start_checkout(1, catalog.pro_lifetime())

    receipt = PaymentReceipt(payment_id=checkout.purchase.id, user_id=1, amount_usd=20.0)
    settled = await service.settle(receipt)
    assert settled.status == PurchaseStatus.PAID
    assert (await _reload(database)).subscription.is_pro is True


async def test_duplicate_callbacks_are_ignored(database, settings, catalog):
    await _make_user(database)
    service = _service(database, settings, catalog, _StubProvider())
    checkout = await service.start_checkout(1, catalog.pro_lifetime())

    for _ in range(3):
        await service.mark_paid(checkout.purchase.id, external_payment_id="tx-1")

    assert (await service.get(checkout.purchase.id)).status == PurchaseStatus.PAID
    # Granted once and billed once, despite three callbacks.
    assert await AdminService(database, None).revenue_usd() == 20.0
    assert len(await service.history(1)) == 1


async def test_settling_an_unknown_purchase_is_a_noop(database, settings, catalog):
    service = _service(database, settings, catalog, _StubProvider())
    assert await service.settle(
        PaymentReceipt(payment_id="does-not-exist", user_id=1, amount_usd=20.0)) is None


async def test_cancelled_purchase_cannot_be_paid(database, settings, catalog):
    """A provider reporting success on an abandoned invoice is a bug, and must shout."""
    await _make_user(database)
    service = _service(database, settings, catalog, _StubProvider())
    checkout = await service.start_checkout(1, catalog.pro_lifetime())
    await service.mark_cancelled(checkout.purchase.id, "user walked away")

    with pytest.raises(IllegalPurchaseTransition):
        await service.mark_paid(checkout.purchase.id)
    assert (await _reload(database)).subscription.is_pro is False


async def test_checkout_resumes_instead_of_duplicating(database, settings, catalog):
    """Re-tapping Buy must not litter the ledger with abandoned rows."""
    await _make_user(database)
    service = _service(database, settings, catalog, _StubProvider())
    first = await service.start_checkout(1, catalog.pro_lifetime())
    second = await service.start_checkout(1, catalog.pro_lifetime())
    assert first.purchase.id == second.purchase.id
    assert len(await service.history(1)) == 1


async def test_history_is_newest_first(database, settings, catalog):
    await _make_user(database)
    service = _service(database, settings, catalog, _StubProvider())
    first = await service.start_checkout(1, catalog.pro_lifetime())
    await service.mark_failed(first.purchase.id, "network error")
    second = await service.start_checkout(1, catalog.pro_lifetime())

    history = await service.history(1)
    assert [p.id for p in history] == [second.purchase.id, first.purchase.id]
    assert (await service.latest(1)).id == second.purchase.id
