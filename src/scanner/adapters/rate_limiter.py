"""Token-bucket rate limiter per venue (Scanner §2.6).

Adapters degrade gracefully (await capacity) rather than exceeding published limits.
Configurable refill rate and burst capacity.
"""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self._rate = rate_per_sec
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                await asyncio.sleep(deficit / self._rate)

    def try_acquire(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False
