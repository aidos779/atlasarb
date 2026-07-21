"""Telegram Crypto Pay (Crypto Bot) adapter — the live crypto checkout provider.

Two pieces:

* :class:`CryptoPayClient` — a thin async wrapper over the Crypto Pay HTTP API
  (``createInvoice`` / ``getInvoices``) plus webhook signature verification. The
  transport is injectable so the client is testable without a network.
* :class:`CryptoPayProvider` — the :class:`~src.services.payments.provider.PaymentProvider`
  the composition root swaps in for the placeholder. It creates a **fiat-priced** invoice
  (``$20 USD``) that a payer settles in any accepted crypto asset (USDT/TON/BTC…); Crypto
  Bot performs the conversion at pay time, so the price stays denominated in USD and the
  asset list is pure configuration.

Signature verification follows Crypto Pay's scheme exactly: the HMAC-SHA256 key is the
SHA-256 digest of the API token, and the signature covers the raw request body. A forged
or tampered webhook fails this check and is never trusted — the property the whole
settlement path leans on.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

from src.config import get_logger
from src.services.payments.provider import Invoice, PaymentReceipt

log = get_logger("services.payments.cryptopay")

#: (url, headers, json_body) -> parsed JSON response. Injectable for tests.
Transport = Callable[[str, dict, dict], Awaitable[dict]]


class CryptoPayError(RuntimeError):
    """A Crypto Pay API call returned ``ok: false`` or was unreachable."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def format_amount(amount: float | Decimal) -> str:
    """Render a money amount for the API: 20.0 -> "20", 49.5 -> "49.5"."""
    text = f"{Decimal(str(amount)):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


class CryptoPayClient:
    def __init__(self, token: str, api_url: str = "https://pay.crypt.bot/api", *,
                 transport: Transport | None = None, timeout: float = 20.0) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        # Webhook HMAC key = SHA-256 of the token (Crypto Pay spec).
        self._secret = hashlib.sha256(token.encode()).digest()

    async def _default_transport(self, url: str, headers: dict, body: dict) -> dict:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                return await resp.json()

    async def _call(self, method: str, params: dict) -> dict:
        url = f"{self._api_url}/{method}"
        headers = {"Crypto-Pay-API-Token": self._token,
                   "Content-Type": "application/json"}
        transport = self._transport or self._default_transport
        data = await transport(url, headers, params)
        if not data.get("ok"):
            err = data.get("error") or {}
            raise CryptoPayError(f"crypto pay {method} failed: {err}",
                                 code=err.get("code"))
        return data.get("result") or {}

    async def create_invoice(self, *, amount: float | Decimal, accepted_assets: list[str],
                             payload: str, fiat: str = "USD",
                             description: str | None = None,
                             expires_in: int | None = None) -> dict:
        params: dict = {
            "currency_type": "fiat",
            "fiat": fiat,
            "amount": format_amount(amount),
            "payload": payload,
        }
        if accepted_assets:
            params["accepted_assets"] = ",".join(accepted_assets)
        if description:
            params["description"] = description[:1024]
        if expires_in:
            params["expires_in"] = int(expires_in)
        return await self._call("createInvoice", params)

    async def get_invoices(self, invoice_ids: list[str | int]) -> list[dict]:
        if not invoice_ids:
            return []
        result = await self._call(
            "getInvoices", {"invoice_ids": ",".join(str(i) for i in invoice_ids)})
        return list(result.get("items", []))

    def verify_signature(self, raw_body: bytes, signature: str) -> bool:
        """Constant-time check that ``signature`` is Crypto Pay's HMAC over ``raw_body``."""
        if not signature:
            return False
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


def receipt_from_invoice(update: dict, invoice: dict) -> PaymentReceipt:
    """Build a receipt from a paid-invoice object. ``payment_id`` is our purchase id
    (the invoice ``payload`` we set at creation)."""
    return PaymentReceipt(
        payment_id=str(invoice.get("payload") or ""),
        user_id=0,  # resolved from the purchase row; not carried in the invoice
        amount_usd=float(invoice.get("amount") or 0.0),
        asset=invoice.get("paid_asset") or invoice.get("asset"),
        invoice_id=str(invoice.get("invoice_id") or ""),
        raw=update,
    )


class CryptoPayProvider:
    """Crypto Pay implementation of the PaymentProvider port."""

    id = "cryptobot"

    def __init__(self, client: CryptoPayClient, *, accepted_assets: list[str],
                 expires_in: int = 3600,
                 description: str = "AtlasArb Pro Lifetime") -> None:
        self._client = client
        self._assets = accepted_assets
        self._expires_in = expires_in
        self._description = description

    async def create_invoice(self, user_id: int, amount_usd: float,
                             payload: str) -> Invoice:
        result = await self._client.create_invoice(
            amount=amount_usd, accepted_assets=self._assets, payload=payload,
            description=self._description, expires_in=self._expires_in)
        invoice_id = str(result.get("invoice_id"))
        pay_url = (result.get("bot_invoice_url") or result.get("pay_url")
                   or result.get("mini_app_invoice_url")
                   or result.get("web_app_invoice_url"))
        if not invoice_id or not pay_url:
            raise CryptoPayError(f"createInvoice returned no usable invoice: {result}")
        log.info("crypto_pay_invoice_created", invoice_id=invoice_id, user_id=user_id,
                 amount_usd=amount_usd)
        return Invoice(payment_id=invoice_id, pay_url=pay_url, amount_usd=amount_usd)

    async def verify(self, raw_body: bytes, signature: str) -> PaymentReceipt | None:
        if not self._client.verify_signature(raw_body, signature):
            return None
        try:
            update = json.loads(raw_body)
        except (ValueError, TypeError):
            return None
        if update.get("update_type") != "invoice_paid":
            return None
        invoice = update.get("payload") or {}
        if invoice.get("status") != "paid":
            return None
        return receipt_from_invoice(update, invoice)
