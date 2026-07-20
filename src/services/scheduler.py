"""Background scheduler — Daily Summary delivery and mute purge (PRD §13.4, §13.5).

Runs a 60-second tick. Daily Summary fires at each user's configured local time
(FR-NOTIF-02, timezone-aware).

There is no subscription expiry pass: Pro is a one-time lifetime purchase, so no
account can lapse and nothing needs sweeping.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import describe_exc, get_logger
from src.database.base import Database
from src.database.repositories.misc_repos import NotificationRepository
from src.database.repositories.user_repo import UserRepository
from src.i18n import t
from src.services.notification_service import Notifier
from src.services.signal_registry import SignalRegistry
from src.services.subscription_service import SubscriptionService

log = get_logger("services.scheduler")


class BackgroundScheduler:
    def __init__(self, database: Database, registry: SignalRegistry,
                 subscriptions: SubscriptionService) -> None:
        self._db = database
        self._registry = registry
        self._subscriptions = subscriptions
        self._notifier: Notifier | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._summary_sent: dict[int, str] = {}   # user_id -> YYYY-MM-DD:HH:MM

    def bind_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                log.error("scheduler_tick_error", error=describe_exc(exc), exc_info=exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        await self._purge_mutes()
        await self._daily_summaries()

    async def _purge_mutes(self) -> None:
        async with self._db.session() as session:
            await NotificationRepository(session).purge_expired_mutes(datetime.now(UTC))

    async def _daily_summaries(self) -> None:
        if self._notifier is None:
            return
        async with self._db.session() as session:
            users = await UserRepository(session).daily_summary_users()
        for profile in users:
            try:
                tz = ZoneInfo(profile.settings.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                tz = UTC
            local = datetime.now(tz)
            hhmm = local.strftime("%H:%M")
            if hhmm != profile.settings.daily_summary_time:
                continue
            marker = f"{local.strftime('%Y-%m-%d')}:{hhmm}"
            if self._summary_sent.get(profile.telegram_user_id) == marker:
                continue
            self._summary_sent[profile.telegram_user_id] = marker
            await self._send_summary(profile)

    async def _send_summary(self, profile) -> None:
        matching = [s for s in self._registry.all_active() if profile.filter.matches(s)]
        top = sorted(matching, key=lambda s: s.net_profit_pct, reverse=True)[:3]
        lang = profile.settings.language.value
        lines = [t("summary.title", lang, count=len(matching))]
        for s in top:
            lines.append(t("summary.line", lang, coin=s.coin,
                           quote=s.trading_pair.split("/")[-1],
                           profit=f"{s.net_profit_pct:.2f}",
                           buy=s.buy_exchange, sell=s.sell_exchange))
        if not top:
            lines.append(t("summary.empty", lang))
        await self._notifier.send_text(profile.telegram_user_id, "\n".join(lines))

