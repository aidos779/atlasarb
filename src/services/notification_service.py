"""Notification service (PRD §13, §18) — per-user Instant Alert eligibility + dispatch.

Consumes the engine bridge stream and, for each published signal, evaluates every
eligible user against: tier arb-type gate, saved Filters (§12.1), low-confidence gate
(R-SCHEMA-2), per-user hourly cap with throttle notice (§13.3/R-NOTIF-1), tier delay
(§13.3), per-user cooldown + significant-change override (§18/BR-COOL-2), mutes (§13.5),
and Favorite Coin/Exchange alerts that bypass general filters (§13.1, Basic+).

Delivery is via the injected Notifier port (the bot's send function) — Dependency
Inversion keeps this service independent of aiogram.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from src.config import get_logger
from src.database.base import Database
from src.database.repositories.favorites_repo import FavoritesRepository
from src.database.repositories.misc_repos import NotificationRepository
from src.database.repositories.user_repo import UserRepository
from src.domain.entitlements import UNLIMITED, entitlements_for
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.services.engine_bridge import EngineBridge

log = get_logger("services.notification")

_COOLDOWN_SEC = 60
_PROFIT_OVERRIDE_PP = Decimal("0.5")  # §18.3 ±0.5 percentage points


class Notifier(Protocol):
    async def send_alert(self, user_id: int, signal: Signal, language: str) -> bool: ...
    async def send_text(self, user_id: int, text: str) -> bool: ...


class NotificationService:
    def __init__(self, database: Database, bridge: EngineBridge, config_provider,
                 on_sent=None) -> None:
        self._db = database
        self._bridge = bridge
        self._config_provider = config_provider   # () -> ScannerConfig (confidence threshold)
        self._notifier: Notifier | None = None
        self._on_sent = on_sent or (lambda: None)  # telemetry hook (ENGINE STATS)
        self._task: asyncio.Task | None = None
        self._passes: set[asyncio.Task] = set()
        self._stop = asyncio.Event()

    def bind_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="notification-consumer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event, signal = await self._bridge.next_event()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("consume_error", error=str(exc), exc_info=True)
                continue
            log.info("dispatcher_received", signal_id=signal.id, admit_event=event,
                     arb_type=signal.arb_type.value, coin=signal.coin,
                     net_profit_pct=float(round(signal.net_profit_pct, 4)),
                     queue_pending=self._bridge.pending())
            try:
                await self._dispatch(signal)
            except Exception as exc:  # noqa: BLE001
                log.warning("dispatch_error", signal_id=signal.id, error=str(exc),
                            exc_info=True)

    async def _dispatch(self, signal: Signal) -> None:
        # Schedule a per-tier delayed pass (§13.3). Pro=0s, Basic=10s, Free=60s.
        for delay in (0, 10, 60):
            task = asyncio.create_task(self._delayed_pass(signal, delay))
            # Keep a reference and retrieve exceptions so a failing pass is logged
            # rather than surfacing as an unretrieved-task warning (which hid the
            # UserRole/enum coercion crash that silently killed every notification).
            self._passes.add(task)
            task.add_done_callback(self._passes.discard)

    async def _delayed_pass(self, signal: Signal, delay: int) -> None:
        if delay:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
        # Loading candidates must never crash the whole pass: a single malformed user
        # row (bad enum: role/tier/status/language written by an older schema or manual
        # edit) previously raised out of the list build and silently dropped alerts for
        # ALL users. Guard it and surface the trace.
        try:
            async with self._db.session() as session:
                candidates = await UserRepository(session).alert_candidates()
        except Exception as exc:  # noqa: BLE001
            log.warning("alert_candidates_failed", signal_id=signal.id, delay=delay,
                        error=str(exc), exc_info=True)
            return
        eligible = [p for p in candidates
                    if entitlements_for(p.effective_tier).signal_delay_sec == delay]
        log.info("dispatch_pass", signal_id=signal.id, delay=delay,
                 candidates=len(candidates), eligible_this_pass=len(eligible))
        sent = 0
        for profile in eligible:
            try:
                if await self._evaluate_user(profile, signal):
                    sent += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("user_eval_error", user_id=profile.telegram_user_id,
                            signal_id=signal.id, error=str(exc), exc_info=True)
        log.info("dispatch_pass_done", signal_id=signal.id, delay=delay, delivered=sent)

    async def _evaluate_user(self, profile: UserProfile, signal: Signal) -> bool:
        """Return True iff an alert was actually delivered to this user."""
        uid = profile.telegram_user_id
        if self._notifier is None:
            log.warning("notifier_unbound", signal_id=signal.id)
            return False
        ent = entitlements_for(profile.effective_tier)
        settings = profile.settings

        async with self._db.session() as session:
            notif_repo = NotificationRepository(session)
            fav_repo = FavoritesRepository(session)

            # Favorite Coin/Exchange alerts bypass general filters (§13.1, Basic+).
            fav_trigger = False
            if ent.favorite_entity_alerts:
                if settings.favorite_coin_alerts:
                    coins = await fav_repo.values(uid, "coin")
                    fav_trigger = fav_trigger or signal.coin in coins
                if settings.favorite_exchange_alerts:
                    exch = await fav_repo.values(uid, "exchange")
                    fav_trigger = fav_trigger or bool(
                        {signal.buy_exchange, signal.sell_exchange} & set(exch))

            passes_filter = (
                settings.instant_alerts_enabled
                and ent.arb_type_allowed(signal.arb_type)
                and profile.filter.matches(signal)
            )
            if not (passes_filter or fav_trigger):
                log.debug("alert_filtered", user_id=uid, signal_id=signal.id,
                          instant_alerts=settings.instant_alerts_enabled,
                          arb_allowed=ent.arb_type_allowed(signal.arb_type),
                          filter_match=profile.filter.matches(signal),
                          min_profit_pct=float(profile.filter.min_profit_pct),
                          signal_net_pct=float(round(signal.net_profit_pct, 4)))
                return False

            # Low-confidence gate (R-SCHEMA-2) — never for a below-threshold signal.
            threshold = self._config_provider().confidence_threshold
            if signal.confidence_score < threshold:
                log.debug("alert_low_confidence", user_id=uid, signal_id=signal.id,
                          confidence=signal.confidence_score, threshold=threshold)
                return False

            # Mute (§13.5).
            if await notif_repo.is_muted(uid, signal.trading_pair, datetime.now(UTC)):
                log.debug("alert_muted", user_id=uid, signal_id=signal.id)
                return False

            # Per-user cooldown + significant-change override (§18).
            dedup = "|".join(str(x) for x in signal.dedup_key())
            cooldown = await notif_repo.get_cooldown(uid, dedup)
            if cooldown and cooldown.until.replace(tzinfo=UTC) > datetime.now(UTC):
                delta = abs(signal.net_profit_pct - Decimal(str(cooldown.last_net_pct)))
                if delta < _PROFIT_OVERRIDE_PP:
                    log.debug("alert_cooldown", user_id=uid, signal_id=signal.id)
                    return False  # still cooling down, not a significant change

            # Hourly cap with throttle notice (§13.3 / R-NOTIF-1).
            if ent.instant_alerts_per_hour != UNLIMITED:
                since = datetime.now(UTC) - timedelta(hours=1)
                sent = await notif_repo.alerts_in_last_hour(uid, since)
                if sent >= ent.instant_alerts_per_hour:
                    if sent == ent.instant_alerts_per_hour:
                        await self._notifier.send_text(
                            uid,
                            "You've hit your hourly alert limit — more matching signals "
                            "were found. Upgrade to Pro for unlimited alerts.")
                        await notif_repo.log(uid, "throttle_notice",
                                             "Hourly alert limit reached")
                    log.debug("alert_hourly_capped", user_id=uid, signal_id=signal.id)
                    return False

            ok = await self._notifier.send_alert(uid, signal, settings.language.value)
            if ok:
                self._on_sent()
                log.info("message_delivered", user_id=uid,
                         coin=signal.coin, pair=signal.trading_pair,
                         buy_exchange=signal.buy_exchange,
                         sell_exchange=signal.sell_exchange,
                         net_profit_pct=float(round(signal.net_profit_pct, 4)),
                         signal_id=signal.id)
                await notif_repo.log(uid, "instant_alert",
                                     f"{signal.coin} {signal.net_profit_pct:.2f}%")
                until = datetime.now(UTC) + timedelta(seconds=_COOLDOWN_SEC)
                await notif_repo.set_cooldown(uid, dedup,
                                              float(signal.net_profit_pct), until)
                return True
            return False
