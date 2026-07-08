import pytest

from src.database.repositories.favorites_repo import FavoritesRepository
from src.database.repositories.history_repo import HistoryRepository
from src.database.repositories.user_repo import UserRepository
from src.domain.enums import Currency, Language, UserRole
from src.domain.signal import Signal

pytestmark = pytest.mark.asyncio


async def test_user_roundtrip(database):
    async with database.session() as s:
        repo = UserRepository(s)
        await repo.get_or_create(5, "bob", "Bob")
    async with database.session() as s:
        repo = UserRepository(s)
        profile = await repo.get(5)
        profile.role = UserRole.FREE
        profile.settings.language = Language.RU
        profile.settings.currency = Currency.EUR
        profile.onboarding_step = 3
        await repo.save_profile(profile)
    async with database.session() as s:
        profile = await UserRepository(s).get(5)
    assert profile.role == UserRole.FREE
    assert profile.settings.language == Language.RU
    assert profile.settings.currency == Currency.EUR
    assert profile.onboarding_complete


async def test_favorites_freeze_and_unfreeze(database):
    async with database.session() as s:
        await UserRepository(s).get_or_create(6, "x", "X")
    async with database.session() as s:
        favs = FavoritesRepository(s)
        for i in range(5):
            await favs.add(6, "coin", f"C{i}")
        await favs.freeze_excess(6, "coin", keep=3)
    async with database.session() as s:
        favs = FavoritesRepository(s)
        assert await favs.count(6, "coin") == 3
        await favs.unfreeze_all(6, "coin")
        assert await favs.count(6, "coin") == 5


async def test_history_caps_at_50(database):
    async with database.session() as s:
        repo = HistoryRepository(s)
        for i in range(60):
            sig = Signal(id=f"s{i}", coin="ETH", trading_pair="ETH/USDT",
                         buy_exchange="binance", sell_exchange="okx")
            await repo.record_interaction(7, sig, "viewed")
    async with database.session() as s:
        rows = await HistoryRepository(s).list_interactions(7, "viewed", limit=100)
    assert len(rows) == 50
