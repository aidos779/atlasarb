"""Base DEX adapter (Scanner §2.4, §5.4).

Shared JSON-RPC transport with ≥2-provider failover per network (§2.4), health via
block-number probe, and pool-state reads. Concrete DEX modules supply their pool set
and reserve-decoding. Data sourced via official RPC/SDK/API only (ARCH-2); pool sets are
verified-token-list gated (§3.4) — the curated pool list is engine *data* input, not code.
"""
from __future__ import annotations

from abc import abstractmethod
from decimal import Decimal

import aiohttp

from src.config import describe_exc, get_logger
from src.config.scanner_config import ScannerConfig
from src.config.settings import Settings
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, FundingRate, OrderBook
from src.domain.ports import ExchangeAdapter, MarketDataSink
from src.scanner.adapters.rate_limiter import TokenBucket

log = get_logger("adapter.dex")


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
        self._rpc_idx = 0
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
        self._session = aiohttp.ClientSession(timeout=timeout)

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
        return self._taker_fee

    def withdrawal_fee_usd(self, base_asset: str, network: str | None) -> Decimal | None:
        return Decimal(0)  # DEX same-custody / atomic — no withdrawal fee (§8.4)

    def withdrawals_enabled(self, base_asset: str, network: str | None) -> bool:
        return True

    # ── JSON-RPC with failover (§2.4) ──
    async def rpc_call(self, method: str, params: list) -> object | None:
        if not self._rpc_urls or self._session is None:
            return None
        attempts = len(self._rpc_urls)
        for _ in range(attempts):
            url = self._rpc_urls[self._rpc_idx % len(self._rpc_urls)]
            await self._limiter.acquire()
            try:
                async with self._session.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ) as resp:
                    if resp.status != 200:
                        raise ConnectionError(f"rpc {resp.status}")
                    data = await resp.json()
                    if "error" in data:
                        raise ValueError(str(data["error"]))
                    return data.get("result")
            except Exception as exc:  # noqa: BLE001 — failover to next provider
                log.debug("rpc_failover", network=self.network, url=url, error=describe_exc(exc))
                self._rpc_idx += 1
        return None

    async def eth_call(self, to: str, data: str) -> str | None:
        result = await self.rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
        return result if isinstance(result, str) else None
