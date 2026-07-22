"""Composition root — wires every component and runs the bot + scanning engine.

Responsibilities (and nothing else): construct concrete implementations, inject them,
start background tasks, and start Telegram polling. All business logic lives in the
layers below; this file is the only place that knows about all of them at once.
"""
from __future__ import annotations

import asyncio
import os
import sys

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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import BotCommand, ErrorEvent

from src.bot.callbacks import is_expired_callback_error
from src.bot.context import BotContext
from src.bot.handlers import register_handlers
from src.bot.middlewares.context import ContextMiddleware
from src.bot.middlewares.throttle import ThrottleMiddleware
from src.bot.notifier import TelegramNotifier
from src.config import (
    configure_logging,
    describe_exc,
    get_logger,
    get_settings,
    install_global_exception_hooks,
)
from src.config.scanner_config import ConfigManager, ScannerConfig
from src.database.base import Database
from src.database.repositories.history_repo import HistoryRepository
from src.domain.dev_mode import set_unlimited_access
from src.i18n import LANGUAGES, t, validate_catalog
from src.scanner.adapters.fx import FxRateProvider
from src.scanner.adapters.registry import build_scanner_components
from src.scanner.adapters.rpc_health import log_startup_rpc_health
from src.scanner.engine import ScanningEngine
from src.services.admin_service import AdminService
from src.services.analytics_service import AnalyticsService
from src.services.engine_bridge import EngineBridge
from src.services.favorites_service import FavoritesService
from src.services.history_service import HistoryService, ReliabilityCache
from src.services.notification_service import NotificationService
from src.services.payments import (
    CryptoPayClient,
    CryptoPayProvider,
    CryptoPayWebhookHandler,
    PaymentReconciler,
    PlaceholderPaymentProvider,
)
from src.services.product_catalog import ProductCatalog
from src.services.purchase_service import PurchaseService
from src.services.rate_limiter import SlidingWindowLimiter
from src.services.scheduler import BackgroundScheduler
from src.services.search_service import SearchService
from src.services.signal_access_service import SignalAccessService
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
    BotCommand(command="buy", description="Buy Lifetime access"),
    BotCommand(command="settings", description="Settings"),
    BotCommand(command="search", description="Search coins/exchanges"),
    BotCommand(command="help", description="Help"),
    BotCommand(command="cancel", description="Cancel current input"),
]


# Callback-query answers that lose the 15s Telegram window (a user taps a stale button,
# or the handler was briefly slow) raise TelegramBadRequest "query is too old…". This is a
# benign timing condition, not a bug — swallow it as a structured warning instead of
# letting the raw traceback flood the logs. Any other TelegramBadRequest is re-raised so
# genuine API misuse still surfaces. Handlers ack through src.bot.callbacks.ack (which
# absorbs these in-place); this dispatcher-level net covers any answer path outside it.
async def _on_telegram_bad_request(event: ErrorEvent) -> bool:
    if is_expired_callback_error(event.exception):
        log.warning("callback_query_expired", error=str(event.exception))
        return True  # handled — no traceback
    raise event.exception  # not ours — let aiogram log it as before


def _verify_production_config(settings) -> None:
    """Fail fast on a misconfigured production boot (no-op elsewhere).

    Booting production on dev defaults is worse than not booting at all: a SQLite-backed
    deployment silently loses every user on restart, and a "change-me" webhook secret
    lets anyone forge a payment callback. Both look like application bugs weeks later
    rather than the deploy mistake they are. Refuse to start instead, naming every
    problem at once so a broken deploy is fixed in one pass.
    """
    errors = settings.production_config_errors()
    if not errors:
        return
    log.critical("production_config_invalid", environment=settings.environment,
                 error_count=len(errors), errors=errors)
    for problem in errors:
        log.critical("production_config_error", problem=problem)
    sys.exit(1)


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
        install_global_exception_hooks()
        # Dev-build paywall removal: outside production, every user gets full PRO
        # access to all tier-gated features. Production keeps the real paywall.
        set_unlimited_access(not self.settings.is_production)
        if not self.settings.is_production:
            log.warning("dev_unlimited_access_enabled",
                        note="all users have full PRO access (paywall disabled)")
        else:
            log.info("paywall_enforced", environment=self.settings.environment)
        # Preflight AFTER the paywall toggle so the check sees the state it validates.
        _verify_production_config(self.settings)
        # FR-LOC-01 — an incomplete catalog must not reach users as a half-translated UI.
        # Raises MissingTranslationsError, which main() reports as a fatal startup error.
        validate_catalog()
        log.info("i18n_catalog_validated", languages=list(LANGUAGES))
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
        # Historical reliability (§11.5): sync TTL-cache over the archived-signal
        # hit-rate; the assembler reads it per candidate without touching the DB.
        history = HistoryService(self.database)
        self.engine = ScanningEngine(
            config, components.adapters, self.bridge, self.bridge, components.gas,
            components.verified_tokens, cache=components.cache, health=components.health,
            reliability_provider=ReliabilityCache(history),
        )
        self.config_manager.subscribe(self.engine.update_config)
        self.fx = FxRateProvider()
        exchange_names = {vid: a.display_name for vid, a in components.adapters.items()}

        # Services.
        users = UserService(self.database, self.settings)
        catalog = ProductCatalog(self.settings)
        subscriptions = SubscriptionService(self.database, self.settings, catalog)
        favorites = FavoritesService(self.database)
        search = SearchService(self.registry, exchange_names)
        analytics = AnalyticsService(self.registry, self.engine)
        admin = AdminService(self.database, self.engine)
        support = SupportService(self.database)
        signal_access = SignalAccessService(self.database, catalog)
        # Live Telegram Crypto Pay when CRYPTO_PAY_TOKEN is set; otherwise the placeholder
        # refuses checkout rather than pretending it succeeded (see services/payments).
        payments, self._crypto_client = self._build_payment_provider()
        purchases = PurchaseService(self.database, subscriptions, payments)
        # Late-bound in run() once the Telegram notifier exists; the webhook/reconciler
        # confirmation callbacks read it at call time.
        self._payment_notifier = None
        self._webhook_runner = None
        self.payment_webhook: CryptoPayWebhookHandler | None = None
        reconciler: PaymentReconciler | None = None
        if self._crypto_client is not None:
            price_usd = catalog.pro_lifetime().amount_float
            reconciler = PaymentReconciler(
                self.database, self._crypto_client, purchases,
                on_paid=self._send_payment_confirmation)
            self.payment_webhook = CryptoPayWebhookHandler(
                self.database, self._crypto_client, purchases, price_usd=price_usd,
                accepted_assets=self.settings.crypto_pay_asset_list,
                on_paid=self._send_payment_confirmation)
        self.notifications = NotificationService(
            self.database, self.bridge, lambda: self.config_manager.config,
            on_sent=self.engine.metrics.record_notification_sent,
            signal_access=signal_access)
        self.scheduler = BackgroundScheduler(
            self.database, self.registry, subscriptions, reconciler=reconciler)

        self.ctx = BotContext(
            settings=self.settings, config_manager=self.config_manager,
            database=self.database, users=users, subscriptions=subscriptions,
            favorites=favorites, history=history, search=search, analytics=analytics,
            admin=admin, support=support, notifications=self.notifications,
            scheduler=self.scheduler, registry=self.registry, engine=self.engine,
            fx=self.fx, refresh_limiter=SlidingWindowLimiter(1, 3.0),
            exchange_names=exchange_names, signal_access=signal_access,
            payments=payments, products=catalog, purchases=purchases,
        )

        self.bot = Bot(
            token=self.settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self._setup_dispatcher()

    def _build_payment_provider(self):
        """(provider, crypto_client|None). A configured CRYPTO_PAY_TOKEN wires the live
        Crypto Pay provider; otherwise the placeholder keeps checkout as "coming soon"."""
        if not self.settings.crypto_pay_token:
            log.info("payment_provider_placeholder",
                     note="no CRYPTO_PAY_TOKEN — checkout shows 'coming soon'")
            return PlaceholderPaymentProvider(), None
        client = CryptoPayClient(
            self.settings.crypto_pay_token, self.settings.crypto_pay_api_url)
        provider = CryptoPayProvider(
            client, accepted_assets=self.settings.crypto_pay_asset_list,
            expires_in=self.settings.crypto_pay_invoice_expires_sec)
        log.info("payment_provider_cryptopay",
                 assets=self.settings.crypto_pay_asset_list,
                 webhook=self.settings.crypto_pay_webhook_enabled)
        return provider, client

    async def _send_payment_confirmation(self, purchase) -> None:
        """Deliver the "🎉 Payment received!" message in the buyer's language. Shared by
        the webhook handler and the polling reconciler; idempotency upstream guarantees it
        fires exactly once per settled purchase."""
        notifier = self._payment_notifier
        if notifier is None:
            return
        profile = await self.ctx.users.get(purchase.user_id)
        lang = profile.settings.language.value if profile else "en"
        text = "\n\n".join([
            t("payment.confirmed.title", lang),
            t("payment.confirmed.body", lang),
            t("payment.confirmed.thanks", lang),
        ])
        await notifier.send_text(purchase.user_id, text)

    async def _start_webhook_server(self) -> None:
        """Start the inbound Crypto Pay webhook listener (opt-in). No-op without a live
        provider or when disabled — the polling reconciler settles invoices either way."""
        if self.payment_webhook is None or not self.settings.crypto_pay_webhook_enabled:
            return
        from aiohttp import web

        async def handle(request: web.Request) -> web.Response:
            raw = await request.read()
            signature = request.headers.get("crypto-pay-api-signature", "")
            result = await self.payment_webhook.handle(raw, signature)
            # 400 only for an unparseable body (so Crypto Pay retries a genuine glitch);
            # every deliberate refusal returns 200 so it is not redelivered forever.
            code = 400 if result.status == "malformed" else 200
            return web.json_response(
                {"ok": result.ok, "status": result.status}, status=code)

        app = web.Application()
        app.router.add_post(self.settings.crypto_pay_webhook_path, handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.settings.crypto_pay_webhook_host,
                           self.settings.crypto_pay_webhook_port)
        await site.start()
        self._webhook_runner = runner
        log.info("crypto_pay_webhook_listening",
                 host=self.settings.crypto_pay_webhook_host,
                 port=self.settings.crypto_pay_webhook_port,
                 path=self.settings.crypto_pay_webhook_path)

    def _setup_dispatcher(self) -> None:
        ctx_mw = ContextMiddleware(self.ctx)
        throttle = ThrottleMiddleware()
        for observer in (self.dp.message, self.dp.callback_query):
            # Throttle FIRST (registration order is execution order): loading the profile
            # costs 8 SQL round trips, and running it ahead of the limiter spent all of
            # them on updates the limiter was about to drop — so a flood hit Postgres at
            # full rate. Shedding first makes a throttled update cost zero queries.
            # ThrottleMiddleware needs only `event_from_user`, which aiogram's own
            # dispatcher-level UserContextMiddleware supplies ahead of both of these.
            observer.middleware(throttle)
            observer.middleware(ctx_mw)
        register_handlers(self.dp)
        self.dp.errors.register(_on_telegram_bad_request,
                                ExceptionTypeFilter(TelegramBadRequest))
        self.dp.workflow_data.update(ctx=self.ctx, bot=self.bot)

    async def run(self) -> None:
        # Re-install now that a loop exists: the constructor runs before asyncio.run()
        # creates one, so the loop-level handler (orphaned-task tracebacks) can only be
        # attached here.
        install_global_exception_hooks(asyncio.get_running_loop())
        await self.database.create_all()
        notifier = TelegramNotifier(self.bot, self.ctx.users, self.fx, self.registry)
        self.notifications.bind_notifier(notifier)
        self.scheduler.bind_notifier(notifier)
        # Payment confirmations reuse the same notifier once it exists.
        self._payment_notifier = notifier
        await self._start_webhook_server()

        await self.engine.start()
        # Fire-and-forget: probe every configured RPC endpoint once and log how many are
        # actually reachable at boot (diagnostics only — never gates the pool). Kept off
        # the startup path so it cannot delay polling; a reference is held so the task is
        # not garbage-collected before it finishes.
        self._rpc_health_task = asyncio.create_task(
            log_startup_rpc_health(self.settings, self.config_manager.config),
            name="rpc-startup-health")
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
        if self._webhook_runner is not None:
            await self._webhook_runner.cleanup()
        await self.notifications.stop()
        await self.scheduler.stop()
        await self.engine.stop()
        await self.database.dispose()
        await self.bot.session.close()


def main() -> None:
    # Outermost net. sys.excepthook does not cover an exception escaping asyncio.run(),
    # so without this the process's final traceback is the one thing that still reached
    # stderr unstructured — precisely the record an operator most needs parsed.
    try:
        app = Application()
        asyncio.run(app.run())
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 — log, then exit non-zero
        log.critical("fatal_startup_error", error=describe_exc(exc), exc_info=exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
