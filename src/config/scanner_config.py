"""Scanner Configuration surface — implements Spec Part 3 §20.

Every threshold/interval/limit referenced as "configurable" lives here. No scanner
parameter requires a code change to adjust (§20 / Business Rule 21). Supports:
  - typed parameters with ranges (§20.3 validation)
  - cross-parameter invariants enforced at load time (§20.3)
  - layered override: base defaults -> environment layer -> runtime overrides (§20.6)
  - hot-reload with subscriber notification (§20.4)
  - introspection of effective value + originating layer (§20.6)
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any


class ConfigError(ValueError):
    """Raised when a configuration change is invalid (§20.3 — all-or-nothing)."""


class ArbType(StrEnum):
    CEX_CEX = "CEX_CEX"
    CEX_DEX = "CEX_DEX"
    DEX_DEX = "DEX_DEX"
    FUNDING = "FUNDING"
    CROSS_CHAIN = "CROSS_CHAIN"


# Per-arbitrage-type TTL (seconds) — §12.4
DEFAULT_TTLS: dict[str, int] = {
    ArbType.CEX_CEX.value: 120,
    ArbType.CEX_DEX.value: 180,
    ArbType.DEX_DEX.value: 90,
    ArbType.CROSS_CHAIN.value: 600,
    ArbType.FUNDING.value: 3600,  # funding-interval governed; upper bound guard
}

# Per-type cooldown (seconds) — §13.2 (Funding N/A -> 0 meaning interval-governed)
DEFAULT_COOLDOWNS: dict[str, int] = {
    ArbType.CEX_CEX.value: 60,
    ArbType.DEX_DEX.value: 60,
    ArbType.CEX_DEX.value: 180,
    ArbType.CROSS_CHAIN.value: 180,
    ArbType.FUNDING.value: 0,
}


@dataclass
class ScannerConfig:
    """Central, hot-reloadable scanner configuration (§20.1 parameters)."""

    # ── Profit / validation gates (§10, §8) ──
    min_net_profit_usd: float = 5.0            # §10 Minimum Profit gate
    min_roi_pct: float = 0.15                  # §10 both bars must pass
    min_gross_profit_pct: float = 0.10         # §8 early-exit floor
    max_gas_pct_of_gross: float = 50.0         # §7.2 gas ceiling
    max_slippage_cex_pct: float = 1.0          # §9.2
    max_slippage_dex_pct: float = 1.5          # §9.2
    min_liquidity_cex_usd: float = 5000.0      # §9.1
    min_liquidity_dex_usd: float = 10000.0     # §9.1
    liquidity_score_floor: float = 30.0        # §9.4
    min_tradeable_size_usd: float = 100.0      # §10 liquidity gate
    max_sane_spread_pct: float = 20.0          # §4.5 invalid-price ceiling
    implausible_spread_pct: float = 1000.0     # §15.5 last-resort validator ceiling
    # CEX↔CEX price-spread plausibility ceiling (§4.5). Real same-asset spot spreads
    # between exchanges sit well under a few percent; a double-digit "spread" is almost
    # always a ticker collision (same symbol, DIFFERENT underlying token per venue — e.g.
    # AI on Binance vs a different AI on OKX) or a stale/bad tick, not a tradeable arb.
    # Candidates above this are dropped in the detector, BEFORE the profit pipeline.
    # Does not apply to funding/cross-chain (their "spread" is a projected carry).
    max_plausible_cex_spread_pct: float = 10.0
    # Explicit ticker-collision denylist: base assets whose ticker maps to DIFFERENT
    # underlying tokens across venues (no shared identity), so a CEX↔CEX "spread" on them
    # is never a real arb. Dropped at the identity level BEFORE the spread heuristic, so a
    # known collision (e.g. "AI") stops re-tripping the plausibility guard every tick.
    # Seeded with collisions observed in production; extend as new ones are found.
    ambiguous_tickers: frozenset[str] = frozenset({"AI"})
    stablecoin_crossquote_bps: float = 3.0     # §8.9 default conversion cost

    # ── Bridge / cross-chain (§7.5) ──
    max_bridge_time_sec: int = 1200            # 20 min hard ceiling
    fast_bridge_threshold_sec: int = 120       # < 2 min -> eligible for TOP

    # ── Funding-rate arbitrage (§7.4 / §8.10) ──
    funding_hold_hours: float = 168.0          # projection horizon (carry is held, not scalped)
    funding_position_size_usd: float = 1000.0  # notional per delta-neutral leg

    # ── Confidence & ranking (§10, §11) ──
    confidence_threshold: float = 70.0         # §10 min-confidence gate / R-SCHEMA-2
    rank_top_min: float = 85.0                 # §11.3
    rank_high_min: float = 70.0
    rank_medium_min: float = 50.0

    # ── Lifetime / cooldown (§12, §13) ──
    ttl_by_type: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TTLS))
    cooldown_by_type: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_COOLDOWNS))
    significant_profit_change_pct: float = 15.0  # §12.3 / §13.4

    # ── Discovery (§3) ──
    cex_discovery_interval_sec: int = 900      # 15 min
    dex_discovery_interval_sec: int = 300      # 5 min
    warmup_samples: int = 3                    # §3.1
    delist_grace_period_sec: int = 1800        # 30 min §3.2

    # ── Timeouts / retries (§2, §15.2) ──
    cex_rest_timeout_sec: float = 5.0
    # Market discovery (exchangeInfo) returns a large payload — Binance's ~700-pair
    # response repeatedly timed out at the 5s general REST budget. Give discovery its own
    # larger timeout so a slow-but-working listing fetch is not aborted as a failure.
    cex_discovery_timeout_sec: float = 20.0
    # Per-provider RPC budget. rpc_call fails over across providers *sequentially*, so a
    # network whose endpoints all time out costs up to N x this value on a single call.
    # Kept at 5s (not 8s) so a fully-degraded network exhausts its failover chain fast
    # instead of stacking into tens of seconds and starving co-hosted loop work (the
    # notification consumer shares this event loop). 5s still clears a healthy mainnet
    # eth_call comfortably.
    dex_rpc_timeout_sec: float = 5.0
    healthcheck_timeout_sec: float = 3.0
    rest_max_attempts: int = 3
    ws_fast_reconnect_max: int = 6             # §2.3
    ws_backoff_base_sec: float = 1.0
    ws_backoff_multiplier: float = 2.0
    ws_backoff_cap_sec: float = 30.0
    ws_backoff_jitter: float = 1.0             # full jitter → shards de-sync (no storm)
    # Interval between reconnect attempts once a shard has escalated to the offline state.
    # Kept short (and paired with per-cycle host/proxy rotation) so an offline shard is
    # recovered in tens of seconds, not the tens of minutes seen when it stuck on a single
    # blocked mirror.
    ws_slow_retry_interval_sec: float = 30.0
    # A single shard offline longer than this is escalated to a `ws_shard_offline` alert
    # (partial market-data loss for that venue's symbol range). 0 disables the alert.
    ws_offline_alert_sec: float = 300.0
    ws_heartbeat_timeout_sec: float = 20.0
    ws_ping_interval_sec: float = 15.0         # app-level keepalive ping cadence (§2.3)
    ws_idle_timeout_sec: float = 60.0          # no inbound frame this long -> reconnect
    # Per-shard connect/reconnect stagger (§2.3): shards open `idx * stagger` apart and
    # each reconnect adds up to `stagger` of extra random delay, so a common upstream
    # drop does not turn into a synchronized reconnect storm across every shard.
    ws_connect_stagger_sec: float = 0.5

    # ── DEX RPC provider health (§2.4 failover) ──
    # Consecutive failures before a provider is temporarily disabled; cooldown grows
    # from base to cap as it keeps failing, then it is probed again (auto-recovery).
    # Threshold 5 tolerates isolated/short-lived blips (a public node dropping 2-3
    # requests) without pulling the provider from rotation; a genuinely dead endpoint
    # still trips quickly because its failures are consecutive.
    rpc_provider_fail_threshold: int = 5
    rpc_provider_cooldown_sec: float = 20.0
    rpc_provider_cooldown_max_sec: float = 300.0
    # Permanent retirement: a provider that fails this many times *consecutively* on a
    # hard kind (HTTP block / rate-limit / timeout) is a dead endpoint (deprecated host,
    # permanent geo-block, moved behind auth) — not a transient blip. It is retired for the
    # process lifetime instead of being re-probed on the cooldown forever (prod logs showed
    # eth.llamarpc.com at fail_streak 3600+ and rpc.flashbots.net still being re-probed 300×
    # over 44h). 50 is far above rpc_provider_fail_threshold (5): reaching it means the node
    # never once succeeded across ~50 cooldown cycles (hours), so even during a whole-network
    # RPC outage a genuinely usable node resets its streak long before retirement. 0 disables
    # permanent retirement (streak-based) entirely. Retired providers reset on restart.
    rpc_provider_permanent_fail_threshold: int = 50
    # Hedged failover (§2.4). A single rpc_call fans out to `rpc_hedge_factor` healthy
    # providers at once (racing; first good result wins, the rest are cancelled) so one
    # slow/timing-out endpoint cannot set the call's latency floor. It tries at most
    # `rpc_max_providers_per_call` providers before giving up, bounding the worst case to
    # ceil(max/hedge) x dex_rpc_timeout_sec regardless of how many providers are configured
    # — critical because the notification consumer shares this event loop, and with ~10
    # free ETH endpoints a strictly-sequential perbор could block it for tens of seconds
    # during a degradation onset (before failing providers accumulate enough strikes to be
    # cooled down and skipped). Hedge=2 keeps the extra request volume on rate-limited free
    # nodes modest; a pair of reliable paid endpoints makes the fastest win almost always.
    rpc_hedge_factor: int = 2
    rpc_max_providers_per_call: int = 6
    # A provider whose EWMA latency exceeds this is de-prioritized (sunk below faster
    # peers) but NOT disabled — it stays as a last-resort backup so the network never goes
    # dark just because every node is slow. Prod logs showed public nodes at 2-7s; a fast
    # paid endpoint should always outrank them. 0 disables the extra penalty.
    rpc_max_healthy_latency_ms: float = 2500.0

    # ── Health / staleness (§2.2, §4.4, §16) ──
    stale_cex_ws_sec: float = 15.0
    stale_cex_rest_sec: float = 60.0
    # DEX pools are polled per cycle over unauthenticated public RPC; one slow cycle
    # must not flip a venue to Maintenance. 30s caused observed DEX flapping — 60s
    # (≈ several poll cycles) eliminated it while still catching a truly dead feed.
    # Past this the venue goes DEGRADED (data flagged stale but venue kept in the active
    # pool with reduced confidence), not straight to Maintenance.
    stale_dex_rpc_sec: float = 60.0
    # A DEX venue that stays stale this long (no fresh pool read) has a genuinely dead feed,
    # not a slow cycle — escalate DEGRADED -> Maintenance and drop it from signal generation.
    # 240s ≈ 4 min ≈ several failed poll cycles beyond the DEGRADED grace window.
    stale_dex_offline_sec: float = 240.0
    # A whole EVM network with zero healthy RPC providers for longer than this is a
    # sustained outage (all pools failing at once — the observed `rpc_all_providers_failed`
    # storms). Surface it as an escalated `rpc_network_down` event for external alerting.
    rpc_network_down_alert_sec: float = 60.0
    active_healthcheck_interval_sec: float = 10.0
    health_recovery_consecutive: int = 3       # §14.2 N consecutive
    max_age_cex_price_sec: float = 10.0        # §16
    max_age_dex_price_sec: float = 26.0        # ~2x block interval buffer
    max_age_orderbook_cex_sec: float = 15.0
    max_age_funding_sec: float = 120.0
    # Rolling funding-rate samples kept per (venue, base) for the funding confidence
    # stability/volatility factor (§11.5).
    funding_history_window: int = 20
    # Minimum annualized funding-rate differential for a funding candidate to be emitted
    # (noise floor — below this the delta-neutral carry cannot clear entry+exit fees at
    # the default size/hold). 0.05 = 5% annualized. Tunable by operators without touching
    # code; users still apply their own per-alert min_profit filter on top.
    funding_min_annualized_spread: float = 0.05

    # ── Scheduling (§1.1, §1.6, §16) ──
    reconciliation_interval_sec: float = 1.0   # safety-net max every 1s
    detection_budget_ms: float = 500.0         # §16
    signal_gen_budget_ms: float = 200.0        # §16
    priority2_batch_window_ms: float = 50.0    # §1.6 micro-batch

    # ── Outlier detection (§4.6) ──
    outlier_window_ticks: int = 20
    outlier_mad_k: float = 5.0
    cross_venue_deviation_pct: float = 10.0

    # ── Ranking weights (§11.2) ──
    w_profit: float = 0.40
    w_liquidity: float = 0.25
    w_confidence: float = 0.20
    w_risk: float = 0.15

    # ── Confidence weights (§11.5) ──
    f_price_freshness: float = 0.20
    f_liquidity: float = 0.20
    f_spread_stability: float = 0.15
    f_exchange_health: float = 0.15
    f_historical_reliability: float = 0.15
    f_data_completeness: float = 0.15

    # ── Liquidity score weights (§9.4) ──
    lw_depth: float = 0.60
    lw_balance: float = 0.20
    lw_stability: float = 0.20

    # Priority 1 majors (§1.6)
    priority1_assets: tuple[str, ...] = ("BTC", "ETH", "SOL")

    def ttl_for(self, arb_type: str) -> int:
        return self.ttl_by_type.get(arb_type, 120)

    def cooldown_for(self, arb_type: str) -> int:
        return self.cooldown_by_type.get(arb_type, 60)

    def max_slippage_pct(self, venue_type: str) -> float:
        return self.max_slippage_dex_pct if venue_type == "DEX" else self.max_slippage_cex_pct

    def min_liquidity_usd(self, venue_type: str) -> float:
        return self.min_liquidity_dex_usd if venue_type == "DEX" else self.min_liquidity_cex_usd


# ── Validation ranges (§20.3): param -> (min, max) ──
_RANGES: dict[str, tuple[float, float]] = {
    "min_net_profit_usd": (0.0, 1e6),
    "min_roi_pct": (0.0, 100.0),
    "max_gas_pct_of_gross": (0.0, 100.0),
    "max_slippage_cex_pct": (0.0, 100.0),
    "max_slippage_dex_pct": (0.0, 100.0),
    "min_liquidity_cex_usd": (0.0, 1e9),
    "min_liquidity_dex_usd": (0.0, 1e9),
    "liquidity_score_floor": (0.0, 100.0),
    "confidence_threshold": (0.0, 100.0),
    "rank_top_min": (0.0, 100.0),
    "rank_high_min": (0.0, 100.0),
    "rank_medium_min": (0.0, 100.0),
    "significant_profit_change_pct": (0.0, 1000.0),
    "warmup_samples": (1, 100),
    "reconciliation_interval_sec": (0.1, 60.0),
    "max_bridge_time_sec": (1, 86400),
    "fast_bridge_threshold_sec": (1, 86400),
}


def validate_config(cfg: ScannerConfig) -> None:
    """Range + cross-parameter invariant checks (§20.3). Raises ConfigError."""
    for name, (lo, hi) in _RANGES.items():
        val = getattr(cfg, name)
        if not (lo <= val <= hi):
            raise ConfigError(f"{name}={val} out of range [{lo}, {hi}]")

    if cfg.min_net_profit_usd < 0:
        raise ConfigError("min_net_profit_usd cannot be negative")

    # Cooldown cannot exceed Max Signal Age for the same type (§20.3).
    for arb_type, cooldown in cfg.cooldown_by_type.items():
        ttl = cfg.ttl_by_type.get(arb_type, 0)
        if cooldown and ttl and cooldown > ttl:
            raise ConfigError(
                f"cooldown_by_type[{arb_type}]={cooldown} exceeds ttl {ttl} (§20.3)"
            )

    # DEX slippage below CEX requires explicit operator override (§20.3).
    if cfg.max_slippage_dex_pct < cfg.max_slippage_cex_pct:
        raise ConfigError(
            "max_slippage_dex_pct below CEX value needs explicit override (§20.3)"
        )

    # Rank thresholds must be monotonically ordered.
    if not (cfg.rank_top_min >= cfg.rank_high_min >= cfg.rank_medium_min):
        raise ConfigError("ranking thresholds must satisfy TOP >= HIGH >= MEDIUM")

    # DEX offline escalation must be no sooner than the DEGRADED (stale) threshold, else a
    # venue would jump ONLINE -> Maintenance with no DEGRADED grace window at all.
    if cfg.stale_dex_offline_sec < cfg.stale_dex_rpc_sec:
        raise ConfigError(
            "stale_dex_offline_sec must be >= stale_dex_rpc_sec (DEGRADED before Maintenance)"
        )

    # Ranking / confidence / liquidity weights must sum ~1.0.
    for label, weights in (
        ("ranking", (cfg.w_profit, cfg.w_liquidity, cfg.w_confidence, cfg.w_risk)),
        ("confidence", (cfg.f_price_freshness, cfg.f_liquidity, cfg.f_spread_stability,
                        cfg.f_exchange_health, cfg.f_historical_reliability,
                        cfg.f_data_completeness)),
        ("liquidity", (cfg.lw_depth, cfg.lw_balance, cfg.lw_stability)),
    ):
        total = sum(weights)
        if abs(total - 1.0) > 1e-6:
            raise ConfigError(f"{label} weights must sum to 1.0, got {total}")


class ConfigManager:
    """Owns the effective ScannerConfig, layering and hot-reload (§20.4/§20.6)."""

    def __init__(self, base: ScannerConfig | None = None) -> None:
        self._base = base or ScannerConfig()
        self._env_layer: dict[str, Any] = {}
        self._runtime_layer: dict[str, Any] = {}
        self._effective = self._compose()
        validate_config(self._effective)
        self._subscribers: list[Callable[[ScannerConfig], None]] = []
        self._source: dict[str, str] = {}
        self._recompute_sources()

    def _compose(self) -> ScannerConfig:
        cfg = copy.deepcopy(self._base)
        for layer in (self._env_layer, self._runtime_layer):
            for key, value in layer.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        return cfg

    def _recompute_sources(self) -> None:
        self._source = {f.name: "base" for f in fields(ScannerConfig)}
        for key in self._env_layer:
            self._source[key] = "environment"
        for key in self._runtime_layer:
            self._source[key] = "runtime"

    @property
    def config(self) -> ScannerConfig:
        return self._effective

    def apply_environment_layer(self, overrides: dict[str, Any]) -> None:
        """Set the environment-specific layer (§20.6). All-or-nothing."""
        candidate = copy.deepcopy(self)
        candidate._env_layer = {**self._env_layer, **overrides}
        candidate._effective = candidate._compose()
        validate_config(candidate._effective)  # raises before we mutate self
        self._env_layer.update(overrides)
        self._reload()

    def apply_runtime_override(self, overrides: dict[str, Any]) -> ScannerConfig:
        """Hot-reload path (§20.4). Validates first; applies atomically or not at all."""
        merged_runtime = {**self._runtime_layer, **overrides}
        trial_base = copy.deepcopy(self._base)
        for layer in (self._env_layer, merged_runtime):
            for k, v in layer.items():
                if hasattr(trial_base, k):
                    setattr(trial_base, k, v)
        validate_config(trial_base)
        self._runtime_layer = merged_runtime
        self._reload()
        return self._effective

    def _reload(self) -> None:
        self._effective = self._compose()
        self._recompute_sources()
        for callback in self._subscribers:
            callback(self._effective)

    def subscribe(self, callback: Callable[[ScannerConfig], None]) -> None:
        self._subscribers.append(callback)

    def introspect(self, name: str) -> tuple[Any, str]:
        """Return (effective value, originating layer) for a parameter (§20.6)."""
        return getattr(self._effective, name), self._source.get(name, "base")
