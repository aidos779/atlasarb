"""Binance adapter — official REST metadata + WS bookTicker/depth (ARCH-2)."""
from __future__ import annotations

from decimal import Decimal

import aiohttp

from src.domain.market import BookLevel, CanonicalSymbol, FundingRate, OrderBook, PriceQuote
from src.scanner.adapters.base_cex import BaseCexAdapter

_FUTURES_URL = "https://fapi.binance.com"


class BinanceAdapter(BaseCexAdapter):
    id = "binance"
    display_name = "Binance"
    ws_max_symbols_per_conn = 200
    ws_max_conns = 3

    def __init__(self, settings, config, sink, **kw) -> None:
        # All public Binance market data (exchangeInfo, bookTicker, depth, ping/health,
        # discovery, WS + every reconnect) goes through data-api/data-stream.binance.vision
        # via settings.binance_rest_url / binance_ws_url — api.binance.com and
        # stream.binance.com return HTTP 451 from restricted locations. No fallback to the
        # blocked hosts. When even the Vision hosts are blocked for the server's IP,
        # BINANCE_PROXY / BINANCE_PROXY_FALLBACK route Binance REST + WS (and the funding
        # fetch below) through a dedicated proxy chain with automatic failover.
        proxies = [p for p in (settings.binance_proxy,
                               settings.binance_proxy_fallback) if p.strip()]
        super().__init__(settings, config, sink, settings.binance_rest_url,
                         settings.binance_ws_url, rate_per_sec=100, burst=200,
                         proxies=proxies, **kw)

    def _ping_path(self) -> str:
        return "/api/v3/ping"

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        return f"{symbol.base_asset}{symbol.quote_asset}"

    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]:
        data = await self._get_json_logged(session, f"{self._rest_url}/api/v3/exchangeInfo")
        out: list[CanonicalSymbol] = []
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING" or not s.get("isSpotTradingAllowed"):
                continue
            quote = s.get("quoteAsset")
            if quote not in ("USDT", "USDC"):
                continue
            out.append(self._canonical(s["baseAsset"], quote))
        return out

    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]:
        params: list[str] = []
        for s in symbols:
            raw = self._raw_symbol(s).lower()
            params.append(f"{raw}@bookTicker")
            params.append(f"{raw}@depth20@100ms")
        if not params:
            return []
        # Binance caps params/frame; chunk to be safe.
        frames = []
        for i in range(0, len(params), 200):
            frames.append({"method": "SUBSCRIBE", "params": params[i:i + 200], "id": i + 1})
        return frames

    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]:
        stream = message.get("stream", "")
        data = message.get("data")
        if not stream or data is None:
            return []
        if "@bookTicker" in stream:
            sym = self._symbol_map.get(data["s"])
            if not sym:
                return []
            return [PriceQuote(
                venue=self.id, symbol=sym, bid=Decimal(str(data["b"])),
                ask=Decimal(str(data["a"])), last=Decimal(str(data["a"])), source="WS",
            )]
        if "@depth" in stream:
            raw = stream.split("@")[0].upper()
            sym = self._symbol_map.get(raw)
            if not sym:
                return []
            bids = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data.get("bids", [])]
            asks = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data.get("asks", [])]
            if not bids or not asks:
                return []
            return [OrderBook(venue=self.id, symbol=sym, bids=bids, asks=asks,
                              sequence=data.get("lastUpdateId"))]
        return []

    async def _fetch_funding(self, session: aiohttp.ClientSession,
                             base_asset: str) -> FundingRate | None:
        symbol = f"{base_asset}USDT"
        async with session.get(f"{_FUTURES_URL}/fapi/v1/premiumIndex",
                               params={"symbol": symbol}) as resp:
            if resp.status != 200:
                return None
            d = await resp.json()
        return FundingRate(
            venue=self.id, base_asset=base_asset,
            current_rate=Decimal(str(d.get("lastFundingRate", "0"))),
            predicted_rate=None,
            next_funding_time=float(d.get("nextFundingTime", 0)) / 1000.0,
            interval_hours=8,
        )
