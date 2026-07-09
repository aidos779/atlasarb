"""Health-scored JSON-RPC provider pool (Scanner §2.4 failover).

Turns a flat list of RPC endpoints into a self-healing rotation:

  - every provider carries a health score (EWMA of success) + failure streak;
  - a provider that fails ``fail_threshold`` times in a row is temporarily
    disabled with an exponentially growing cooldown (base → cap);
  - disabled providers are automatically re-probed once their cooldown elapses;
  - callers iterate ``order()``, which yields healthy providers first (round-robin
    so load spreads across them) and only then cooldown-expired probes;
  - failures are classified (HTTP / TIMEOUT / RATE_LIMIT / RPC_ERROR) so the health
    log names the real cause and rate-limits/blocks back off harder than transient
    RPC errors;
  - health transitions (disabled / recovered) are logged.

Discovery therefore never stops while at least one provider is healthy, and a single
dead endpoint (the ``http 403`` in the logs) is dropped from rotation instead of being
retried at the same slot every cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from src.config import get_logger

log = get_logger("adapter.rpc")


class RpcErrorKind(str, Enum):
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
    fail_streak: int = 0
    disabled_until: float = 0.0
    cooldown: float = 0.0       # current cooldown length (grows on repeat failure)
    last_kind: RpcErrorKind | None = None

    def healthy(self, now: float) -> bool:
        return now >= self.disabled_until


@dataclass
class RpcProviderPool:
    urls: list[str]
    fail_threshold: int = 3
    cooldown_base_sec: float = 30.0
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
        """URLs to try this call: healthy ones first (round-robin rotated), then any
        cooldown-expired probes. Never empty while ``urls`` is non-empty — if every
        provider is still cooling down, the one nearest recovery is returned as a
        single probe so discovery can always make progress."""
        if not self._providers:
            return []
        now = time.monotonic() if now is None else now
        n = len(self._providers)
        # Round-robin starting point so load rotates across healthy providers.
        rotated = [self._providers[(self._cursor + i) % n] for i in range(n)]
        self._cursor = (self._cursor + 1) % n
        healthy = [p for p in rotated if p.healthy(now)]
        # Prefer higher score among the healthy (stable sort keeps round-robin order
        # among equals), so a flaky-but-enabled endpoint sinks below solid ones.
        healthy.sort(key=lambda p: p.score, reverse=True)
        if healthy:
            return [p.url for p in healthy]
        # All disabled → probe the one closest to recovery so we never block.
        soonest = min(self._providers, key=lambda p: p.disabled_until)
        return [soonest.url]

    def record_success(self, url: str) -> None:
        p = self._by_url(url)
        if p is None:
            return
        was_disabled = p.disabled_until > 0.0 or p.fail_streak >= self.fail_threshold
        p.score = min(1.0, p.score * 0.7 + 0.3)  # EWMA toward 1
        p.fail_streak = 0
        p.cooldown = 0.0
        p.disabled_until = 0.0
        p.last_kind = None
        if was_disabled:
            log.info("rpc_provider_recovered", network=self.network, url=url,
                     score=round(p.score, 3))

    def record_failure(self, url: str, kind: RpcErrorKind) -> None:
        p = self._by_url(url)
        if p is None:
            return
        now = time.monotonic()
        p.score = max(0.0, p.score * 0.7)  # EWMA toward 0
        p.fail_streak += 1
        p.last_kind = kind
        if p.fail_streak >= self.fail_threshold and p.disabled_until <= now:
            # Exponential cooldown growth, capped. Hard kinds (block/throttle/timeout)
            # start at the full base; a plain RPC error at half, so a momentarily
            # buggy node recovers faster than a genuinely blocked host.
            start = self.cooldown_base_sec if kind in _HARD_KINDS else self.cooldown_base_sec / 2
            p.cooldown = min(self.cooldown_max_sec,
                             start if p.cooldown == 0.0 else p.cooldown * 2)
            p.disabled_until = now + p.cooldown
            log.warning("rpc_provider_disabled", network=self.network, url=url,
                        kind=kind.value, fail_streak=p.fail_streak,
                        cooldown_sec=round(p.cooldown, 1), score=round(p.score, 3))

    def snapshot(self) -> list[dict]:
        """Per-provider health for the exhaustion log / diagnostics."""
        now = time.monotonic()
        return [
            {"url": p.url, "score": round(p.score, 3), "fail_streak": p.fail_streak,
             "disabled": not p.healthy(now),
             "last_kind": p.last_kind.value if p.last_kind else None}
            for p in self._providers
        ]

    def _by_url(self, url: str) -> _Provider | None:
        for p in self._providers:
            if p.url == url:
                return p
        return None
