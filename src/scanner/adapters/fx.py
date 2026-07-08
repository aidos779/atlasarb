"""FX rate provider (PRD FR-LOC-02) — USD → display currency, ≤5min stale.

Calculations always run in USD internally (BR-SET-2); conversion is display-only. Rates
refresh from a public FX endpoint with a 5-minute cache; conservative static fallbacks
keep the bot functional offline. USDT is treated ~1:1 with USD for display.
"""
from __future__ import annotations

import time
from decimal import Decimal

import aiohttp

from src.config import describe_exc, get_logger

log = get_logger("adapter.fx")

_STATIC_FALLBACK = {
    "USD": Decimal(1), "USDT": Decimal(1), "EUR": Decimal("0.92"),
    "KZT": Decimal("470"), "RUB": Decimal("92"),
}
_MAX_AGE_SEC = 300


class FxRateProvider:
    def __init__(self, endpoint: str = "https://open.er-api.com/v6/latest/USD") -> None:
        self._endpoint = endpoint
        self._rates = dict(_STATIC_FALLBACK)
        self._fetched_at = 0.0

    async def _refresh(self) -> None:
        if time.time() - self._fetched_at < _MAX_AGE_SEC:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._endpoint) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
            rates = data.get("rates", {})
            for cur in ("EUR", "KZT", "RUB"):
                if cur in rates:
                    self._rates[cur] = Decimal(str(rates[cur]))
            self._rates["USD"] = Decimal(1)
            self._rates["USDT"] = Decimal(1)
            self._fetched_at = time.time()
        except Exception as exc:  # noqa: BLE001 — keep last-known/static rates
            log.debug("fx_refresh_failed", error=describe_exc(exc))

    async def convert(self, amount_usd: Decimal, currency: str) -> Decimal:
        await self._refresh()
        rate = self._rates.get(currency.upper(), Decimal(1))
        return amount_usd * rate
