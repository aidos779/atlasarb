"""Signal Lifecycle Manager (Scanner §12): Created→Active→Updated→Expired→Archived.

Owns the active working set keyed by dedup key. Decides new-vs-update (§12.3),
computes TTL/expiry (§12.4), archives to history within 1 tick of expiry (§12.6),
and keeps the active set free of expired entries so dedup lookups stay correct.
"""
from __future__ import annotations

import time
from decimal import Decimal

from src.config import get_logger
from src.config.scanner_config import ScannerConfig
from src.domain.enums import ArbitrageType, ExpiryReason, SignalStatus
from src.domain.ports import NotificationQueue, SignalHistoryStore
from src.domain.signal import Signal
from src.scanner.lifecycle.cooldown import CooldownStore

log = get_logger("scanner.lifecycle")


class LifecycleManager:
    def __init__(
        self, config: ScannerConfig, cooldown: CooldownStore,
        queue: NotificationQueue, history: SignalHistoryStore,
    ) -> None:
        self._config = config
        self._cooldown = cooldown
        self._queue = queue
        self._history = history
        self._active: dict[tuple, Signal] = {}

    def update_config(self, config: ScannerConfig) -> None:
        self._config = config

    def active_signals(self) -> list[Signal]:
        return list(self._active.values())

    def active_route_keys(self):
        """Live keys-view of the active-signal dedup keys (read-only). Used by the detector
        economic floor to exempt active routes so §12.4 spread-collapse expiry is preserved.
        A view, not a copy: it reflects admits/expiries without per-tick rebuilding."""
        return self._active.keys()

    def get_active(self, key: tuple) -> Signal | None:
        return self._active.get(key)

    def set_ttl(self, signal: Signal) -> None:
        ttl = self._config.ttl_for(signal.arb_type.value)
        if signal.arb_type == ArbitrageType.FUNDING and signal.funding_next_time:
            signal.expires_at = signal.funding_next_time
        else:
            signal.expires_at = signal.timestamp + ttl

    async def admit(self, signal: Signal) -> tuple[bool, str]:
        """Admit a validated signal. Returns (published, event) where event is
        'new' | 'update' | 'suppressed'. Implements §12.2/§12.3 + §13.1/§13.3.
        """
        key = signal.dedup_key()
        now = time.time()
        existing = self._active.get(key)

        if existing is not None:
            # §13.1 — same key as an Active signal is an update, never a duplicate.
            significant = (
                self._cooldown.significant_change(existing.net_profit_usd, signal.net_profit_usd)
                or signal.ranking != existing.ranking
            )
            # Signal ids are already deterministic from the route (Signal.route_id), so
            # existing.id == signal.id here — but keep the original timestamp so age/TTL
            # continuity is preserved across updates.
            signal.timestamp = existing.timestamp
            signal.last_updated = now
            self.set_ttl(signal)
            self._active[key] = signal
            if significant:
                self._log_published(signal, "update")
                await self._queue.publish(signal, "update")
                return True, "update"
            return False, "suppressed"  # sub-threshold fluctuation absorbed (§12.3)

        # New key. If in post-expiry cooldown, suppress notification but track (§13.2).
        if self._cooldown.in_cooldown(key, now):
            self.set_ttl(signal)
            self._active[key] = signal
            return False, "suppressed"

        self.set_ttl(signal)
        signal.status = SignalStatus.ACTIVE
        self._active[key] = signal
        self._log_published(signal, "new")
        await self._queue.publish(signal, "new")
        return True, "new"

    def _log_published(self, signal: Signal, event: str) -> None:
        log.info(
            "signal_published", admit_event=event, id=signal.id,
            arb_type=signal.arb_type.value, coin=signal.coin,
            pair=signal.trading_pair,
            buy_exchange=signal.buy_exchange, sell_exchange=signal.sell_exchange,
            buy_price=float(signal.buy_price), sell_price=float(signal.sell_price),
            spread_pct=float(round(signal.spread_pct, 4)),
            net_profit_pct=float(round(signal.net_profit_pct, 4)),
            net_profit_usd=float(round(signal.net_profit_usd, 2)),
            size_usd=float(round(signal.recommended_trade_size_usd, 2)),
            liquidity_usd=float(round(signal.liquidity_usd, 2)),
            confidence=signal.confidence_score, ranking=signal.ranking.value,
        )

    async def expire(self, key: tuple, reason: ExpiryReason, now: float | None = None) -> None:
        signal = self._active.pop(key, None)
        if signal is None:
            return
        now = now or time.time()
        signal.status = SignalStatus.EXPIRED
        signal.expired_at = now
        signal.expiry_reason = reason.value
        signal.signal_lifetime_sec = int(now - signal.timestamp)
        self._cooldown.on_expired(key, signal.arb_type, now)
        await self._history.archive(signal)  # §12.6 within 1 tick
        log.debug("signal_expired", id=signal.id, reason=reason.value)

    async def sweep_expired(self, now: float | None = None) -> int:
        """Age-based expiry pass (§12.4). Called every reconciliation tick."""
        now = now or time.time()
        expired = 0
        for key, signal in list(self._active.items()):
            if now >= signal.expires_at:
                await self.expire(key, ExpiryReason.TTL_EXCEEDED, now)
                expired += 1
        return expired

    async def force_expire_venue(self, venue: str, now: float | None = None) -> int:
        """§14.3 / BR-EXST-1 — expire all signals touching an offline venue."""
        count = 0
        for key, signal in list(self._active.items()):
            if venue in (signal.buy_exchange, signal.sell_exchange):
                await self.expire(key, ExpiryReason.VENUE_OFFLINE, now)
                count += 1
        return count

    async def force_expire_delisted(self, pair: str, now: float | None = None) -> int:
        """§3.2 — force-expire open signals referencing a delisted pair."""
        count = 0
        for key, signal in list(self._active.items()):
            if signal.trading_pair == pair:
                await self.expire(key, ExpiryReason.MARKET_DELISTED, now)
                count += 1
        return count

    async def close_if_spread_gone(self, key: tuple, new_net_usd: Decimal) -> bool:
        """§12.4 — immediate expiry when netProfit drops <= 0."""
        if new_net_usd <= 0 and key in self._active:
            await self.expire(key, ExpiryReason.SPREAD_CLOSED)
            return True
        return False
