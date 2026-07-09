"""Telegram Notifier — implements the NotificationService.Notifier port with aiogram.

Renders the Instant Alert (§13.2) with its action buttons and sends push messages. The
NotificationService stays framework-agnostic; this adapter is the only aiogram-aware part
of the notification path.
"""
from __future__ import annotations

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.formatters.signal import format_alert
from src.bot.i18n import t
from src.config import get_logger
from src.domain.signal import Signal
from src.scanner.adapters.fx import FxRateProvider
from src.services.signal_registry import SignalRegistry
from src.services.user_service import UserService

log = get_logger("bot.notifier")


class TelegramNotifier:
    def __init__(self, bot: Bot, users: UserService, fx: FxRateProvider,
                 registry: SignalRegistry) -> None:
        self._bot = bot
        self._users = users
        self._fx = fx
        self._registry = registry

    async def send_alert(self, user_id: int, signal: Signal, language: str) -> bool:
        profile = await self._users.get(user_id)
        if profile is None:
            log.warning("alert_no_profile", user_id=user_id, signal_id=signal.id)
            return False
        # Formatting is a common silent-failure point (HTML entity / i18n key / FX
        # lookup). Surface it with a full trace instead of letting it bubble up and
        # get swallowed by the dispatcher's per-user guard.
        try:
            text = await format_alert(signal, profile, self._fx)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert_format_failed", user_id=user_id, signal_id=signal.id,
                        error=str(exc), exc_info=True)
            return False
        b = InlineKeyboardBuilder()
        b.button(text=t("btn.details", language), callback_data=f"sig:details:{signal.id}")
        b.button(text=t("btn.favorite", language), callback_data=f"sig:fav:{signal.id}")
        b.button(text=t("alert.mute", language), callback_data=f"alert:mute:{signal.id}")
        b.adjust(2, 1)
        log.info("telegram_send_requested", user_id=user_id, signal_id=signal.id,
                 coin=signal.coin, arb_type=signal.arb_type.value)
        try:
            msg = await self._bot.send_message(user_id, text, reply_markup=b.as_markup(),
                                               disable_web_page_preview=True)
            log.info("telegram_api_response", user_id=user_id, signal_id=signal.id,
                     ok=True, message_id=getattr(msg, "message_id", None))
            return True
        except Exception as exc:  # noqa: BLE001 — Bot API errors must be visible
            # WARNING + full trace: chat-not-found, bot-blocked, 429 flood-wait, HTML
            # parse errors all landed here silently at DEBUG, hiding telegram_sent=0.
            log.warning("telegram_send_failed", user_id=user_id, signal_id=signal.id,
                        error=str(exc), exc_info=True)
            return False

    async def send_text(self, user_id: int, text: str) -> bool:
        try:
            await self._bot.send_message(user_id, text)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("text_send_failed", user_id=user_id, error=str(exc), exc_info=True)
            return False
