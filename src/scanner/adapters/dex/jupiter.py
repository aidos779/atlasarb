"""Jupiter adapter (Solana aggregator) — official token + price APIs (ARCH-2, §4.1/§5.4).

Discovery: the verified-token catalog (tokens/v2 tag=verified) is filtered against the
§3.4 verified-asset allowlist, giving mints/decimals as data. Pricing: one batched
price/v3 call covers every tracked mint (with per-token routed liquidity), refreshed on
a short TTL — so pool polling costs O(1) HTTP requests per cycle instead of O(pairs),
which is what the free-tier rate limit allows. To fit the engine's uniform DEX leg
model each price is expressed as equivalent constant-product reserves sized to
Jupiter's reported routed liquidity (documented approximation for aggregator depth).
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.config import describe_exc, get_logger
from src.domain.enums import ExchangeStatus, VenueType
from src.domain.market import CanonicalSymbol, OrderBook
from src.scanner.adapters.base_dex import BaseDexAdapter
log = get_logger("adapter.dex")

# Solana-native verified symbols only. EVM-origin symbols (SHIB/PEPE/ETH/BTC…) are
# excluded: Jupiter's "verified" tag contains unrelated Solana tokens reusing those
# tickers (e.g. a Solana meme called SHIB at 200× the real SHIB price), which forged
# implausible cross-chain spreads against the EVM DEX legs.
_ALLOWED_BASES: set[str] = {"SOL", "JUP", "BONK", "WIF", "JTO", "PYTH", "RAY",
                            "ORCA", "RENDER", "WLD"}

_SOL_MINT = "So11111111111111111111111111111111111111112"
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
_QUOTES = {"USDC": _USDC_MINT, "USDT": _USDT_MINT}

_PRICE_TTL_SEC = 4.0            # batch price refresh cadence (≈ block-ish)
_MAX_BASES = 40                 # price/v3 accepts up to 50 ids per call
_MIN_ROUTED_LIQUIDITY_USD = Decimal(50_000)
_LIQUIDITY_CAP_USD = Decimal(2_000_000)


class JupiterAdapter(BaseDexAdapter):
    id = "jupiter"
    display_name = "Jupiter"

    def __init__(self, settings, config, sink, **kw) -> None:
        super().__init__(settings, config, sink, network="solana", rate_per_sec=10, burst=20, **kw)
        # jupiter_api_url points at .../swap/v1; token & price APIs share the host.
        self._api_root = settings.jupiter_api_url.rstrip("/").rsplit("/swap", 1)[0]
        self._mints: dict[str, str] = {"SOL": _SOL_MINT}        # symbol -> mint
        self._discovered_at = 0.0
        # Jupiter routes through concentrated/stable pools; ~0.10% effective fee
        # (matches the pool_fee_tier we attach to synthesized books).
        self._taker_fee = Decimal("0.001")
        self._prices: dict[str, dict] = {}                      # mint -> price/v3 row
        self._prices_at = 0.0

    # ── discovery (verified catalog ∩ §3.4 allowlist) ──
    async def _maybe_discover(self) -> None:
        now = time.time()
        if self._discovered_at and now - self._discovered_at < self._config.dex_discovery_interval_sec:
            return
        if self._session is None:
            return
        try:
            await self._limiter.acquire()
            async with self._session.get(
                f"{self._api_root}/tokens/v2/tag", params={"query": "verified"}
            ) as resp:
                if resp.status != 200:
                    return
                rows = await resp.json()
        except Exception as exc:  # noqa: BLE001 — keep previous universe
            log.debug("jupiter_discovery_failed", error=describe_exc(exc))
            return
        mints: dict[str, str] = {"SOL": _SOL_MINT}
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            mint = row.get("id")
            if not mint or symbol not in _ALLOWED_BASES:
                continue
            if symbol not in mints and len(mints) < _MAX_BASES:
                mints[symbol] = mint
        self._mints = mints
        self._discovered_at = now
        log.info("dex_pools_discovered", venue=self.id, network="solana",
                 pools=len(mints) * len(_QUOTES))

    async def _list_pools(self) -> list[CanonicalSymbol]:
        await self._maybe_discover()
        return [CanonicalSymbol(base, quote, VenueType.DEX, "solana")
                for base in self._mints for quote in _QUOTES]

    # ── batched pricing ──
    async def _refresh_prices(self) -> None:
        now = time.time()
        if now - self._prices_at < _PRICE_TTL_SEC or self._session is None:
            return
        ids = list(self._mints.values()) + [_USDC_MINT, _USDT_MINT]
        await self._limiter.acquire()
        async with self._session.get(
            f"{self._api_root}/price/v3", params={"ids": ",".join(ids)}
        ) as resp:
            if resp.status != 200:
                raise ConnectionError(f"jupiter price {resp.status}")
            data = await resp.json()
        if not isinstance(data, dict) or not data:
            raise ValueError("jupiter price payload empty")
        self._prices = data
        self._prices_at = now

    async def health_check(self) -> bool:
        if self._session is None:
            return False
        try:
            self._prices_at = 0.0  # force a real round-trip
            await self._refresh_prices()
            ok = _SOL_MINT in self._prices
            if ok:
                self._status = ExchangeStatus.ONLINE
            return ok
        except Exception:  # noqa: BLE001
            return False

    async def _read_pool(self, symbol: CanonicalSymbol) -> OrderBook | None:
        base_mint = self._mints.get(symbol.base_asset)
        quote_mint = _QUOTES.get(symbol.quote_asset)
        if base_mint is None or quote_mint is None:
            return None
        await self._refresh_prices()
        base_row = self._prices.get(base_mint)
        quote_row = self._prices.get(quote_mint)
        if not base_row or not quote_row:
            return None
        base_usd = Decimal(str(base_row.get("usdPrice", 0)))
        quote_usd = Decimal(str(quote_row.get("usdPrice", 0)))
        if base_usd <= 0 or quote_usd <= 0:
            return None
        price = base_usd / quote_usd  # quote per base
        liquidity = Decimal(str(base_row.get("liquidity", 0) or 0))
        if liquidity < _MIN_ROUTED_LIQUIDITY_USD:
            return None  # too thin to treat as a routable venue
        reserve_quote = min(liquidity / 2, _LIQUIDITY_CAP_USD)
        reserve_base = reserve_quote / price
        return OrderBook(
            venue=self.id, symbol=symbol, bids=[], asks=[],
            pool_address=f"jupiter:{symbol.base_asset}", pool_fee_tier=Decimal("0.001"),
            reserve_base=reserve_base, reserve_quote=reserve_quote,
        )
