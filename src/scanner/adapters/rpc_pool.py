"""Health-scored JSON-RPC provider pool (Scanner §2.4 failover).

Turns a flat list of RPC endpoints into a self-healing, latency-aware rotation:

  - every provider tracks an EWMA success rate, an EWMA latency, an EWMA timeout
    rate, a consecutive-failure streak, and time-of-last-success/failure;
  - selection is a **circuit breaker**: CLOSED (healthy, in rotation), OPEN
    (tripped after ``fail_threshold`` consecutive failures, cooling down), or
    HALF_OPEN (cooldown elapsed, offered as a single probe). A success closes it;
  - among CLOSED providers, ranking is **adaptive and latency-relative**: the score
    weighs success rate, latency measured against the pool's current best, recent
    stability and recency — so traffic converges onto the fastest healthy endpoint
    while round-robin among near-equals prevents starvation;
  - failures are classified (UNAUTHORIZED / HTTP / RATE_LIMIT / TIMEOUT / RPC_ERROR).
    A permanent auth failure (UNAUTHORIZED — e.g. a public Ankr endpoint that now
    needs a key) is retired for the process lifetime instead of being retried every
    cycle; transient kinds back off with an exponential, capped cooldown;
  - disable/recover transitions are logged (disables throttled per provider).

Discovery never stops while one provider is CLOSED, and a permanently broken endpoint
is retired rather than burning a retry every cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from src.config import LogThrottle, get_logger

log = get_logger("adapter.rpc")

_disable_log_throttle = LogThrottle(interval_sec=120.0)

# Effectively "forever" for the process — a permanently unauthorized endpoint cannot
# recover without a configuration change, so it is retired rather than re-probed.
_PERMANENT_COOLDOWN_SEC = 10 * 365 * 24 * 3600.0


class RpcErrorKind(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"  # 401 / auth-required (permanent without a key)
    HTTP = "HTTP"                  # other non-200 (e.g. 403 block / 5xx)
    RATE_LIMIT = "RATE_LIMIT"      # 429 / -32005 etc.
    TIMEOUT = "TIMEOUT"            # request timed out
    RPC_ERROR = "RPC_ERROR"        # 200 with JSON error or null result


class BreakerState(StrEnum):
    CLOSED = "closed"        # healthy, in rotation
    OPEN = "open"            # tripped, cooling down
    HALF_OPEN = "half_open"  # cooldown elapsed, offered as a probe


# Failure kinds that indicate the endpoint itself is bad for us (block / throttle)
# get the full cooldown; a transient RPC error backs off more gently.
_HARD_KINDS = frozenset({RpcErrorKind.HTTP, RpcErrorKind.RATE_LIMIT, RpcErrorKind.TIMEOUT})

# Multiplier on ``fail_threshold`` for a streak made up purely of timeouts. A timeout on
# a public node is very often a latency spike, not a dead endpoint (prod EWMA latencies
# of 2-7s sit right at the 5s request deadline), and the hedged fan-out already shields
# call latency from a slow node — so tripping the breaker as fast as for a hard 403/429
# block bought nothing and produced the disabled→TIMEOUT→recovered flapping seen in
# production. A definitive signal (any HTTP block / rate-limit / auth failure in the
# streak) still trips at the base threshold.
_TIMEOUT_STREAK_MULTIPLIER = 2


@dataclass
class _Provider:
    url: str
    tier: int = 1               # 0 = paid/primary (preferred), 1 = public fallback
    score: float = 1.0          # EWMA success rate in [0, 1]
    latency_ms: float = 0.0     # EWMA of successful-call latency
    timeout_rate: float = 0.0   # EWMA of "was this failure a timeout?" in [0, 1]
    fail_streak: int = 0
    # True once the current consecutive-failure streak contains any non-timeout failure —
    # a pure-timeout streak trips the breaker later (see _TIMEOUT_STREAK_MULTIPLIER).
    streak_has_nontimeout: bool = False
    disabled_until: float = 0.0
    cooldown: float = 0.0       # current cooldown length (grows on repeat failure)
    permanent: bool = False     # retired (e.g. unauthorized) — never re-probed
    last_kind: RpcErrorKind | None = None
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    def healthy(self, now: float) -> bool:
        return not self.permanent and now >= self.disabled_until

    def state(self, now: float) -> BreakerState:
        # OPEN: retired, or still inside the cooldown window.
        if self.permanent or now < self.disabled_until:
            return BreakerState.OPEN
        # Past the cooldown. ``disabled_until`` is only cleared to 0 by a success, so a
        # non-zero value we have already passed means the breaker tripped and is being
        # re-probed but has not yet been re-proven → HALF_OPEN. Zero means it was never
        # tripped (or a success has since fully closed it) → CLOSED.
        if self.disabled_until > 0:
            return BreakerState.HALF_OPEN
        return BreakerState.CLOSED

    def rank(self, now: float, best_latency_ms: float, slow_latency_ms: float = 0.0) -> float:
        """Adaptive health rank (higher = healthier), latency-relative.

        Success rate dominates; latency is penalised **relative to the pool's current
        best** so the ranking converges onto the fastest healthy endpoint regardless
        of absolute network speed. Recent timeouts, recency of the last good response
        and stability since the last failure refine the ordering. A provider slower than
        ``slow_latency_ms`` takes an extra fixed penalty so a genuinely slow node sinks
        below faster peers (but stays in rotation as a last resort).
        """
        r = self.score
        # Latency, relative to the fastest healthy provider (0 penalty at parity,
        # up to −0.35 when this provider is >=3x slower than the best).
        if self.latency_ms > 0 and best_latency_ms > 0:
            ratio = self.latency_ms / best_latency_ms
            r -= min((ratio - 1.0) / 2.0, 1.0) * 0.35 if ratio > 1.0 else 0.0
        # Absolute slow-node floor: past the configured ceiling, take a fixed −0.5 hit so
        # any provider under the ceiling outranks it regardless of relative comparison.
        if slow_latency_ms > 0 and self.latency_ms > slow_latency_ms:
            r -= 0.5
        # Timeout aversion: a provider that times out is worse than one that errors
        # fast, because a timeout also costs us the full request budget.
        r -= self.timeout_rate * 0.20
        # Recency: answered recently beats an unknown-state provider.
        if self.last_success_at and now - self.last_success_at <= 60.0:
            r += 0.15
        # Stability: no failures for 10+ minutes earns the full bonus.
        if self.last_failure_at:
            r += min((now - self.last_failure_at) / 600.0, 1.0) * 0.15
        else:
            r += 0.15
        return r


@dataclass
class RpcProviderPool:
    urls: list[str]
    fail_threshold: int = 5
    cooldown_base_sec: float = 20.0
    cooldown_max_sec: float = 300.0
    network: str = ""
    slow_latency_ms: float = 0.0    # de-prioritize (don't disable) nodes slower than this
    # Consecutive hard failures before a provider is retired permanently (0 = never).
    permanent_fail_threshold: int = 0
    # URLs to treat as the paid/primary tier (preferred over public fallback nodes).
    primary_urls: set[str] = field(default_factory=set)
    _providers: list[_Provider] = field(default_factory=list)
    _cursor: int = 0

    def __post_init__(self) -> None:
        self._providers = [
            _Provider(u, tier=0 if u in self.primary_urls else 1) for u in self.urls
        ]

    def update_config(self, fail_threshold: int, cooldown_base: float,
                      cooldown_max: float, slow_latency_ms: float | None = None,
                      permanent_fail_threshold: int | None = None) -> None:
        self.fail_threshold = fail_threshold
        self.cooldown_base_sec = cooldown_base
        self.cooldown_max_sec = cooldown_max
        if slow_latency_ms is not None:
            self.slow_latency_ms = slow_latency_ms
        if permanent_fail_threshold is not None:
            self.permanent_fail_threshold = permanent_fail_threshold

    def order(self, now: float | None = None) -> list[str]:
        """URLs to try this call, best-first.

        In-rotation providers (CLOSED, plus HALF_OPEN ones whose cooldown has elapsed and
        are being re-probed) come first, ranked healthiest (fastest) first with round-robin
        among near-equals so no single provider is starved. If none are in rotation, a
        single probe — the non-permanent OPEN provider nearest recovery — is returned so
        discovery can always make progress. Permanently retired providers are excluded.
        """
        if not self._providers:
            return []
        now = time.monotonic() if now is None else now
        n = len(self._providers)
        # Round-robin starting point so load rotates across equally-ranked providers.
        rotated = [self._providers[(self._cursor + i) % n] for i in range(n)]
        self._cursor = (self._cursor + 1) % n
        healthy = [p for p in rotated if p.healthy(now)]
        if healthy:
            best_latency = min((p.latency_ms for p in healthy if p.latency_ms > 0),
                               default=0.0)
            # Tier first (all healthy paid primaries before any public fallback), then a
            # coarse rank bucket keeps round-robin spread among near-equal providers while a
            # slower/unhealthier endpoint sinks below solid ones within the same tier. A
            # primary is only skipped when it is unhealthy — then it is simply absent here
            # and the fallbacks take over automatically.
            healthy.sort(key=lambda p: (
                p.tier, -round(p.rank(now, best_latency, self.slow_latency_ms), 1)))
            return [p.url for p in healthy]
        # All open → probe the non-permanent one closest to recovery so we never block,
        # preferring a primary at equal recovery time so paid capacity is re-tried first.
        probes = [p for p in self._providers if not p.permanent]
        if not probes:
            return []  # every provider permanently retired — nothing to try
        soonest = min(probes, key=lambda p: (p.disabled_until, p.tier))
        return [soonest.url]

    def ewma_latency_ms(self, url: str) -> float:
        """EWMA latency of one provider (0.0 if unknown) — lets the transport grant a
        known-slow-but-alive node a deadline matched to how it actually responds."""
        p = self._by_url(url)
        return p.latency_ms if p is not None else 0.0

    def record_success(self, url: str, latency_ms: float = 0.0) -> None:
        p = self._by_url(url)
        if p is None:
            return
        was_disabled = p.disabled_until > 0.0 or p.fail_streak >= self.fail_threshold
        p.score = min(1.0, p.score * 0.8 + 0.2)  # EWMA toward 1
        if latency_ms > 0:
            p.latency_ms = (latency_ms if p.latency_ms == 0.0
                            else p.latency_ms * 0.8 + latency_ms * 0.2)
        p.timeout_rate *= 0.8  # decay toward 0 on success
        p.fail_streak = 0
        p.streak_has_nontimeout = False
        p.cooldown = 0.0
        p.disabled_until = 0.0
        p.permanent = False
        p.last_kind = None
        p.last_success_at = time.monotonic()
        if was_disabled:
            log.info("rpc_provider_recovered", network=self.network, url=url,
                     score=round(p.score, 3), latency_ms=round(p.latency_ms, 1))

    def record_failure(self, url: str, kind: RpcErrorKind) -> None:
        p = self._by_url(url)
        if p is None:
            return
        now = time.monotonic()
        # Gentler EWMA decay than the disable gate: score reflects the *rate* of
        # failures for ranking; disabling is driven by the consecutive streak (or an
        # immediate permanent retirement for auth failures).
        p.score = max(0.0, p.score * 0.8)
        p.timeout_rate = p.timeout_rate * 0.8 + (0.2 if kind == RpcErrorKind.TIMEOUT else 0.0)
        p.fail_streak += 1
        if kind != RpcErrorKind.TIMEOUT:
            p.streak_has_nontimeout = True
        p.last_kind = kind
        p.last_failure_at = now

        # Permanent auth failure: retire immediately (one strike). Retrying a public
        # endpoint that now requires a key can never succeed without a config change.
        if kind == RpcErrorKind.UNAUTHORIZED:
            if not p.permanent:
                p.permanent = True
                p.cooldown = _PERMANENT_COOLDOWN_SEC
                p.disabled_until = now + _PERMANENT_COOLDOWN_SEC
                log.warning("rpc_provider_retired", network=self.network, url=url,
                            reason="unauthorized", detail="endpoint requires authentication")
            return

        # Permanent retirement of a persistently dead endpoint (§2.4): a long *consecutive*
        # streak of hard failures (HTTP block / rate-limit / timeout) is a node that will
        # not recover without a config change — retire it instead of re-probing every
        # cooldown cycle for the rest of the process (the eth.llamarpc.com / rpc.flashbots.net
        # case). Streak-based so a node that ever succeeds again resets and is spared.
        if (self.permanent_fail_threshold > 0 and kind in _HARD_KINDS
                and p.fail_streak >= self.permanent_fail_threshold and not p.permanent):
            p.permanent = True
            p.cooldown = _PERMANENT_COOLDOWN_SEC
            p.disabled_until = now + _PERMANENT_COOLDOWN_SEC
            log.warning("rpc_provider_retired", network=self.network, url=url,
                        reason="repeated_failures", kind=kind.value,
                        fail_streak=p.fail_streak,
                        detail="endpoint dead — retired for process lifetime")
            return

        # A pure-timeout streak is usually a latency spike on an otherwise-alive node
        # (ranking already sinks it via timeout_rate + the slow-latency penalty, and
        # hedging keeps calls fast) — require a longer streak before pulling it from
        # rotation. Any harder evidence in the streak keeps the fast threshold.
        threshold = (self.fail_threshold if p.streak_has_nontimeout
                     else self.fail_threshold * _TIMEOUT_STREAK_MULTIPLIER)
        if p.fail_streak >= threshold and p.disabled_until <= now:
            # Exponential cooldown growth, capped. Hard kinds (block/throttle/timeout)
            # start at the full base; a plain RPC error at half, so a momentarily
            # buggy node recovers faster than a genuinely blocked host.
            start = self.cooldown_base_sec if kind in _HARD_KINDS else self.cooldown_base_sec / 2
            p.cooldown = min(self.cooldown_max_sec,
                             start if p.cooldown == 0.0 else p.cooldown * 2)
            p.disabled_until = now + p.cooldown
            emit, suppressed = _disable_log_throttle.allow((self.network, url))
            if emit:
                log.warning("rpc_provider_disabled", network=self.network, url=url,
                            kind=kind.value, fail_streak=p.fail_streak,
                            cooldown_sec=round(p.cooldown, 1), score=round(p.score, 3),
                            timeout_rate=round(p.timeout_rate, 3),
                            repeats_suppressed=suppressed)

    def healthy_count(self, now: float | None = None) -> tuple[int, int]:
        """(healthy, total) — for the periodic engine health summary. Permanently
        retired providers are excluded from the total so the ratio reflects usable
        capacity, not dead configuration."""
        now = time.monotonic() if now is None else now
        usable = [p for p in self._providers if not p.permanent]
        return sum(1 for p in usable if p.healthy(now)), len(usable)

    def snapshot(self, now: float | None = None) -> list[dict]:
        """Per-provider health for the exhaustion log / diagnostics."""
        now = time.monotonic() if now is None else now
        return [
            {"url": p.url, "state": p.state(now).value, "score": round(p.score, 3),
             "latency_ms": round(p.latency_ms, 1), "timeout_rate": round(p.timeout_rate, 3),
             "fail_streak": p.fail_streak, "permanent": p.permanent,
             "last_kind": p.last_kind.value if p.last_kind else None}
            for p in self._providers
        ]

    def _by_url(self, url: str) -> _Provider | None:
        for p in self._providers:
            if p.url == url:
                return p
        return None
