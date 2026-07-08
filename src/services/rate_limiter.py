"""In-memory per-user rate limiters (PRD BR-SEARCH-2 20/min, BR-SIG-4 refresh 1/3s).

Sliding-window counters keyed by user_id. For multi-instance deployment these move to
Redis (documented future extension); the interface stays identical.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_sec: float) -> None:
        self._max = max_events
        self._window = window_sec
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._events[user_id]
        cutoff = now - self._window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self._max:
            return False
        q.append(now)
        return True

    def retry_after(self, user_id: int, now: float | None = None) -> float:
        now = now or time.time()
        q = self._events[user_id]
        if not q:
            return 0.0
        return max(0.0, self._window - (now - q[0]))
