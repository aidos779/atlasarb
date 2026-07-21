"""Crypto Pay settlement — the webhook handler and the polling reconciler.

Covers the whole trust boundary end to end against a real database: a verified paid
callback activates Lifetime exactly once; a duplicate is ignored; a forged signature and
a tampered amount are refused; a replayed settlement never double-grants; and the
reconciler settles or expires invoices when no webhook arrives.
"""
import hashlib
import hmac
import json

import pytest

from src.database.repositories.payment_event_repo import PaymentEventRepository
from src.domain.purchase import PurchaseStatus
from src.services.admin_service import AdminService
from src.services.payments.cryptopay import CryptoPayClient
from src.services.payments.provider import Invoice, PaymentReceipt
from src.services.payments.reconciler import PaymentReconciler
from src.services.payments.webhook import CryptoPayWebhookHandler
from src.services.purchase_service import PurchaseService
from src.services.subscription_service import SubscriptionService
from tests.integration.test_subscription import _make_user, _reload

pytestmark = pytest.mark.asyncio

TOKEN = "12345:AAdummytoken"
PRICE = 20.0


def _sign(body: bytes, token: str = TOKEN) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


class _StubProvider:
    """Issues invoices with a fixed id so a PENDING purchase can be set up."""

    id = "cryptobot"

    def __init__(self, invoice_id: str = "INV-1") -> None:
        self.invoice_id = invoice_id

    async def create_invoice(self, user_id, amount_usd, payload) -> Invoice:
        return Invoice(payment_id=self.invoice_id,
                       pay_url=f"https://pay.test/{self.invoice_id}", amount_usd=amount_usd)

    async def verify(self, raw_body, signature) -> PaymentReceipt | None:
        return None


class _FakeClient:
    """Real signature logic, canned getInvoices — for the reconciler."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self._real = CryptoPayClient(TOKEN)

    def verify_signature(self, raw, sig) -> bool:
        return self._real.verify_signature(raw, sig)

    async def get_invoices(self, invoice_ids) -> list[dict]:
        wanted = {str(i) for i in invoice_ids}
        return [i for i in self._items if str(i["invoice_id"]) in wanted]


async def _pending_purchase(database, settings, catalog, invoice_id="INV-1", uid=1):
    await _make_user(database, uid)
    subscriptions = SubscriptionService(database, settings, catalog)
    purchases = PurchaseService(database, subscriptions, _StubProvider(invoice_id))
    checkout = await purchases.start_checkout(uid, catalog.pro_lifetime())
    fresh = await purchases.get(checkout.purchase.id)
    assert fresh.status == PurchaseStatus.PENDING
    assert fresh.external_payment_id == invoice_id      # PENDING with invoice id
    return purchases


def _paid_update(purchase_id: str, *, invoice_id="INV-1", update_id=100,
                 amount="20", asset="TON", status="paid", fiat="USD") -> dict:
    return {"update_id": update_id, "update_type": "invoice_paid",
            "payload": {"invoice_id": invoice_id, "status": status, "payload": purchase_id,
                        "amount": amount, "fiat": fiat, "paid_asset": asset}}


def _handler(database, purchases, calls: list) -> CryptoPayWebhookHandler:
    async def on_paid(purchase):
        calls.append(purchase.id)

    return CryptoPayWebhookHandler(
        database, CryptoPayClient(TOKEN), purchases, price_usd=PRICE,
        accepted_assets=["USDT", "TON", "BTC"], on_paid=on_paid)


# ── webhook: happy path ──

async def test_paid_webhook_activates_lifetime(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    calls: list[str] = []
    handler = _handler(database, purchases, calls)

    body = json.dumps(_paid_update(purchase.id)).encode()
    result = await handler.handle(body, _sign(body))

    assert result.status == "activated"
    settled = await purchases.get(purchase.id)
    assert settled.status == PurchaseStatus.PAID
    assert settled.asset == "TON"
    assert settled.provider_payload  # invoice snapshot stored
    assert (await _reload(database)).subscription.is_pro is True
    assert calls == [purchase.id]              # confirmation sent exactly once


async def test_paid_webhook_records_event_history(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    handler = _handler(database, purchases, [])
    body = json.dumps(_paid_update(purchase.id)).encode()
    await handler.handle(body, _sign(body))

    async with database.session() as s:
        events = await PaymentEventRepository(s).for_invoice("INV-1")
    assert len(events) == 1
    assert events[0].signature_valid is True
    assert events[0].processed is True


# ── webhook: duplicate protection ──

async def test_duplicate_webhook_is_ignored(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    calls: list[str] = []
    handler = _handler(database, purchases, calls)
    body = json.dumps(_paid_update(purchase.id)).encode()
    sig = _sign(body)

    first = await handler.handle(body, sig)
    second = await handler.handle(body, sig)          # same update_id
    third = await handler.handle(body, sig)

    assert first.status == "activated"
    assert second.status == "duplicate"
    assert third.status == "duplicate"
    assert calls == [purchase.id]                     # confirmed once, not thrice
    assert await AdminService(database, None).revenue_usd() == PRICE  # billed once


async def test_replayed_settlement_with_new_update_id_never_double_grants(
        database, settings, catalog):
    """A replay attack that changes only update_id (re-signed) still grants once."""
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    handler = _handler(database, purchases, [])

    first = json.dumps(_paid_update(purchase.id, update_id=1)).encode()
    replay = json.dumps(_paid_update(purchase.id, update_id=2)).encode()
    r1 = await handler.handle(first, _sign(first))
    r2 = await handler.handle(replay, _sign(replay))

    assert r1.status == "activated"
    assert r2.status == "already_paid"                # purchase already PAID
    assert await AdminService(database, None).revenue_usd() == PRICE


# ── webhook: security refusals ──

async def test_forged_signature_is_rejected(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    handler = _handler(database, purchases, [])
    body = json.dumps(_paid_update(purchase.id)).encode()

    result = await handler.handle(body, "deadbeef")   # not a valid HMAC

    assert result.status == "invalid_signature"
    assert (await _reload(database)).subscription.is_pro is False
    # The rejected callback is still recorded (never lose webhook history).
    async with database.session() as s:
        events = await PaymentEventRepository(s).for_invoice("INV-1")
    assert len(events) == 1 and events[0].signature_valid is False


async def test_amount_mismatch_is_refused(database, settings, catalog):
    """Even a correctly-signed body whose amount is not the price does not settle."""
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    handler = _handler(database, purchases, [])
    body = json.dumps(_paid_update(purchase.id, amount="0.01")).encode()

    result = await handler.handle(body, _sign(body))

    assert result.status == "amount_mismatch"
    assert (await _reload(database)).subscription.is_pro is False


async def test_unknown_purchase_is_ignored(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    handler = _handler(database, purchases, [])
    body = json.dumps(_paid_update("no-such-purchase")).encode()
    result = await handler.handle(body, _sign(body))
    assert result.status == "unknown_purchase"


async def test_malformed_body_is_recorded_and_400(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    handler = _handler(database, purchases, [])
    result = await handler.handle(b"not json", _sign(b"not json"))
    assert result.ok is False and result.status == "malformed"


# ── reconciler (polling) ──

async def test_reconciler_settles_a_paid_invoice(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    calls: list[str] = []

    async def on_paid(p):
        calls.append(p.id)

    client = _FakeClient([{"invoice_id": "INV-1", "status": "paid", "paid_asset": "USDT"}])
    reconciler = PaymentReconciler(database, client, purchases, on_paid=on_paid)

    settled = await reconciler.run_once()

    assert settled == 1
    assert (await purchases.get(purchase.id)).status == PurchaseStatus.PAID
    assert (await _reload(database)).subscription.is_pro is True
    assert calls == [purchase.id]
    # Nothing left to poll — a second pass is a no-op (no double grant).
    assert await reconciler.run_once() == 0


async def test_reconciler_expires_an_expired_invoice(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    client = _FakeClient([{"invoice_id": "INV-1", "status": "expired"}])
    reconciler = PaymentReconciler(database, client, purchases)

    settled = await reconciler.run_once()

    assert settled == 0
    assert (await purchases.get(purchase.id)).status == PurchaseStatus.EXPIRED
    assert (await _reload(database)).subscription.is_pro is False


async def test_reconciler_leaves_active_invoice_pending(database, settings, catalog):
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    client = _FakeClient([{"invoice_id": "INV-1", "status": "active"}])
    reconciler = PaymentReconciler(database, client, purchases)

    assert await reconciler.run_once() == 0
    assert (await purchases.get(purchase.id)).status == PurchaseStatus.PENDING


async def test_webhook_and_reconciler_do_not_double_settle(database, settings, catalog):
    """Run both against the same paid invoice — exactly one grant, one confirmation."""
    purchases = await _pending_purchase(database, settings, catalog)
    purchase = await purchases.latest(1)
    calls: list[str] = []
    handler = _handler(database, purchases, calls)

    body = json.dumps(_paid_update(purchase.id)).encode()
    await handler.handle(body, _sign(body))           # webhook settles it

    client = _FakeClient([{"invoice_id": "INV-1", "status": "paid", "paid_asset": "TON"}])

    async def on_paid(p):
        calls.append(p.id)

    reconciler = PaymentReconciler(database, client, purchases, on_paid=on_paid)
    assert await reconciler.run_once() == 0           # already PAID, nothing to do
    assert calls == [purchase.id]                     # confirmed exactly once
    assert await AdminService(database, None).revenue_usd() == PRICE
