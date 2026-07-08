"""Live signal registry — the in-memory working set the bot reads from.

Fed by the engine bridge on new/update/expire. Serves the Signal List (§9), Signal
Details (§10), Search filter application (§11), and Related Signals (§10.8). Keeps a
short buffer of recently-expired signals so a Details tap racing expiry can still render
the last snapshot (BR-DETAILS-4).
"""
from __future__ import annotations

import time
from collections import OrderedDict

from src.domain.enums import RankingLevel, SignalStatus
from src.domain.signal import Signal
from src.domain.user import UserFilter

_RECENT_EXPIRED_MAX = 500
_SORT_KEYS = {
    "profit": lambda s: s.net_profit_pct,
    "spread": lambda s: s.spread_pct,
    "liquidity": lambda s: s.liquidity_usd,
    "risk": lambda s: -s.risk_score.numeric,
    "age": lambda s: -s.timestamp,
}


class SignalRegistry:
    def __init__(self) -> None:
        self._active: dict[str, Signal] = {}
        self._recent_expired: OrderedDict[str, Signal] = OrderedDict()

    # ── mutations (from engine bridge) ──
    def upsert(self, signal: Signal) -> None:
        self._active[signal.id] = signal

    def expire(self, signal: Signal) -> None:
        self._active.pop(signal.id, None)
        signal.status = SignalStatus.EXPIRED
        self._recent_expired[signal.id] = signal
        while len(self._recent_expired) > _RECENT_EXPIRED_MAX:
            self._recent_expired.popitem(last=False)

    # ── reads ──
    def get(self, signal_id: str) -> Signal | None:
        return self._active.get(signal_id) or self._recent_expired.get(signal_id)

    def all_active(self) -> list[Signal]:
        return list(self._active.values())

    def query(
        self, user_filter: UserFilter, sort: str = "profit", descending: bool = True,
        allowed_types: frozenset | None = None, delay_sec: int = 0,
        now: float | None = None,
    ) -> list[Signal]:
        """Filtered, tier-delayed, sorted active signals (Signal List §9)."""
        now = now or time.time()
        out = []
        for s in self._active.values():
            if delay_sec and (now - s.timestamp) < delay_sec:
                continue  # BR-SIG-1 free-tier delay
            if allowed_types is not None and s.arb_type not in allowed_types:
                continue  # tier arb-type gate (§15.2)
            if not user_filter.matches(s):
                continue
            out.append(s)
        key = _SORT_KEYS.get(sort, _SORT_KEYS["profit"])
        out.sort(key=key, reverse=descending)
        return out

    def related(self, signal: Signal, limit: int = 3) -> list[Signal]:
        """Up to `limit` related signals (§10.8): same coin other venues / same venues."""
        out = []
        for s in self._active.values():
            if s.id == signal.id:
                continue
            same_coin = s.coin == signal.coin
            same_venues = {s.buy_exchange, s.sell_exchange} & {
                signal.buy_exchange, signal.sell_exchange}
            if same_coin or same_venues:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def top_ranked(self) -> list[Signal]:
        return sorted(
            (s for s in self._active.values() if s.ranking == RankingLevel.TOP),
            key=lambda s: s.net_profit_pct, reverse=True,
        )

    def count(self) -> int:
        return len(self._active)
