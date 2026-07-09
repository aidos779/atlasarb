"""Composition root — wires every component and runs the bot + scanning engine.

Responsibilities (and nothing else): construct concrete implementations, inject them,
start background tasks, and start Telegram polling. All business logic lives in the
layers below; this file is the only place that knows about all of them at once.
"""
from __future__ import annotations

import asyncio
import os

import certifi

# Bind the process-wide default TLS trust store to the bundled certifi CA set BEFORE any
# library (aiogram/aiohttp/asyncpg) builds its first SSL context. On runtimes whose system
# trust store is empty — a stock python.org macOS build or a slim Docker image without the
# ca-certificates package — the default store yields CERTIFICATE_VERIFY_FAILED for every
# outbound HTTPS/WSS call, which silently offlines every exchange and blocks Telegram
# delivery. The scanner adapters also set this explicitly per-connector (adapters/tls.py);
# this covers third-party libraries we don't construct the session for.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from src.bot.context import BotContext
from src.bot.handlers import register_handlers
from src.bot.middlewares.context import ContextMiddleware
from src.bot.middlewares.throttle import ThrottleMiddleware
from src.bot.notifier import TelegramNotifier
from src.config import configure_logging, get_logger, get_settings
from src.config.scanner_config import ConfigManager, ScannerConfig
from src.database.base import Database
from src.database.repositories.history_repo import HistoryRepository
from src.domain.dev_mode import set_unlimited_access
from src.scanner.adapters.fx import FxRateProvider
from src.scanner.adapters.registry import build_scanner_components
from src.scanner.engine import ScanningEngine
from src.services.admin_service import AdminService
from src.services.analytics_service import AnalyticsService
from src.services.engine_bridge import EngineBridge
from src.services.favorites_service import FavoritesService
from src.services.history_service import HistoryService
from src.services.notification_service import NotificationService
from src.services.rate_limiter import SlidingWindowLimiter
from src.services.scheduler import BackgroundScheduler
from src.services.search_service import SearchService
from src.services.signal_registry import SignalRegistry
from src.services.subscription_service import SubscriptionService
from src.services.support_service import SupportService
from src.services.user_service import UserService

log = get_logger("app")

_COMMANDS = [
    BotCommand(command="start", description="Start / Main Menu"),
    BotCommand(command="menu", description="Main Menu"),
    BotCommand(command="signals", description="Live arbitrage signals"),
    BotCommand(command="favorites", description="Favorites"),
    BotCommand(command="history", description="Signal history (Paid)"),
    BotCommand(command="profile", description="Your profile"),
    BotCommand(command="subscription", description="Subscription & plans"),
    BotCommand(command="settings", description="Settings"),
    BotCommand(command="search", description="Search coins/exchanges"),
    BotCommand(command="help", description="Help"),
    BotCommand(command="cancel", description="Cancel current input"),
]


def _environment_config_layer(environment: str) -> dict:
    """§20.6 environment-specific config layer — wider tolerances in non-prod."""
    if environment.lower() in ("development", "staging"):
        return {"min_liquidity_cex_usd": 500.0, "min_liquidity_dex_usd": 1000.0,
                "min_net_profit_usd": 1.0}
    return {}


def _file_config_layer(path: str) -> dict:
    """Operator overrides from SCANNER_CONFIG_FILE (§20.6 environment layer).
    The file always existed as documentation; now it is actually loaded."""
    import tomllib
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001 — bad file must not kill startup
        log.warning("scanner_config_file_invalid", path=path, error=str(exc))
        return {}


class Application:
    def __init__(self) -> None:
        self.settings = get_settings()
        configure_logging(self.settings.log_level, self.settings.log_json)
        # Dev-build paywall removal: outside production, every user gets full PRO
        # access to all tier-gated features. Production keeps the real paywall.
        set_unlimited_access(not self.settings.is_production)
        if not self.settings.is_production:
            log.warning("dev_unlimited_access_enabled",
                        note="all users have full PRO access (paywall disabled)")
        self.config_manager = ConfigManager(ScannerConfig())
        self.config_manager.apply_environment_layer(
            _environment_config_layer(self.settings.environment))
        file_layer = _file_config_layer(self.settings.scanner_config_file)
        if file_layer:
            self.config_manager.apply_environment_layer(file_layer)
            log.info("scanner_config_file_applied",
                     path=self.settings.scanner_config_file,
                     keys=sorted(file_layer))
        config = self.config_manager.config

        self.database = Database(self.settings)
        components = build_scanner_components(self.settings, config)
        self.registry = SignalRegistry()
        self.bridge = EngineBridge(
            self.registry, self.database,
            history_repo_factory=lambda s: HistoryRepository(s),
        )
        self.engine = ScanningEngine(
            config, components.adapters, self.bridge, self.bridge, components.gas,
            components.verified_tokens, cache=components.cache, health=components.health,
        )
        self.config_manager.subscribe(self.engine.update_config)
        self.fx = FxRateProvider()
        exchange_names = {vid: a.display_name for vid, a in components.adapters.items()}

        # Services.
        users = UserService(self.database, self.settings)
        subscriptions = SubscriptionService(self.database, self.settings)
        favorites = FavoritesService(self.database)
        history = HistoryService(self.database)
        search = SearchService(self.registry, exchange_names)
        analytics = AnalyticsService(self.registry, self.engine)
        admin = AdminService(self.database, self.engine)
        support = SupportService(self.database)
        self.notifications = NotificationService(
            self.database, self.bridge, lambda: self.config_manager.config,
            on_sent=self.engine.metrics.record_notification_sent)
        self.scheduler = BackgroundScheduler(self.database, self.registry, subscriptions)

        self.ctx = BotContext(
            settings=self.settings, config_manager=self.config_manager,
            database=self.database, users=users, subscriptions=subscriptions,
            favorites=favorites, history=history, search=search, analytics=analytics,
            admin=admin, support=support, notifications=self.notifications,
            scheduler=self.scheduler, registry=self.registry, engine=self.engine,
            fx=self.fx, refresh_limiter=SlidingWindowLimiter(1, 3.0),
            exchange_names=exchange_names,
        )

        self.bot = Bot(
            token=self.settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self._setup_dispatcher()

    def _setup_dispatcher(self) -> None:
        ctx_mw = ContextMiddleware(self.ctx)
        throttle = ThrottleMiddleware()
        for observer in (self.dp.message, self.dp.callback_query):
            observer.middleware(ctx_mw)
            observer.middleware(throttle)
        register_handlers(self.dp)
        self.dp.workflow_data.update(ctx=self.ctx, bot=self.bot)

    async def run(self) -> None:
        await self.database.create_all()
        notifier = TelegramNotifier(self.bot, self.ctx.users, self.fx, self.registry)
        self.notifications.bind_notifier(notifier)
        self.scheduler.bind_notifier(notifier)

        await self.engine.start()
        self.notifications.start()
        self.scheduler.start()
        await self.bot.set_my_commands(_COMMANDS)
        log.info("bot_starting", environment=self.settings.environment)
        try:
            await self.dp.start_polling(self.bot, handle_signals=True)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        log.info("bot_shutting_down")
        await self.notifications.stop()
        await self.scheduler.stop()
        await self.engine.stop()
        await self.database.dispose()
        await self.bot.session.close()


def main() -> None:
    app = Application()
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
