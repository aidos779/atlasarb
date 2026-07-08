"""Regression: pairs discovered AFTER the WS connects must be subscribed on the
live socket, so market data flows and candidates can be produced.

This reproduces the "no candidate_created after discovery" root cause: the socket
connected with an empty subscription set (discovery had not run yet) and newly
discovered pairs were never sent to the open connection.
"""
import asyncio
from decimal import Decimal

import aiohttp
import orjson
import pytest
from aiohttp import web

from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.domain.enums import VenueType
from src.domain.market import CanonicalSymbol, PriceQuote
from src.scanner.adapters.base_cex import BaseCexAdapter

pytestmark = pytest.mark.asyncio


class _Sink:
    def __init__(self):
        self.prices: list[PriceQuote] = []

    def upsert_price(self, q):
        self.prices.append(q)

    def upsert_book(self, b):
        pass


class _Adapter(BaseCexAdapter):
    id = "fake"
    display_name = "fake"

    async def _fetch_markets(self, session):
        return []

    def _subscribe_frames(self, symbols):
        # One subscribe frame carrying the raw symbols.
        return [{"op": "subscribe", "pairs": [self._raw_symbol(s) for s in symbols]}]

    def _parse_message(self, message):
        if message.get("type") != "ticker":
            return []
        sym = self._symbol_map.get(message["s"])
        if not sym:
            return []
        return [PriceQuote(venue=self.id, symbol=sym, bid=Decimal(message["b"]),
                           ask=Decimal(message["a"]), last=Decimal(message["a"]), source="WS")]

    async def _fetch_funding(self, session, base_asset):
        return None


async def _server():
    app = web.Application()

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = orjson.loads(msg.data)
            if frame.get("op") == "subscribe":
                # Stream a ticker for each subscribed pair (this is what proves the
                # live-subscribe reached the server).
                for raw in frame.get("pairs", []):
                    await ws.send_str(orjson.dumps(
                        {"type": "ticker", "s": raw, "b": "3000", "a": "3001"}).decode())
        return ws

    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/ws"


async def test_pair_subscribed_after_connect_streams_data():
    runner, url = await _server()
    cfg = ScannerConfig()
    cfg.ws_ping_interval_sec = 0.2
    sink = _Sink()
    a = _Adapter(Settings(), cfg, sink=sink, rest_url="http://x", ws_url=url,
                 rate_per_sec=100, burst=100)
    sym = CanonicalSymbol("ETH", "USDT", VenueType.CEX)
    try:
        await a.connect()                       # session up; shards created lazily
        await asyncio.sleep(0.2)
        await a.subscribe_ticker([sym])         # discovery happens AFTER connect
        # Data should now arrive because the pair spun up a shard + subscribed.
        for _ in range(40):
            if sink.prices:
                break
            await asyncio.sleep(0.1)
        assert a._ws is not None, "shard socket should be live after subscribe"
        assert sink.prices, "no market data after post-connect subscribe (bug present)"
        assert sink.prices[0].symbol.pair == "ETH/USDT"
        assert sink.prices[0].ask == Decimal("3001")
    finally:
        await a.disconnect()
        await runner.cleanup()
