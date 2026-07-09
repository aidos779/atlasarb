"""Health-scored JSON-RPC provider pool (Scanner §2.4 failover).

Turns a flat list of RPC endpoints into a self-healing rotation:

  - every provider carries an EWMA success rate, an EWMA latency, a failure streak,
    time-of-last-success and time-of-last-failure;
  - ranking combines success rate, latency, recent failures, recency of the last
    good response, and stability (time since the last failure) — so the healthiest
    provider is always tried first;
  - a provider that fails ``fail_threshold`` times *in a row* is temporarily
    disabled with an exponentially growing cooldown (base → cap); isolated or
    short-lived failures never disable a provider because any success resets the
    streak;
  - disabled providers are automatically re-probed once their cooldown elapses
    (half-open), and a single success fully restores them;
  - failures are classified (HTTP / TIMEOUT / RATE_LIMIT / RPC_ERROR) so the health
    log names the real cause and rate-limits/blocks back off harder than transient
    RPC errors;
  - health transitions (disabled / recovered) are logged; repeated disable warnings
    for the same provider are throttled.

Discovery therefore never stops while at least one provider is healthy, and a single
dead endpoint (the ``http 403`` in the logs) is dropped from rotation instead of being
retried at the same slot every cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from src.config import LogThrottle, get_logger

log = get_logger("adapter.rpc")

_disable_log_throttle = LogThrottle(interval_sec=120.0)


class RpcErrorKind(StrEnum):
    HTTP = "HTTP"            # non-200 (e.g. 403 block / 5xx)
    RATE_LIMIT = "RATE_LIMIT"  # 429 / -32005 etc.
    TIMEOUT = "TIMEOUT"     # request timed out
    RPC_ERROR = "RPC_ERROR"  # 200 with JSON error or null result


# Failure kinds that indicate the endpoint itself is bad for us (block / throttle)
# get the full cooldown; a transient RPC error backs off more gently.
_HARD_KINDS = frozenset({RpcErrorKind.HTTP, RpcErrorKind.RATE_LIMIT, RpcErrorKind.TIMEOUT})


@dataclass
class _Provider:
    url: str
    score: float = 1.0          # EWMA success rate in [0, 1]
    latency_ms: float = 0.0     # EWMA of successful-call latency
    fail_streak: int = 0
    disabled_until: float = 0.0
    cooldown: float = 0.0       # current cooldown length (grows on repeat failure)
    last_kind: RpcErrorKind | None = None
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    def healthy(self, now: float) -> bool:
        return now >= self.disabled_until

    def rank(self, now: float) -> float:
        """Composite health rank (higher = healthier). Weighs success rate most,
        then latency, recency of the last good response, and stability since the
        last failure — the §2.4 goal of always preferring the healthiest provider."""
        r = self.score
        # Latency penalty: 0 at 0ms → −0.3 at ≥1.5s.
        r -= min(self.latency_ms / 1500.0, 1.0) * 0.3
        # Recency: a provider that answered within the last minute is preferred over
        # one we have not heard from in a while (unknown state).
        if self.last_success_at:
            age = now - self.last_success_at
            r += 0.15 if age <= 60.0 else 0.0
        # Stability: no failures for 10+ minutes earns the full bonus, scaling down
        # to zero for a provider that just failed.
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
    _providers: list[_Provider] = field(default_factory=list)
    _cursor: int = 0

    def __post_init__(self) -> None:
        self._providers = [_Provider(u) for u in self.urls]

    def update_config(self, fail_threshold: int, cooldown_base: float,
                      cooldown_max: float) -> None:
        self.fail_threshold = fail_threshold
        self.cooldown_base_sec = cooldown_base
        self.cooldown_max_sec = cooldown_max

    def order(self, now: float | None = None) -> list[str]:
        """URLs to try this call: healthy ones first (ranked healthiest-first, with
        round-robin rotation as the tiebreak so load spreads across equally-healthy
        providers), then any cooldown-expired probes. Never empty while ``urls`` is
        non-empty — if every provider is still cooling down, the one nearest recovery
        is returned as a single probe so discovery can always make progress."""
        if not self._providers:
            return []
        now = time.monotonic() if now is None else now
        n = len(self._providers)
        # Round-robin starting point so load rotates across equally-ranked providers.
        rotated = [self._providers[(self._cursor + i) % n] for i in range(n)]
        self._cursor = (self._cursor + 1) % n
        healthy = [p for p in rotated if p.healthy(now)]
        # Rank buckets of 0.1 keep the round-robin spread among near-equal providers
        # while a genuinely unhealthier endpoint sinks below solid ones.
        healthy.sort(key=lambda p: round(p.rank(now), 1), reverse=True)
        if healthy:
            return [p.url for p in healthy]
        # All disabled → probe the one closest to recovery so we never block.
        soonest = min(self._providers, key=lambda p: p.disabled_until)
        return [soonest.url]

    def record_success(self, url: str, latency_ms: float = 0.0) -> None:
        p = self._by_url(url)
        if p is None:
            return
        was_disabled = p.disabled_until > 0.0 or p.fail_streak >= self.fail_threshold
        p.score = min(1.0, p.score * 0.8 + 0.2)  # EWMA toward 1
        if latency_ms > 0:
            p.latency_ms = (latency_ms if p.latency_ms == 0.0
                            else p.latency_ms * 0.8 + latency_ms * 0.2)
        p.fail_streak = 0
        p.cooldown = 0.0
        p.disabled_until = 0.0
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
        # failures for ranking; disabling is driven only by the consecutive streak,
        # so isolated failures reorder providers instead of removing them.
        p.score = max(0.0, p.score * 0.8)
        p.fail_streak += 1
        p.last_kind = kind
        p.last_failure_at = now
        if p.fail_streak >= self.fail_threshold and p.disabled_until <= now:
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
                            repeats_suppressed=suppressed)

    def healthy_count(self, now: float | None = None) -> tuple[int, int]:
        """(healthy, total) — for the periodic engine health summary."""
        now = time.monotonic() if now is None else now
        return sum(1 for p in self._providers if p.healthy(now)), len(self._providers)

    def snapshot(self) -> list[dict]:
        """Per-provider health for the exhaustion log / diagnostics."""
        now = time.monotonic()
        return [
            {"url": p.url, "score": round(p.score, 3),
             "latency_ms": round(p.latency_ms, 1), "fail_streak": p.fail_streak,
             "disabled": not p.healthy(now),
             "last_kind": p.last_kind.value if p.last_kind else None}
            for p in self._providers
        ]

    def _by_url(self, url: str) -> _Provider | None:
        for p in self._providers:
            if p.url == url:
                return p
        return None
