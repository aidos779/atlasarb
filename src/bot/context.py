"""BotContext — the dependency container injected into every handler (Composition Root).

Handlers receive this via aiogram's workflow data (Dependency Injection); they never
construct services or touch the DB/engine directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config.scanner_config import ConfigManager
from src.config.settings import Settings
from src.database.base import Database
from src.scanner.adapters.fx import FxRateProvider
from src.scanner.engine import ScanningEngine
from src.services.admin_service import AdminService
from src.services.analytics_service import AnalyticsService
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


@dataclass
class BotContext:
    settings: Settings
    config_manager: ConfigManager
    database: Database
    users: UserService
    subscriptions: SubscriptionService
    favorites: FavoritesService
    history: HistoryService
    search: SearchService
    analytics: AnalyticsService
    admin: AdminService
    support: SupportService
    notifications: NotificationService
    scheduler: BackgroundScheduler
    registry: SignalRegistry
    engine: ScanningEngine
    fx: FxRateProvider
    refresh_limiter: SlidingWindowLimiter
    exchange_names: dict[str, str]
