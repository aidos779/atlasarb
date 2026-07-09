"""Reconnect backoff (Scanner §2.3): exponential with jitter.

``raw = min(base * multiplier^attempt, cap)`` is the exponential ceiling; ``jitter`` is
the *fraction* of that ceiling that is randomized (0 = fixed, 1 = full jitter). The delay
is drawn from ``[raw*(1-jitter), raw]``.

The old formula used a symmetric ±jitter band (≈0.8–1.2× raw). With many shards failing
on the same upstream drop at the same attempt count, that band was too narrow to break
them apart — every shard reconnected inside the same ~2s window, which is the reconnect
storm seen in the logs. Full (or near-full) jitter spreads reconnects across the whole
`[0, raw]` interval so the shards de-synchronize. After the fast-retry budget is exhausted
the caller marks the venue OFFLINE and switches to a fixed slow-retry loop.
"""
from __future__ import annotations

import random


def backoff_delay(attempt: int, base: float, multiplier: float, cap: float,
                  jitter: float) -> float:
    raw = min(base * (multiplier ** attempt), cap)
    if raw <= 0:
        return 0.0
    jitter = min(max(jitter, 0.0), 1.0)
    rand_span = raw * jitter
    fixed = raw - rand_span
    return fixed + random.uniform(0.0, rand_span)
