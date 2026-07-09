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
    "ethereum": (
        "https://ethereum.publicnode.com",
        "https://eth.merkle.io",
        "https://rpc.mevblocker.io",
        "https://eth-mainnet.public.blastapi.io",
        "https://eth.rpc.blxrbdn.com",
        "https://eth.api.onfinality.io/public",
        "https://rpc.flashbots.net",
    ),
    "bnb": (
        "https://bsc.publicnode.com",
        "https://bsc-dataseed1.defibit.io",
        "https://bsc-dataseed1.ninicoin.io",
    ),
    "arbitrum": ("https://arbitrum-one.publicnode.com", "https://arbitrum.meowrpc.com"),
    "optimism": ("https://optimism.publicnode.com", "https://optimism.meowrpc.com"),
    "base": ("https://base.publicnode.com", "https://base.meowrpc.com"),
    "polygon": ("https://polygon-bor.publicnode.com", "https://polygon.meowrpc.com"),
    "solana": ("https://solana-rpc.publicnode.com",),
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
    bybit_rest_url: str = "https://api.bybit.com"
    bybit_ws_url: str = "wss://stream.bybit.com/v5/public/spot"
    # Optional per-adapter proxy for Bybit only (its CDN 403-blocks some server IPs on
    # every host). Empty = direct. Accepts http://, https://, socks5:// (see BybitAdapter).
    bybit_proxy: str = ""
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
        urls = list(mapping.get(network.lower(), []))
        # Append extra public fallbacks (deduped, preserving configured priority). On a
        # rate-limited/blocked production host the configured providers can all 429/403 at
        # once — the Ethereum-DEX "API Offline" symptom — so the failover chain must have
        # independent providers to rotate to. rpc_call still tries them in order and the
        # token bucket bounds the rate.
        for extra in _EXTRA_FALLBACK_RPCS.get(network.lower(), ()):
            if extra not in urls:
                urls.append(extra)
        return urls


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — env is read once per process."""
    return Settings()
