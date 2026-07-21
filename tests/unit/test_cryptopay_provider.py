"""Telegram Crypto Pay client + provider — invoice creation and signature verification.

These are the trust boundary: a forged webhook must fail verification, and a real one
must produce a receipt keyed on our purchase id. The transport is stubbed so no network
is touched.
"""
import hashlib
import hmac
import json

import pytest

from src.services.payments.cryptopay import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayProvider,
    format_amount,
)

pytestmark = pytest.mark.asyncio

TOKEN = "12345:AAdummytoken"


def _sign(token: str, body: bytes) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


class _Transport:
    """Records calls and returns canned Crypto Pay responses."""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, url: str, headers: dict, body: dict) -> dict:
        self.calls.append((url, headers, body))
        return self.response


# ── format_amount ──

def test_format_amount_strips_meaningless_cents():
    assert format_amount(20.0) == "20"
    assert format_amount(49.5) == "49.5"
    assert format_amount(20) == "20"


# ── createInvoice ──

async def test_create_invoice_builds_a_fiat_usd_invoice():
    transport = _Transport({"ok": True, "result": {
        "invoice_id": 777, "status": "active",
        "bot_invoice_url": "https://t.me/CryptoBot?start=IV777"}})
    client = CryptoPayClient(TOKEN, transport=transport)
    provider = CryptoPayProvider(client, accepted_assets=["USDT", "TON", "BTC"],
                                 expires_in=3600)

    invoice = await provider.create_invoice(1, 20.0, payload="purchase-abc")

    assert invoice.payment_id == "777"
    assert invoice.pay_url == "https://t.me/CryptoBot?start=IV777"
    # The provider priced it in fiat USD and offered all three assets.
    _, headers, body = transport.calls[0]
    assert headers["Crypto-Pay-API-Token"] == TOKEN
    assert body["currency_type"] == "fiat"
    assert body["fiat"] == "USD"
    assert body["amount"] == "20"
    assert body["accepted_assets"] == "USDT,TON,BTC"
    assert body["payload"] == "purchase-abc"
    assert body["expires_in"] == 3600


async def test_create_invoice_prefers_bot_url_then_falls_back():
    transport = _Transport({"ok": True, "result": {
        "invoice_id": 9, "pay_url": "https://pay.legacy/9"}})
    provider = CryptoPayProvider(CryptoPayClient(TOKEN, transport=transport),
                                 accepted_assets=["USDT"])
    invoice = await provider.create_invoice(1, 20.0, payload="p")
    assert invoice.pay_url == "https://pay.legacy/9"


async def test_api_error_raises():
    transport = _Transport({"ok": False, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}})
    client = CryptoPayClient(TOKEN, transport=transport)
    with pytest.raises(CryptoPayError):
        await client.create_invoice(amount=20, accepted_assets=["USDT"], payload="p")


async def test_invoice_without_url_is_an_error():
    transport = _Transport({"ok": True, "result": {"invoice_id": 5}})
    provider = CryptoPayProvider(CryptoPayClient(TOKEN, transport=transport),
                                 accepted_assets=["USDT"])
    with pytest.raises(CryptoPayError):
        await provider.create_invoice(1, 20.0, payload="p")


# ── signature verification ──

def test_valid_signature_verifies():
    client = CryptoPayClient(TOKEN)
    body = b'{"update_id": 1}'
    assert client.verify_signature(body, _sign(TOKEN, body)) is True


def test_tampered_body_fails_verification():
    client = CryptoPayClient(TOKEN)
    body = b'{"update_id": 1, "amount": "20"}'
    sig = _sign(TOKEN, body)
    tampered = b'{"update_id": 1, "amount": "0.01"}'
    assert client.verify_signature(tampered, sig) is False


def test_wrong_token_fails_verification():
    body = b'{"update_id": 1}'
    sig = _sign("other:token", body)
    assert CryptoPayClient(TOKEN).verify_signature(body, sig) is False


def test_empty_signature_fails():
    assert CryptoPayClient(TOKEN).verify_signature(b"{}", "") is False


# ── provider.verify (webhook parse) ──

async def test_verify_returns_receipt_for_paid_invoice():
    provider = CryptoPayProvider(CryptoPayClient(TOKEN), accepted_assets=["USDT"])
    update = {"update_id": 42, "update_type": "invoice_paid",
              "payload": {"invoice_id": 777, "status": "paid", "payload": "purchase-abc",
                          "amount": "20", "fiat": "USD", "paid_asset": "TON"}}
    body = json.dumps(update).encode()
    receipt = await provider.verify(body, _sign(TOKEN, body))
    assert receipt is not None
    assert receipt.payment_id == "purchase-abc"
    assert receipt.invoice_id == "777"
    assert receipt.asset == "TON"
    assert receipt.amount_usd == 20.0


async def test_verify_rejects_bad_signature():
    provider = CryptoPayProvider(CryptoPayClient(TOKEN), accepted_assets=["USDT"])
    update = {"update_id": 42, "update_type": "invoice_paid",
              "payload": {"invoice_id": 1, "status": "paid", "payload": "p"}}
    body = json.dumps(update).encode()
    assert await provider.verify(body, "deadbeef") is None


async def test_verify_ignores_non_paid_updates():
    provider = CryptoPayProvider(CryptoPayClient(TOKEN), accepted_assets=["USDT"])
    update = {"update_id": 1, "update_type": "invoice_paid",
              "payload": {"invoice_id": 1, "status": "active", "payload": "p"}}
    body = json.dumps(update).encode()
    assert await provider.verify(body, _sign(TOKEN, body)) is None
