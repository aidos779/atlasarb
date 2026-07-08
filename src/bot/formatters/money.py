"""Currency + timezone formatting (PRD §14, BR-DETAILS-3, NFR-USE-03).

All monetary values respect the user's Currency setting; all timestamps respect the
user's Timezone. Calculations run in USD internally; conversion is display-only.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SYMBOLS = {"USD": "$", "EUR": "€", "KZT": "₸", "RUB": "₽", "USDT": "$"}


async def format_money(fx, amount_usd: Decimal, currency: str) -> str:
    converted = await fx.convert(amount_usd, currency)
    symbol = _SYMBOLS.get(currency.upper(), "")
    q = converted.quantize(Decimal("0.01"))
    if currency.upper() in ("KZT", "RUB"):
        return f"{q:,.0f} {symbol}".strip()
    return f"{symbol}{q:,.2f}"


def format_time(ts: float, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M:%S")


def format_datetime(ts: float, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M %Z")


def format_pct(value: Decimal, digits: int = 2) -> str:
    return f"{value:+.{digits}f}%"
