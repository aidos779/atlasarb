"""Reconnect backoff (Scanner §2.3): exponential with jitter.

delay = min(base * multiplier^attempt, cap) * (1 ± jitter). After the fast-retry budget
is exhausted the caller marks the venue OFFLINE and switches to a fixed slow-retry loop.
"""
from __future__ import annotations

import random


def backoff_delay(attempt: int, base: float, multiplier: float, cap: float,
                  jitter: float) -> float:
    raw = min(base * (multiplier ** attempt), cap)
    factor = 1.0 + random.uniform(-jitter, jitter)
    return max(0.0, raw * factor)
