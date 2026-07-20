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

from src.config import describe_exc, get_logger
from src.database.base import Database
from src.database.repositories.favorites_repo import FavoritesRepository
from src.database.repositories.misc_repos import NotificationRepository
from src.database.repositories.user_repo import UserRepository
from src.domain.entitlements import UNLIMITED, distinct_signal_delays, entitlements_for
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.i18n import t
from src.services.engine_bridge import EngineBridge
from src.services.signal_access_service import CHANNEL_ALERT, SignalAccessService

log = get_logger("services.notification")

_PAYWALL_NOTICE = "paywall_notice"
_COOLDOWN_SEC = 60
_PROFIT_OVERRIDE_PP = Decimal("0.5")  # §18.3 ±0.5 percentage points

# Consumers draining the bridge queue. One consumer serialised the whole outbound
# pipeline behind a single signal's fan-out, so a slow pass (hundreds of users x a
# Telegram round-trip) let the backlog grow until entries aged out. Several consumers
# overlap those waits; the work is I/O-bound, so this costs no CPU.
_CONSUMER_WORKERS = 4

# Bound on users evaluated concurrently within one dispatch pass. Each slot holds one DB
# session plus at most one in-flight Telegram send, so this must stay comfortably under
# the DB pool (db_pool_size default 20) — otherwise a large pass would exhaust the pool
# and stall every other query in the process, including the bot's own handlers.
_USER_FANOUT = 8


class Notifier(Protocol):
    async def send_alert(self, user_id: int, signal: Signal, language: str) -> bool: ...
    async def send_text(self, user_id: int, text: str) -> bool: ...


class NotificationService:
    def __init__(self, database: Database, bridge: EngineBridge, config_provider,
                 on_sent=None, signal_access: SignalAccessService | None = None) -> None:
        self._db = database
        self._bridge = bridge
        self._config_provider = config_provider   # () -> ScannerConfig (confidence threshold)
        self._signal_access = signal_access
        self._notifier: Notifier | None = None
        self._on_sent = on_sent or (lambda: None)  # telemetry hook (ENGINE STATS)
        self._tasks: list[asyncio.Task] = []
        self._passes: set[asyncio.Task] = set()
        self._stop = asyncio.Event()

    def bind_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run(worker), name=f"notification-consumer-{worker}")
            for worker in range(_CONSUMER_WORKERS)
        ]

    async def stop(self) -> None:
        self._stop.set()
        # Cancel and reap both consumers and any in-flight delayed passes. Leaving the
        # passes to be garbage-collected surfaced as "Task exception was never retrieved"
        # tracebacks on every shutdown, which the new asyncio hook would now (correctly
        # but noisily) report as unhandled errors.
        pending = [*self._tasks, *self._passes]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []
        self._passes.clear()

    async def _run(self, worker: int) -> None:
        while not self._stop.is_set():
            try:
                event, signal = await self._bridge.next_event()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.error("consume_error", worker=worker, error=describe_exc(exc),
                          exc_info=exc)
                continue
            log.info("dispatcher_received", signal_id=signal.id, admit_event=event,
                     worker=worker,
                     arb_type=signal.arb_type.value, coin=signal.coin,
                     net_profit_pct=float(round(signal.net_profit_pct, 4)),
                     queue_pending=self._bridge.pending())
            try:
                await self._dispatch(signal)
            except Exception as exc:  # noqa: BLE001
                log.error("dispatch_error", signal_id=signal.id, worker=worker,
                          error=describe_exc(exc), exc_info=exc)

    async def _dispatch(self, signal: Signal) -> None:
        # One delivery pass per distinct tier delay, taken from the entitlement matrix.
        # Under the two-plan model every user is real-time, so this is a single pass.
        for delay in distinct_signal_delays():
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
            log.error("alert_candidates_failed", signal_id=signal.id, delay=delay,
                      error=describe_exc(exc), exc_info=exc)
            return
        eligible = [p for p in candidates
                    if entitlements_for(p.effective_tier).signal_delay_sec == delay]
        log.info("dispatch_pass", signal_id=signal.id, delay=delay,
                 arb_type=signal.arb_type.value, coin=signal.coin,
                 net_profit_pct=float(round(signal.net_profit_pct, 4)),
                 candidates=len(candidates), eligible_this_pass=len(eligible))

        # Fan out across users with a bounded concurrency window. Sequential evaluation
        # made the pass cost O(users) Telegram round-trips end to end, which is what let
        # the bridge backlog build during a burst.
        limiter = asyncio.Semaphore(_USER_FANOUT)

        async def _one(profile: UserProfile) -> bool:
            async with limiter:
                # Per-user isolation: this user's failure (a bad enum, a duplicate-key
                # race, a dead session) is contained here and never aborts, rolls back,
                # or short-circuits any other user's evaluation in the same pass.
                try:
                    return await self._evaluate_user(profile, signal)
                except Exception as exc:  # noqa: BLE001
                    log.error("user_eval_error", user_id=profile.telegram_user_id,
                              signal_id=signal.id, error=describe_exc(exc), exc_info=exc)
                    return False

        results = await asyncio.gather(*(_one(p) for p in eligible))
        sent = sum(1 for ok in results if ok)
        log.info("dispatch_pass_done", signal_id=signal.id, delay=delay,
                 arb_type=signal.arb_type.value,
                 net_profit_pct=float(round(signal.net_profit_pct, 4)), delivered=sent)

    async def _evaluate_user(self, profile: UserProfile, signal: Signal) -> bool:
        """Return True iff an alert was actually delivered to this user.

        Runs in two short, independent transactions with the Telegram round-trip between
        them. Holding one session open across the send (as this used to) pinned a pool
        connection for the duration of an external HTTP call — with the pass now fanning
        out over users, that would exhaust ``db_pool_size`` and stall every other query
        in the process. Each transaction is also this user's alone, so a failure in
        either is isolated to them (see the caller's per-user guard).
        """
        uid = profile.telegram_user_id
        if self._notifier is None:
            log.warning("notifier_unbound", signal_id=signal.id)
            return False
        ent = entitlements_for(profile.effective_tier)
        settings = profile.settings

        # ── Phase 1 (read-only transaction): eligibility ──
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
                log.info("alert_dropped_filter", user_id=uid, signal_id=signal.id,
                          instant_alerts=settings.instant_alerts_enabled,
                          arb_allowed=ent.arb_type_allowed(signal.arb_type),
                          filter_match=profile.filter.matches(signal),
                          min_profit_pct=float(profile.filter.min_profit_pct),
                          signal_net_pct=float(round(signal.net_profit_pct, 4)))
                return False

            # Low-confidence gate (R-SCHEMA-2) — never for a below-threshold signal.
            threshold = self._config_provider().confidence_threshold
            if signal.confidence_score < threshold:
                log.info("alert_dropped_confidence", user_id=uid, signal_id=signal.id,
                          confidence=signal.confidence_score, threshold=threshold)
                return False

            # Mute (§13.5).
            if await notif_repo.is_muted(uid, signal.trading_pair, datetime.now(UTC)):
                log.info("alert_dropped_muted", user_id=uid, signal_id=signal.id)
                return False

            # Per-user cooldown + significant-change override (§18).
            dedup = "|".join(str(x) for x in signal.dedup_key())
            cooldown = await notif_repo.get_cooldown(uid, dedup)
            if cooldown and cooldown.until.replace(tzinfo=UTC) > datetime.now(UTC):
                delta = abs(signal.net_profit_pct - Decimal(str(cooldown.last_net_pct)))
                if delta < _PROFIT_OVERRIDE_PP:
                    log.info("alert_dropped_cooldown", user_id=uid, signal_id=signal.id)
                    return False  # still cooling down, not a significant change

            # Hourly cap with throttle notice (§13.3 / R-NOTIF-1).
            notify_throttled = False
            capped = False
            if ent.instant_alerts_per_hour != UNLIMITED:
                since = datetime.now(UTC) - timedelta(hours=1)
                sent = await notif_repo.alerts_in_last_hour(uid, since)
                if sent >= ent.instant_alerts_per_hour:
                    capped = True
                    # The notice fires exactly once, on the alert that crosses the cap.
                    # Record it inside this transaction so a crash before the send can
                    # never re-notify; the send itself happens after the commit.
                    if sent == ent.instant_alerts_per_hour:
                        notify_throttled = True
                        await notif_repo.log(uid, "throttle_notice",
                                             "Hourly alert limit reached")
                    log.info("alert_dropped_hourly_cap", user_id=uid, signal_id=signal.id)

        if notify_throttled:
            await self._notifier.send_text(
                uid, t("alert.hourly_cap", settings.language.value))
        if capped:
            return False

        # Free quota gate — the last check before the send, so a user whose 5 signals
        # are spent is never pushed a 6th.
        if self._signal_access is not None and not await self._signal_access.may_deliver(
                profile):
            log.info("alert_dropped_quota", user_id=uid, signal_id=signal.id)
            return False

        # ── Telegram round-trip, holding no DB connection ──
        ok = await self._notifier.send_alert(uid, signal, settings.language.value)
        if not ok:
            return False

        # Delivery is confirmed — only now does it cost the user a free slot. A send that
        # returned False above skipped this, so a failed notification is never charged.
        if self._signal_access is not None:
            await self._signal_access.record_delivery(profile, signal.id, CHANNEL_ALERT)
            await self._maybe_notify_paywall(profile)

        self._on_sent()
        log.info("message_delivered", user_id=uid,
                 coin=signal.coin, pair=signal.trading_pair,
                 buy_exchange=signal.buy_exchange,
                 sell_exchange=signal.sell_exchange,
                 net_profit_pct=float(round(signal.net_profit_pct, 4)),
                 signal_id=signal.id)

        # ── Phase 2 (write transaction): record delivery + arm the cooldown ──
        # (see _maybe_notify_paywall above for the quota notice)
        # set_cooldown is an atomic UPSERT on (user_id, dedup_key), so two passes racing
        # on the same signal reconcile instead of raising UniqueViolationError.
        async with self._db.session() as session:
            notif_repo = NotificationRepository(session)
            await notif_repo.log(uid, "instant_alert",
                                 f"{signal.coin} {signal.net_profit_pct:.2f}%")
            until = datetime.now(UTC) + timedelta(seconds=_COOLDOWN_SEC)
            await notif_repo.set_cooldown(uid, dedup,
                                          float(signal.net_profit_pct), until)
        return True

    async def _maybe_notify_paywall(self, profile: UserProfile) -> None:
        """Announce the paywall once, on the alert that spends the last free signal.

        Guarded by a logged marker rather than by "did we just cross the line", so a
        crash between the send and the log cannot produce a second announcement, and a
        user who never opens the bot again still got told why the signals stopped.
        """
        if self._signal_access is None or self._notifier is None:
            return
        allowance = await self._signal_access.allowance(profile)
        if not allowance.exhausted:
            return
        uid = profile.telegram_user_id
        async with self._db.session() as session:
            notif_repo = NotificationRepository(session)
            if await notif_repo.has_logged(uid, _PAYWALL_NOTICE):
                return
            await notif_repo.log(uid, _PAYWALL_NOTICE, "Free signal quota reached")
        await self._notifier.send_text(uid, self._signal_access.paywall_text(profile))
        log.info("paywall_notice_sent", user_id=uid)
