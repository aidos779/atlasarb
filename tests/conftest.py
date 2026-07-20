"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from src.config.scanner_config import ConfigManager, ScannerConfig
from src.config.settings import Settings
from src.database.base import Database
from src.services.product_catalog import ProductCatalog


@pytest.fixture
def config() -> ScannerConfig:
    return ScannerConfig()


@pytest.fixture
def config_manager() -> ConfigManager:
    return ConfigManager(ScannerConfig())


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token="123:TEST", database_url="sqlite+aiosqlite:///:memory:",
        environment="development", admin_telegram_ids=[999], support_user_ids=[888],
    )


@pytest.fixture
def catalog(settings: Settings) -> ProductCatalog:
    return ProductCatalog(settings)


@pytest.fixture
async def database(settings: Settings) -> Database:
    db = Database(settings)
    await db.create_all()
    yield db
    await db.dispose()
