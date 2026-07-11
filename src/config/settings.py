"""Application settings loaded from environment variables (12-factor).

Secrets never hardcoded — everything comes from the environment / .env.
See .env.example for the full list.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import (
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
)

# Fields that accept comma-separated env values instead of JSON arrays.
_CSV_FIELDS = frozenset(
    {
        "admin_user_ids",
        "support_user_ids",
        "ethereum_rpc_urls",
        "bnb_rpc_urls",
        "arbitrum_rpc_urls",
        "optimism_rpc_urls",
        "base_rpc_urls",
        "polygon_rpc_urls",
        "solana_rpc_urls",
    }
)


# Extra independent public RPC providers appended after the configured ones as failover
# targets (see Settings.rpc_urls_for). Keyed by network; Ethereum is the one whose public
# endpoints most often rate-limit a shared/cloud IP, offlining the Uniswap/Sushi venues.
_EXTRA_FALLBACK_RPCS: dict[str, tuple[str, ...]] = {
    # Ethereum endpoints below were each verified to return a real eth_blockNumber from a
    # clean network; flaky ones (that 200 with a null result or serve HTML) were dropped
    # because they would poison the failover chain.
    # Independent operators (different data centres / companies) so one operator's outage
    # can't take out several list entries at once. Each verified to return a live
    # eth_blockNumber without any API key on 2026-07-11. Public Ankr (rpc.ankr.com/eth)
    # is deliberately absent — it now answers "Unauthorized: you must be authenticated",
    # so it is retired at config time by _resolve_ankr unless ANKR_API_KEY is set.
    "ethereum": (
        "https://ethereum.publicnode.com",     # PublicNode / Allnodes
        "https://eth.merkle.io",               # Merkle
        "https://rpc.mevblocker.io",           # MevBlocker / CoW
        "https://eth-mainnet.public.blastapi.io",  # Bware / BlastAPI
        "https://eth.rpc.blxrbdn.com",         # bloXroute
        "https://eth.api.onfinality.io/public",    # OnFinality
        "https://rpc.flashbots.net",           # Flashbots
        "https://eth.meowrpc.com",             # MeowRPC
        "https://eth-pokt.nodies.app",         # Nodies / POKT
        "https://gateway.tenderly.co/public/mainnet",  # Tenderly
        "https://api.zan.top/eth-mainnet",     # Zan
        "https://eth.blockrazor.xyz",          # BlockRazor
    ),
    "bnb": (
        "https://bsc.publicnode.com",          # PublicNode / Allnodes
        "https://bsc-dataseed1.defibit.io",    # Defibit
        "https://bsc-dataseed1.ninicoin.io",   # Ninicoin
        "https://1rpc.io/bnb",                 # Automata 1RPC
        "https://bsc.meowrpc.com",             # MeowRPC
        "https://bsc-pokt.nodies.app",         # Nodies / POKT
        "https://bsc.drpc.org",                # dRPC
    ),
    "arbitrum": ("https://arbitrum-one.publicnode.com", "https://arbitrum.meowrpc.com"),
    "optimism": ("https://optimism.publicnode.com", "https://optimism.meowrpc.com"),
    "base": ("https://base.publicnode.com", "https://base.meowrpc.com"),
    "polygon": ("https://polygon-bor.publicnode.com", "https://polygon.meowrpc.com"),
    "solana": ("https://solana-rpc.publicnode.com",),
}

# Ankr's URL path segment per network (used only when an API key is configured, to
# rewrite an unauthenticated endpoint into its authenticated form).
_ANKR_CHAIN: dict[str, str] = {
    "ethereum": "eth", "bnb": "bsc", "arbitrum": "arbitrum", "optimism": "optimism",
    "base": "base", "polygon": "polygon", "solana": "solana",
}


def _split_csv(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    # Tolerate a literal empty-array string ("[]") left over from JSON-style configs.
    if value.strip() in ("", "[]"):
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class _CsvDecodeMixin:
    """Skip JSON decoding for CSV fields so field validators receive the raw string.

    pydantic-settings tries to ``json.loads`` complex (list) fields from the env
    source before any validator runs, which breaks comma-separated values. For the
    CSV fields we return the raw value untouched and let the before-validators parse.
    """

    def decode_complex_value(self, field_name: str, field: FieldInfo, value: Any) -> Any:
        if field_name in _CSV_FIELDS:
            return value
        return super().decode_complex_value(field_name, field, value)


class _CsvEnvSource(_CsvDecodeMixin, EnvSettingsSource):
    pass


class _CsvDotEnvSource(_CsvDecodeMixin, DotEnvSettingsSource):
    pass


class Settings(BaseSettings):
    """Root settings object. Immutable per-process; scanner tuning lives in ScannerConfig."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ── Core ──
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # ── Telegram ──
    bot_token: str = "TEST:TOKEN"
    bot_username: str = "arb_bot"
    telegram_webhook_secret: str = "change-me"
    admin_user_ids: list[int] = Field(default_factory=list)
    support_user_ids: list[int] = Field(default_factory=list)

    # ── Database / Redis ──
    database_url: str = "sqlite+aiosqlite:///./arb.db"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    redis_url: str = "redis://localhost:6379/0"

    # ── Payments ──
    telegram_payments_provider_token: str = ""
    payment_webhook_secret: str = "change-me-payment"
    price_basic_usd: float = 19.0
    price_pro_usd: float = 79.0

    # ── CEX endpoints ──
    # api.binance.com / stream.binance.com return HTTP 451 from restricted locations.
    # data-api / data-stream.binance.vision are Binance's dedicated public market-data
    # domains and serve the identical spot REST + WS; all public Binance data uses them.
    binance_rest_url: str = "https://data-api.binance.vision"
    binance_ws_url: str = "wss://data-stream.binance.vision/stream"
    # Optional dedicated proxy chain for Binance (some server IPs get HTTP 451 on every
    # Binance host). BINANCE_PROXY is the primary; BINANCE_PROXY_FALLBACK is tried when
    # the primary fails. Empty = direct. Accepts http://, https://, socks4://, socks5://.
    # Applies to Binance REST + WS only (see BinanceAdapter / BaseCexAdapter proxy chain).
    binance_proxy: str = ""
    binance_proxy_fallback: str = ""
    # www.okx.com is geo-blocked from several regions (TCP timeout); the app.okx.com
    # mirror serves the identical v5 public API and stays reachable.
    okx_rest_url: str = "https://app.okx.com"
    okx_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    bitget_rest_url: str = "https://api.bitget.com"
    bitget_ws_url: str = "wss://ws.bitget.com/v2/ws/public"
    mexc_rest_url: str = "https://api.mexc.com"
    # wbs.mexc.com is the legacy WS host: it rejects the discontinued JSON channels
    # ("Blocked!") and drops TCP from some networks. wbs-api.mexc.com is the current
    # protobuf endpoint.
    mexc_ws_url: str = "wss://wbs-api.mexc.com/ws"

    # ── DEX RPC ──
    ethereum_rpc_urls: list[str] = Field(default_factory=list)
    bnb_rpc_urls: list[str] = Field(default_factory=list)
    arbitrum_rpc_urls: list[str] = Field(default_factory=list)
    optimism_rpc_urls: list[str] = Field(default_factory=list)
    base_rpc_urls: list[str] = Field(default_factory=list)
    polygon_rpc_urls: list[str] = Field(default_factory=list)
    solana_rpc_urls: list[str] = Field(default_factory=list)
    jupiter_api_url: str = "https://lite-api.jup.ag/swap/v1"

    # Optional Ankr API key. Public unauthenticated Ankr endpoints (rpc.ankr.com/<chain>)
    # now return 401/Unauthorized and are dropped from the pool. With a key set, they are
    # rewritten to the authenticated form (rpc.ankr.com/<chain>/<key>); without one, Ankr
    # is skipped entirely rather than retried forever.
    ankr_api_key: str = ""

    scanner_config_file: str = "config/scanner.toml"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Swap in CSV-aware env sources so comma-separated values are not JSON-decoded.
        return (
            init_settings,
            _CsvEnvSource(settings_cls),
            _CsvDotEnvSource(settings_cls),
            file_secret_settings,
        )

    @field_validator(
        "admin_user_ids", "support_user_ids", mode="before"
    )
    @classmethod
    def _parse_int_csv(cls, v: object) -> list[int]:
        return [int(x) for x in _split_csv(v if isinstance(v, (str, list)) else None)]

    @field_validator(
        "ethereum_rpc_urls", "bnb_rpc_urls", "arbitrum_rpc_urls", "optimism_rpc_urls",
        "base_rpc_urls", "polygon_rpc_urls", "solana_rpc_urls", mode="before",
    )
    @classmethod
    def _parse_str_csv(cls, v: object) -> list[str]:
        return _split_csv(v if isinstance(v, (str, list)) else None)

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def rpc_urls_for(self, network: str) -> list[str]:
        mapping = {
            "ethereum": self.ethereum_rpc_urls,
            "bnb": self.bnb_rpc_urls,
            "arbitrum": self.arbitrum_rpc_urls,
            "optimism": self.optimism_rpc_urls,
            "base": self.base_rpc_urls,
            "polygon": self.polygon_rpc_urls,
            "solana": self.solana_rpc_urls,
        }
        net = network.lower()
        urls: list[str] = []
        # Append extra public fallbacks (deduped, preserving configured priority). On a
        # rate-limited/blocked production host the configured providers can all 429/403 at
        # once — the Ethereum-DEX "API Offline" symptom — so the failover chain must have
        # independent providers to rotate to. rpc_call still tries them in order and the
        # token bucket bounds the rate.
        for url in [*mapping.get(net, []), *_EXTRA_FALLBACK_RPCS.get(net, ())]:
            resolved = self._resolve_ankr(url, net)
            if resolved is not None and resolved not in urls:
                urls.append(resolved)
        return urls

    def _resolve_ankr(self, url: str, network: str) -> str | None:
        """Auth-aware Ankr handling. A public ``rpc.ankr.com/<chain>`` endpoint is
        dropped (returns None) unless an API key is configured, in which case it is
        rewritten to the authenticated ``rpc.ankr.com/<chain>/<key>`` form. Endpoints
        that already carry a key, and every non-Ankr URL, pass through unchanged."""
        if "rpc.ankr.com" not in url:
            return url
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        # Authenticated form already has a key segment after the chain (e.g. /eth/<key>).
        if len(segments) >= 2:
            return url
        if not self.ankr_api_key:
            return None  # skip unauthenticated Ankr entirely
        chain = segments[0] if segments else _ANKR_CHAIN.get(network, network)
        return f"{parts.scheme}://{parts.netloc}/{chain}/{self.ankr_api_key}"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — env is read once per process."""
    return Settings()
