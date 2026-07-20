"""Base DEX adapter (Scanner §2.4, §5.4).

Shared JSON-RPC transport with ≥2-provider failover per network (§2.4), health via
block-number probe, and pool-state reads. Concrete DEX modules supply their pool set
and reserve-decoding. Data sourced via official RPC/SDK/API only (ARCH-2); pool sets are
verified-token-list gated (§3.4) — the curated pool list is engine *data* input, not code.
"""
from __future__ import annotations

import asyncio
import time
from abc import abstractmethod
from decimal import Decimal

import aiohttp

from src.config import LogThrottle, describe_exc, get_logger
from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook
from src.domain.ports import ExchangeAdapter, MarketDataSink
from src.scanner.adapters.rate_limiter import TokenBucket
from src.scanner.adapters.rpc_pool import RpcErrorKind, RpcProviderPool
from src.scanner.adapters.tls import ssl_context

log = get_logger("adapter.dex")

# All-providers-down is worth one aggregated WARNING per network per window, not one
# per call — a fully rate-limited host would otherwise emit it every poll cycle.
_exhausted_log_throttle = LogThrottle(interval_sec=60.0)


# Substrings that identify a permanent authentication failure in a response body /
# JSON-RPC error message (vs a transient block or throttle).
_AUTH_MARKERS = ("unauthorized", "api key", "apikey", "must be authenticated",
                 "authentication required", "invalid key", "forbidden: token")


def _auth_body(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _AUTH_MARKERS)


async def _safe_text(resp) -> str | None:
    try:
        return (await resp.text())[:300]
    except Exception:  # noqa: BLE001
        return None


class BaseDexAdapter(ExchangeAdapter):
    venue_type = VenueType.DEX

    def __init__(
        self, settings: Settings, config: ScannerConfig, sink: MarketDataSink,
        network: str, rate_per_sec: float = 10, burst: float = 20,
        on_success=None, on_failure=None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._sink = sink
        self.network = network
        self._rpc_urls = settings.rpc_urls_for(network)
        # Health-scored provider rotation (§2.4): a 403/429/timeout endpoint is
        # temporarily dropped instead of being retried at the same slot every cycle.
        self._rpc_pool = RpcProviderPool(
            urls=list(self._rpc_urls), network=network,
            fail_threshold=config.rpc_provider_fail_threshold,
            cooldown_base_sec=config.rpc_provider_cooldown_sec,
            cooldown_max_sec=config.rpc_provider_cooldown_max_sec,
            slow_latency_ms=config.rpc_max_healthy_latency_ms,
            permanent_fail_threshold=config.rpc_provider_permanent_fail_threshold,
            primary_urls=settings.rpc_primary_urls_for(network),
        )
        self._limiter = TokenBucket(rate_per_sec, burst)
        self._session: aiohttp.ClientSession | None = None
        self._status = ExchangeStatus.UNKNOWN
        self._taker_fee = Decimal("0.003")
        self._on_success = on_success or (lambda latency=0.0, stream=False: None)
        self._on_failure = on_failure or (lambda hard=False: None)

    @abstractmethod
    async def _list_pools(self) -> list[CanonicalSymbol]: ...

    @abstractmethod
    async def _read_pool(self, symbol: CanonicalSymbol) -> OrderBook | None: ...

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._config.dex_rpc_timeout_sec)
        # certifi-backed TLS (see adapters/tls.py) so RPC/HTTPS verify on hosts whose
        # system trust store is empty (python.org macOS build, slim Docker images).
        connector = aiohttp.TCPConnector(ssl=ssl_context())
        self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
        self._status = ExchangeStatus.UNKNOWN

    async def get_markets(self) -> list[CanonicalSymbol]:
        return await self._list_pools()

    async def subscribe_ticker(self, symbols: list[CanonicalSymbol]) -> None:
        return  # DEX has no push stream; polled per block (§4.1)

    async def subscribe_order_book(self, symbols: list[CanonicalSymbol], depth: int) -> None:
        return

    async def get_funding_rate(self, base_asset: str) -> FundingRate | None:
        return None  # DEX perp funding out of MVP DEX scope

    async def get_pool_state(self, symbol: CanonicalSymbol) -> OrderBook | None:
        try:
            book = await self._read_pool(symbol)
            if book is not None:
                # DEX has no push stream — per-block pool polling *is* its live
                # data feed, so a fresh pool read counts as stream activity for
                # staleness purposes (§4.2).
                self._on_success(stream=True)
                self._status = ExchangeStatus.ONLINE
            return book
        except Exception:  # noqa: BLE001
            self._on_failure()
            return None

    async def health_check(self) -> bool:
        try:
            block = await self.rpc_call("eth_blockNumber", [])
            ok = block is not None
            if ok:
                self._status = ExchangeStatus.ONLINE
            return ok
        except Exception:  # noqa: BLE001
            return False

    def get_status(self) -> ExchangeStatus:
        return self._status

    def taker_fee(self, symbol: CanonicalSymbol) -> Decimal:
        """Flat per-venue fallback rate. NOT used by the spot profit pipeline.

        A DEX leg's fee is taken inside the pool and is already priced into
        DexPoolLeg.fill_price via book.pool_fee_tier, so the assembler charges DEX legs
        0 on top (see assembler._charges_flat_taker_fee). This stays only to satisfy the
        ExchangeAdapter port and to serve non-profit callers; it is a generic default
        that does not track a V3 pool's real tier, so it must not re-enter fee maths.
        """
        return self._taker_fee

    def withdrawal_fee_usd(self, base_asset: str, network: str | None) -> Decimal | None:
        return Decimal(0)  # DEX same-custody / atomic — no withdrawal fee (§8.4)

    def withdrawals_enabled(self, base_asset: str, network: str | None) -> bool:
        return True

    # ── JSON-RPC with hedged, health-aware failover (§2.4) ──
    async def _rpc_attempt(
        self, url: str, payload: dict,
    ) -> tuple[object | None, RpcErrorKind | None, float, str | None]:
        """One request to one provider. Pure w.r.t. the pool — the caller records the
        outcome so a hedged batch never double-counts or records a cancelled racer.
        Returns (result, failure_kind, latency_ms, detail); result is not-None on success.
        """
        await self._limiter.acquire()
        started = time.perf_counter()
        try:
            # Adaptive per-attempt deadline: a node whose *observed* EWMA latency sits at
            # or above the base timeout is slow-but-alive, not dead — cutting it off at
            # the base deadline recorded a false TIMEOUT on most of its completions and
            # produced disable/recover churn. Grant it up to 2x its own EWMA, capped at
            # 2x the base. Fast/unknown providers keep the base deadline unchanged, and
            # the hedged fan-out (not this deadline) is what bounds call latency.
            base = self._config.dex_rpc_timeout_sec
            ewma = self._rpc_pool.ewma_latency_ms(url) / 1000.0
            deadline = max(base, min(ewma * 2.0, base * 2.0)) if ewma > 0 else base
            async with self._session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=deadline)) as resp:
                if resp.status != 200:
                    # 401 (and 403 with an auth body) means the endpoint needs a key we
                    # don't have — a permanent failure to retire, not retry. 429 is
                    # throttling; other non-200 is a generic HTTP failure.
                    if resp.status == 401 or (resp.status == 403 and _auth_body(
                            await _safe_text(resp))):
                        return (None, RpcErrorKind.UNAUTHORIZED, 0.0,
                                f"http {resp.status} (auth required)")
                    kind = (RpcErrorKind.RATE_LIMIT if resp.status == 429
                            else RpcErrorKind.HTTP)
                    return None, kind, 0.0, f"http {resp.status}"
                data = await resp.json(content_type=None)
                if "error" in data:
                    err = data["error"]
                    # Some providers return 200 with a JSON-RPC auth error instead of 401.
                    if _auth_body(str(err)):
                        return None, RpcErrorKind.UNAUTHORIZED, 0.0, str(err)
                    return None, RpcErrorKind.RPC_ERROR, 0.0, str(err)
                result = data.get("result")
                if result is None:
                    # Some public nodes answer 200 with a null result when degraded —
                    # treat as a failure and fail over rather than surfacing a fake outage.
                    return None, RpcErrorKind.RPC_ERROR, 0.0, "null result"
                return result, None, (time.perf_counter() - started) * 1000, None
        except (TimeoutError, aiohttp.ServerTimeoutError):
            return None, RpcErrorKind.TIMEOUT, 0.0, "timeout"
        except asyncio.CancelledError:
            raise  # a losing racer was cancelled after a peer won — not a provider fault
        except Exception as exc:  # noqa: BLE001 — connection reset / DNS / TLS
            return None, RpcErrorKind.HTTP, 0.0, describe_exc(exc)

    async def rpc_call(self, method: str, params: list) -> object | None:
        if not self._rpc_urls or self._session is None:
            return None
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        # Health-ranked, cooldown-aware order: disabled providers are already excluded
        # (see RpcProviderPool.order), so this is the set worth trying now, best-first.
        order = self._rpc_pool.order()[:max(1, self._config.rpc_max_providers_per_call)]
        hedge = max(1, self._config.rpc_hedge_factor)
        last_err: str | None = None
        # Fan out in hedged batches: race `hedge` providers at once so a single slow node
        # can't hold the whole call hostage. First good result wins and cancels the rest;
        # failures are recorded and we fall through to the next batch. Worst case is
        # ceil(len(order)/hedge) x dex_rpc_timeout_sec, not len(order) x timeout.
        for start in range(0, len(order), hedge):
            batch = order[start:start + hedge]
            tasks = {asyncio.create_task(self._rpc_attempt(u, payload)): u for u in batch}
            pending = set(tasks)
            winner: object | None = None
            try:
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        url = tasks[task]
                        result, kind, latency, detail = task.result()
                        if result is not None:
                            self._rpc_pool.record_success(url, latency_ms=latency)
                            winner = result
                            break
                        self._rpc_pool.record_failure(url, kind)  # type: ignore[arg-type]
                        last_err = detail
                        log.debug("rpc_failover", network=self.network, url=url,
                                  kind=kind.value if kind else "?", error=detail)
                    if winner is not None:
                        break
            finally:
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            if winner is not None:
                return winner
        # Every provider tried this call failed — WARNING (throttled per network) with the
        # full per-provider health snapshot so prod shows exactly which endpoints are down.
        emit, suppressed = _exhausted_log_throttle.allow(self.network)
        if emit:
            log.warning("rpc_all_providers_failed", venue=getattr(self, "id", None),
                        network=self.network, method=method, providers=len(order),
                        error=last_err, health=self._rpc_pool.snapshot(),
                        repeats_suppressed=suppressed)
        else:
            log.debug("rpc_all_providers_failed", network=self.network,
                      method=method, error=last_err)
        return None

    async def eth_call(self, to: str, data: str) -> str | None:
        result = await self.rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
        return result if isinstance(result, str) else None
