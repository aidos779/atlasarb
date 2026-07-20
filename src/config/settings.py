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
    # eth.merkle.io was removed 2026-07-19 for the same reason: it now requires
    # authentication, so every probe burned a failover slot before being retired.
    "ethereum": (
        "https://ethereum.publicnode.com",     # PublicNode / Allnodes
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

# Alchemy per-network subdomain — used to build an authenticated endpoint from ALCHEMY_API_KEY.
_ALCHEMY_SUBDOMAIN: dict[str, str] = {
    "ethereum": "eth-mainnet", "bnb": "bnb-mainnet", "arbitrum": "arb-mainnet",
    "optimism": "opt-mainnet", "base": "base-mainnet", "polygon": "polygon-mainnet",
}


# Development default for BOT_TOKEN — treated as "unset" by the production preflight.
_PLACEHOLDER_BOT_TOKEN = "TEST:TOKEN"


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
    bot_token: str = _PLACEHOLDER_BOT_TOKEN
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

    # ── Paid / authenticated RPC providers (primary tier) ──
    # When configured these are placed FIRST in the pool and marked as the primary tier
    # (see rpc_primary_urls_for): the health-scored rotation prefers them over the public
    # fallback nodes and only drops to the free endpoints when every primary is unhealthy.
    # This is the fix for the prod "all public ETH/BNB providers rate-limited at once"
    # storms (rpc_all_providers_failed) — a reliable authenticated endpoint should carry
    # normal traffic while the public list stays as failover only.
    #
    # ALCHEMY_API_KEY builds https://<net>.g.alchemy.com/v2/<key> for each supported net.
    # QUICKNODE_*_URL are full, token-bearing endpoint URLs (QuickNode issues a distinct
    # host per network/account, so they cannot be derived from a bare key).
    alchemy_api_key: str = ""
    quicknode_ethereum_url: str = ""
    quicknode_bnb_url: str = ""

    # Optional Ankr API key. Public unauthenticated Ankr endpoints (rpc.ankr.com/<chain>)
    # now return 401/Unauthorized and are dropped from the pool. With a key set, an
    # authenticated Ankr endpoint (rpc.ankr.com/<chain>/<key>) is added to the primary
    # tier; without one, Ankr is skipped entirely rather than retried forever.
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

    def production_config_errors(self) -> list[str]:
        """Fatal misconfigurations for a production boot — empty list means safe to start.

        Every default in this class is a *development* default: a placeholder bot token,
        a local SQLite file, literal "change-me" secrets. Each one boots silently and
        fails later in a way that looks like a bug rather than a deploy mistake — a
        SQLite-backed prod loses every user row on container restart, and a "change-me"
        webhook secret means anyone can forge a payment callback.

        Returns human-readable reasons rather than raising so the caller can log all of
        them at once; a half-fixed deploy should not require N restarts to find N errors.
        Non-production environments are never checked — they are expected to use defaults.
        """
        if not self.is_production:
            return []
        errors: list[str] = []

        if not self.bot_token or self.bot_token == _PLACEHOLDER_BOT_TOKEN:
            errors.append("BOT_TOKEN is unset or still the placeholder value")
        elif ":" not in self.bot_token:
            errors.append("BOT_TOKEN is malformed (expected '<id>:<secret>')")

        if self.database_url.startswith("sqlite"):
            errors.append(
                "DATABASE_URL points at SQLite — production requires PostgreSQL "
                "(a container restart would discard every user, subscription and signal)")
        elif not self.database_url.strip():
            errors.append("DATABASE_URL is unset")

        for field_name, env_name, placeholder in (
            ("telegram_webhook_secret", "TELEGRAM_WEBHOOK_SECRET", "change-me"),
            ("payment_webhook_secret", "PAYMENT_WEBHOOK_SECRET", "change-me-payment"),
        ):
            value = getattr(self, field_name)
            if not value or value == placeholder:
                errors.append(f"{env_name} is unset or still the default placeholder")

        if not self.admin_user_ids:
            errors.append(
                "ADMIN_USER_IDS is empty — no operator could reach the admin panel")

        if not self.redis_url.strip():
            errors.append("REDIS_URL is unset")

        # Conflicting configuration: the dev paywall bypass must never be reachable in
        # production. It is keyed off `is_production` at the composition root, so this
        # is a belt-and-braces assertion that the two never disagree.
        from src.domain.dev_mode import unlimited_access_enabled
        if unlimited_access_enabled():
            errors.append(
                "unlimited (paywall-free) access is enabled while ENVIRONMENT=production")

        return errors

    def _primary_rpc_urls(self, network: str) -> list[str]:
        """Paid/authenticated primary-tier endpoints for a network, highest-priority first.

        Empty unless a paid provider is configured — so a deployment with no keys keeps the
        pre-existing public-only behaviour. QuickNode (an explicit token-bearing URL) ranks
        ahead of Alchemy, then authenticated Ankr, purely as a stable default ordering; the
        pool's health scoring still routes traffic to whichever primary is actually fastest.
        """
        net = network.lower()
        urls: list[str] = []
        quicknode = {"ethereum": self.quicknode_ethereum_url,
                     "bnb": self.quicknode_bnb_url}.get(net, "")
        if quicknode.strip():
            urls.append(quicknode.strip())
        if self.alchemy_api_key and net in _ALCHEMY_SUBDOMAIN:
            urls.append(f"https://{_ALCHEMY_SUBDOMAIN[net]}.g.alchemy.com/v2/"
                        f"{self.alchemy_api_key}")
        if self.ankr_api_key and net in _ANKR_CHAIN:
            urls.append(f"https://rpc.ankr.com/{_ANKR_CHAIN[net]}/{self.ankr_api_key}")
        # Dedupe defensively while preserving priority order.
        deduped: list[str] = []
        for u in urls:
            if u not in deduped:
                deduped.append(u)
        return deduped

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
        # Order = paid primaries first, then operator-configured nodes, then the extra public
        # fallbacks (all deduped, priority preserved). On a rate-limited/blocked production
        # host the public providers can all 429/403 at once — the Ethereum-DEX "API Offline"
        # symptom — so the primaries carry traffic and the public list is failover only.
        # rpc_call still tries them best-first and the token bucket bounds the rate.
        for url in [*self._primary_rpc_urls(net), *mapping.get(net, []),
                    *_EXTRA_FALLBACK_RPCS.get(net, ())]:
            resolved = self._resolve_ankr(url, net)
            if resolved is not None and resolved not in urls:
                urls.append(resolved)
        return urls

    def rpc_primary_urls_for(self, network: str) -> set[str]:
        """The subset of rpc_urls_for(network) that is a paid/authenticated primary — the
        pool marks these tier 0 so they are preferred over the public fallback nodes."""
        net = network.lower()
        return {r for u in self._primary_rpc_urls(net)
                if (r := self._resolve_ankr(u, net)) is not None}

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
