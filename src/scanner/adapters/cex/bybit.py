"""Bybit v5 adapter — official REST instruments + WS tickers/orderbook (ARCH-2)."""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from src.config import get_logger
from src.domain.market import BookLevel, CanonicalSymbol, FundingRate, OrderBook, PriceQuote
from src.scanner.adapters.base_cex import BaseCexAdapter
from src.scanner.adapters.tls import ssl_context

log = get_logger("adapter.cex")


def _redact_proxy(url: str) -> str:
    """Hide any password in a proxy URL before logging (scheme://user:***@host:port)."""
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = f"{parts.username}:***@{parts.hostname}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:  # noqa: BLE001
        return "***"
    return url


class BybitAdapter(BaseCexAdapter):
    id = "bybit"
    display_name = "Bybit"
    ws_max_symbols_per_conn = 80
    ws_max_conns = 3

    def __init__(self, settings, config, sink, **kw) -> None:
        super().__init__(settings, config, sink, settings.bybit_rest_url,
                         settings.bybit_ws_url, rate_per_sec=20, burst=40, **kw)
        self._last_book: dict[str, tuple[list, list]] = {}
        # Per-adapter proxy (Bybit only). Some server IPs are CloudFront-403'd by Bybit on
        # EVERY host (api.bybit.com / api.bytick.com / api.bybit.kz), so no endpoint swap
        # helps — the request must egress through a proxy. Empty = direct.
        self._proxy = (settings.bybit_proxy or "").strip()
        self._proxy_log = _redact_proxy(self._proxy) if self._proxy else None

    async def connect(self) -> None:
        # Direct connection uses the shared base transport unchanged. Only when BYBIT_PROXY
        # is set do we build a proxy-aware session — scoped to THIS adapter, so every other
        # exchange (and the DEX layer) keeps its direct session.
        if not self._proxy:
            await super().connect()
            return
        from aiohttp_socks import ProxyConnector  # lazy: only needed when a proxy is set
        self._stop.clear()
        timeout = aiohttp.ClientTimeout(total=self._config.cex_rest_timeout_sec)
        # ProxyConnector.from_url handles http://, https://, socks4:// and socks5://; both
        # REST (session.get) and WS (session.ws_connect) then egress through the proxy
        # because they share this connector. certifi TLS is preserved end-to-end.
        connector = ProxyConnector.from_url(self._proxy, ssl=ssl_context())
        self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        log.info("bybit_proxy_enabled", venue=self.id, proxy=self._proxy_log)

    def _ping_path(self) -> str:
        return "/v5/market/time"

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        return f"{symbol.base_asset}{symbol.quote_asset}"

    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]:
        data = await self._get_json_logged(
            session, f"{self._rest_url}/v5/market/instruments-info",
            params={"category": "spot"})
        out = []
        for s in data.get("result", {}).get("list", []):
            if s.get("status") != "Trading":
                continue
            quote = s.get("quoteCoin")
            if quote not in ("USDT", "USDC"):
                continue
            out.append(self._canonical(s["baseCoin"], quote))
        return out

    def _heartbeat_frame(self) -> dict | str | None:
        return {"op": "ping"}

    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]:
        args = []
        for s in symbols:
            raw = self._raw_symbol(s)
            args.append(f"tickers.{raw}")
            args.append(f"orderbook.50.{raw}")
        frames = []
        for i in range(0, len(args), 10):
            frames.append({"op": "subscribe", "args": args[i:i + 10]})
        return frames

    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]:
        topic = message.get("topic", "")
        data = message.get("data")
        if not topic or data is None:
            return []
        if topic.startswith("tickers."):
            raw = topic.split(".", 1)[1]
            sym = self._symbol_map.get(raw)
            if not sym or "bid1Price" not in data:
                return []
            return [PriceQuote(
                venue=self.id, symbol=sym, bid=Decimal(str(data["bid1Price"])),
                ask=Decimal(str(data["ask1Price"])),
                last=Decimal(str(data.get("lastPrice", data["ask1Price"]))), source="WS",
            )]
        if topic.startswith("orderbook."):
            raw = topic.rsplit(".", 1)[1]
            sym = self._symbol_map.get(raw)
            if not sym:
                return []
            bids = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data.get("b", [])]
            asks = [BookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data.get("a", [])]
            # Bybit sends snapshot/delta; keep last non-empty side on delta.
            prev = self._last_book.get(raw, ([], []))
            if not bids:
                bids = prev[0]
            if not asks:
                asks = prev[1]
            self._last_book[raw] = (bids, asks)
            if not bids or not asks:
                return []
            return [OrderBook(venue=self.id, symbol=sym, bids=bids, asks=asks,
                              sequence=data.get("seq"))]
        return []

    async def _fetch_funding(self, session, base_asset: str) -> FundingRate | None:
        async with session.get(f"{self._rest_url}/v5/market/tickers",
                               params={"category": "linear", "symbol": f"{base_asset}USDT"}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        rows = data.get("result", {}).get("list", [])
        if not rows:
            return None
        d = rows[0]
        return FundingRate(
            venue=self.id, base_asset=base_asset,
            current_rate=Decimal(str(d.get("fundingRate", "0"))), predicted_rate=None,
            next_funding_time=float(d.get("nextFundingTime", 0)) / 1000.0, interval_hours=8,
        )
