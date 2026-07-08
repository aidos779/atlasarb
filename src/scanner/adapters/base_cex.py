"""Base CEX adapter (Scanner §2, §3.3, §4, §5).

Handles the transport concerns common to all 5 CEX venues: aiohttp REST session,
persistent WebSocket with §2.3 reconnect/backoff, token-bucket limiting, symbol
normalization, and health reporting. Concrete venues implement only their venue-specific
REST/WS wire formats — proving the ARCH-1 adapter pattern (one module per venue).

Data sources are official REST (metadata) + official WS (real-time) only (ARCH-2).
NFR-SEC-01: public market-data endpoints only — no user API keys anywhere.
"""
from __future__ import annotations

import asyncio
import contextlib
from abc import abstractmethod
from decimal import Decimal

import aiohttp

from src.config import describe_exc, get_logger
from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook, PriceQuote
from src.domain.ports import ExchangeAdapter, MarketDataSink
from src.scanner.adapters.backoff import backoff_delay
from src.scanner.adapters.rate_limiter import TokenBucket
from src.scanner.adapters.withdrawal_fees import WithdrawalFeeProvider

log = get_logger("adapter.cex")


class BaseCexAdapter(ExchangeAdapter):
    venue_type = VenueType.CEX
    network: str | None = None  # CEX are chain-agnostic

    def __init__(
        self, settings: Settings, config: ScannerConfig, sink: MarketDataSink,
        rest_url: str, ws_url: str, rate_per_sec: float, burst: float,
        on_success=None, on_failure=None, withdrawal_fees: WithdrawalFeeProvider | None = None,
    ) -> None:
        self._settings = settings
        self._withdrawal_fees = withdrawal_fees or WithdrawalFeeProvider()
        self._config = config
        self._sink = sink
        self._rest_url = rest_url.rstrip("/")
        self._ws_url = ws_url
        self._limiter = TokenBucket(rate_per_sec, burst)
        self._session: aiohttp.ClientSession | None = None
        # WS is sharded across several connections: every venue caps subscriptions
        # per connection (MEXC ~30 channels, Bybit/OKX a few hundred, Binance 1024
        # streams). Subscribing thousands of pairs on one socket silently failed, so
        # those venues delivered NO book data and their arbitrage was invisible.
        self._shards: list[set[str]] = []          # pairs owned by each shard
        self._shard_ws: list = []                  # live ws handle per shard (or None)
        self._shard_tasks: list = []               # asyncio.Task per shard (or None)
        self._shard_connected: list[bool] = []     # did this shard connect this cycle
        self._shard_of: dict[str, int] = {}        # pair -> shard index
        self._sub_dropped_logged = False
        self._status = ExchangeStatus.UNKNOWN
        self._subscribed: set[str] = set()   # canonical pairs
        self._symbol_map: dict[str, CanonicalSymbol] = {}  # raw -> canonical
        self._stop = asyncio.Event()
        self._on_success = on_success or (lambda latency=0.0, stream=False: None)
        self._on_failure = on_failure or (lambda hard=False: None)
        self._taker_fee = Decimal("0.001")   # 0.10% MVP default (§8.3)

    # ── venue-specific hooks ──
    @abstractmethod
    async def _fetch_markets(self, session: aiohttp.ClientSession) -> list[CanonicalSymbol]: ...

    @abstractmethod
    def _subscribe_frames(self, symbols: list[CanonicalSymbol]) -> list[dict]: ...

    @abstractmethod
    def _parse_message(self, message: dict) -> list[PriceQuote | OrderBook]: ...

    @abstractmethod
    async def _fetch_funding(self, session: aiohttp.ClientSession,
                             base_asset: str) -> FundingRate | None: ...

    def _raw_symbol(self, symbol: CanonicalSymbol) -> str:
        """Default raw symbol form; venues override for their wire format."""
        return f"{symbol.base_asset}{symbol.quote_asset}"

    # Per-venue WS subscription limits (symbols per connection, max connections).
    ws_max_symbols_per_conn: int = 150
    ws_max_conns: int = 4

    @property
    def _ws(self):  # compat: any currently-live shard socket
        return next((w for w in self._shard_ws if w is not None and not w.closed), None)

    # ── ExchangeAdapter contract ──
    async def connect(self) -> None:
        self._stop.clear()
        timeout = aiohttp.ClientTimeout(total=self._config.cex_rest_timeout_sec)
        self._session = aiohttp.ClientSession(timeout=timeout)
        # Shards are created lazily as pairs are discovered/subscribed.

    async def disconnect(self) -> None:
        self._stop.set()
        for task in self._shard_tasks:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._session:
            await self._session.close()
        self._status = ExchangeStatus.UNKNOWN

    async def get_markets(self) -> list[CanonicalSymbol]:
        session = self._require_session()
        await self._limiter.acquire()
        markets = await self._fetch_markets(session)
        for m in markets:
            self._symbol_map[self._raw_symbol(m)] = m
        return markets

    async def subscribe_ticker(self, symbols: list[CanonicalSymbol]) -> None:
        await self._ensure_subscribed(symbols)

    async def subscribe_order_book(self, symbols: list[CanonicalSymbol], depth: int) -> None:
        await self._ensure_subscribed(symbols)

    def _assign_shard(self, pair: str) -> int | None:
        """Assign a pair to a shard with capacity; open a new shard if needed and
        allowed. Returns None once the per-venue subscription budget is exhausted."""
        existing = self._shard_of.get(pair)
        if existing is not None:
            return existing
        for i, sh in enumerate(self._shards):
            if len(sh) < self.ws_max_symbols_per_conn:
                self._shard_of[pair] = i
                return i
        if len(self._shards) < self.ws_max_conns:
            i = len(self._shards)
            self._shards.append(set())
            self._shard_ws.append(None)
            self._shard_tasks.append(None)
            self._shard_connected.append(False)
            self._shard_of[pair] = i
            return i
        return None

    async def _ensure_subscribed(self, symbols: list[CanonicalSymbol]) -> None:
        """Track pairs, distribute them across bounded WS shards, and subscribe new
        pairs on their shard's live socket immediately (discovery runs after connect,
        so late pairs must be pushed to the open connection — the earlier
        'no data after discovery' bug)."""
        new_by_shard: dict[int, list[CanonicalSymbol]] = {}
        for s in symbols:
            self._symbol_map[self._raw_symbol(s)] = s
            if s.pair in self._subscribed:
                continue
            idx = self._assign_shard(s.pair)
            if idx is None:
                if not self._sub_dropped_logged:
                    log.warning("ws_subscription_cap_reached", venue=self.id,
                                cap=self.ws_max_conns * self.ws_max_symbols_per_conn)
                    self._sub_dropped_logged = True
                continue
            self._subscribed.add(s.pair)
            self._shards[idx].add(s.pair)
            new_by_shard.setdefault(idx, []).append(s)

        for idx, syms in new_by_shard.items():
            # Start the shard's connection if not already running.
            task = self._shard_tasks[idx]
            if task is None or task.done():
                self._shard_tasks[idx] = asyncio.create_task(
                    self._shard_loop(idx), name=f"ws-{self.id}-{idx}")
            # Live-subscribe on an already-open shard socket.
            ws = self._shard_ws[idx]
            if ws is not None and not ws.closed:
                try:
                    for frame in self._subscribe_frames(syms):
                        await ws.send_json(frame)
                    log.debug("ws_live_subscribed", venue=self.id, shard=idx,
                              pairs=len(syms))
                except Exception as exc:  # noqa: BLE001 — reconnect re-subscribes
                    log.debug("ws_live_subscribe_failed", venue=self.id,
                              error=describe_exc(exc))

    async def get_funding_rate(self, base_asset: str) -> FundingRate | None:
        session = self._require_session()
        await self._limiter.acquire()
        try:
            return await self._fetch_funding(session, base_asset)
        except Exception:  # noqa: BLE001
            return None

    async def get_pool_state(self, symbol: CanonicalSymbol) -> OrderBook | None:
        return None  # CEX has no pools

    async def health_check(self) -> bool:
        session = self._require_session()
        try:
            await self._limiter.acquire()
            async with session.get(f"{self._rest_url}{self._ping_path()}") as resp:
                ok = resp.status == 200
                self._status = ExchangeStatus.ONLINE if ok else self._status
                return ok
        except Exception:  # noqa: BLE001
            return False

    def get_status(self) -> ExchangeStatus:
        return self._status

    def taker_fee(self, symbol: CanonicalSymbol) -> Decimal:
        return self._taker_fee

    def withdrawal_fee_usd(self, base_asset: str, network: str | None) -> Decimal | None:
        return self._withdrawal_fees.fee_usd(self.id, base_asset, network)

    def withdrawals_enabled(self, base_asset: str, network: str | None) -> bool:
        return True

    # ── WS loop with §2.3 reconnect ──
    def _ping_path(self) -> str:
        return "/"

    async def _shard_loop(self, idx: int) -> None:
        attempt = 0
        offline = False  # True once this shard escalated to slow-retry
        while not self._stop.is_set():
            self._shard_connected[idx] = False
            try:
                await self._run_shard(idx)
                attempt = 0
                offline = False
            except Exception as exc:  # noqa: BLE001
                # A socket that came up then dropped starts a fresh failure streak,
                # not an escalation — otherwise the fast-reconnect budget never resets
                # (the old MEXC slow-retry/offline flap loop).
                if self._shard_connected[idx]:
                    attempt = 0
                    if offline:
                        log.info("ws_recovered", venue=self.id, shard=idx)
                    offline = False
                attempt += 1
                self._on_failure()
                if attempt >= self._config.ws_fast_reconnect_max:
                    if not offline:
                        # Only mark the venue offline when NO shard is connected —
                        # one shard reconnecting must not blank a venue whose other
                        # shards are streaming.
                        if not any(w is not None and not w.closed for w in self._shard_ws):
                            self._status = ExchangeStatus.API_OFFLINE
                            self._on_failure(hard=True)
                        log.warning("ws_offline_slow_retry", venue=self.id, shard=idx,
                                    error=describe_exc(exc))
                        offline = True
                    await self._sleep(self._config.ws_slow_retry_interval_sec)
                    continue
                delay = backoff_delay(
                    attempt, self._config.ws_backoff_base_sec,
                    self._config.ws_backoff_multiplier, self._config.ws_backoff_cap_sec,
                    self._config.ws_backoff_jitter,
                )
                log.debug("ws_reconnect", venue=self.id, shard=idx, attempt=attempt,
                          delay=round(delay, 2), error=describe_exc(exc))
                await self._sleep(delay)

    async def _run_shard(self, idx: int) -> None:
        session = self._require_session()
        loop = asyncio.get_event_loop()
        async with session.ws_connect(self._ws_url, autoping=True, heartbeat=None) as ws:
            self._shard_ws[idx] = ws
            try:
                symbols = [self._symbol_map.get(self._raw_symbol_from_pair(p))
                           for p in list(self._shards[idx])]
                symbols = [s for s in symbols if s is not None]
                for frame in self._subscribe_frames(symbols):
                    await ws.send_json(frame)
                await self._ws_receive_loop(ws, loop, idx)
            finally:
                self._shard_ws[idx] = None

    def _heartbeat_frame(self) -> dict | str | None:
        """Venue-specific application-level keepalive payload.

        CEX WS servers expect an *application* ping (JSON or a literal string), not
        an aiohttp control-frame ping. Relying on aiohttp's ``heartbeat=`` produced
        unanswered control pings and a ServerTimeoutError (== TimeoutError) every
        ~15s — the reconnect churn seen in the logs. Venues override this; ``None``
        means the server pings us and aiohttp autoping answers (e.g. Binance).
        """
        return None

    async def _send_heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        frame = self._heartbeat_frame()
        if frame is None:
            return
        if isinstance(frame, str):
            await ws.send_str(frame)
        else:
            await ws.send_json(frame)

    async def _ws_receive_loop(self, ws: aiohttp.ClientWebSocketResponse, loop,
                               idx: int = 0) -> None:
        self._status = ExchangeStatus.ONLINE
        self._shard_connected[idx] = True
        ping_interval = self._config.ws_ping_interval_sec
        idle_limit = max(self._config.ws_idle_timeout_sec, ping_interval * 2)
        last_rx = last_ping = loop.time()
        while not self._stop.is_set():
            try:
                msg = await ws.receive(timeout=ping_interval)
            except TimeoutError:
                msg = None  # idle tick — fall through to heartbeat/idle checks
            now = loop.time()
            if msg is not None:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    last_rx = now
                    await self._on_ws_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    last_rx = now
                    await self._on_ws_binary(msg.data)
                elif msg.type in (aiohttp.WSMsgType.PONG, aiohttp.WSMsgType.PING):
                    last_rx = now
                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"{self.id} ws closed ({msg.type.name})")
            # Drive app-level keepalive on cadence.
            if now - last_ping >= ping_interval:
                await self._send_heartbeat(ws)
                last_ping = now
            # Dead-connection guard: no inbound frame at all for too long.
            if now - last_rx > idle_limit:
                raise ConnectionError(f"{self.id} ws idle {int(now - last_rx)}s")

    def _parse_binary(self, data: bytes) -> list[PriceQuote | OrderBook]:
        """Venue-specific binary (e.g. protobuf) frame decoding. Default: keepalive."""
        return []

    async def _on_ws_binary(self, data: bytes) -> None:
        try:
            for item in self._parse_binary(data):
                if isinstance(item, PriceQuote):
                    self._sink.upsert_price(item)
                else:
                    self._sink.upsert_book(item)
            self._on_success(stream=True)
        except Exception as exc:  # noqa: BLE001 — malformed: drop, keep stream (§2.4)
            log.debug("ws_binary_parse_error", venue=self.id, error=describe_exc(exc))

    async def _on_ws_text(self, data: str) -> None:
        import orjson
        try:
            payload = orjson.loads(data)
        except Exception:  # noqa: BLE001 — malformed: drop, keep stream (§2.4)
            return
        try:
            for item in self._parse_message(payload):
                if isinstance(item, PriceQuote):
                    self._sink.upsert_price(item)
                else:
                    self._sink.upsert_book(item)
            self._on_success(stream=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("ws_parse_error", venue=self.id, error=describe_exc(exc))

    def _raw_symbol_from_pair(self, pair: str) -> str:
        base, _, quote = pair.partition("/")
        return f"{base}{quote}"

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(f"{self.id} adapter not connected")
        return self._session

    def _canonical(self, base: str, quote: str) -> CanonicalSymbol:
        return CanonicalSymbol(base.upper(), quote.upper(), VenueType.CEX)
