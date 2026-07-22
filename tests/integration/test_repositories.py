import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.database.repositories.favorites_repo import FavoritesRepository
from src.database.repositories.history_repo import HistoryRepository
from src.database.repositories.misc_repos import NotificationRepository
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


async def test_get_or_create_is_idempotent(database):
    """UPSERT registration: repeated /start (or concurrent taps) never raises
    duplicate-key errors and keeps the Telegram identity fresh."""
    for name in ("alice", "alice", "alice2"):
        async with database.session() as s:
            profile = await UserRepository(s).get_or_create(77, name, "Alice")
    assert profile.telegram_user_id == 77
    assert profile.username == "alice2"
    async with database.session() as s:
        # Child rows exist exactly once and load cleanly.
        again = await UserRepository(s).get(77)
    assert again is not None
    assert again.subscription is not None and again.settings is not None


async def test_set_cooldown_is_idempotent_upsert(database):
    """Per-user alert cooldown UPSERT (uq_alert_cooldown): the delayed per-tier passes
    (0/10/60s) and concurrent signals for the same (user, dedup_key) must never raise a
    duplicate-key IntegrityError — the second writer updates the row instead. This is the
    prod user_eval_error / 'duplicate key value violates uq_alert_cooldown' defect."""
    async with database.session() as s:
        await UserRepository(s).get_or_create(88, "carol", "Carol")
    until = datetime.now(UTC) + timedelta(seconds=60)
    # Two writes to the SAME (user_id, dedup_key) — the pre-fix read-then-insert raced here.
    for net in (0.31, 0.42):
        async with database.session() as s:
            await NotificationRepository(s).set_cooldown(88, "ETH/USDT|binance|okx", net, until)
    async with database.session() as s:
        row = await NotificationRepository(s).get_cooldown(88, "ETH/USDT|binance|okx")
    assert row is not None
    assert float(row.last_net_pct) == pytest.approx(0.42)  # last writer won, no error


async def test_concurrent_cooldown_writers_do_not_raise(database):
    """The real shape of the prod defect: the 0s/10s/60s passes for one signal land on
    the same (user_id, dedup_key) *in flight*, each in its own session. Sequential writes
    would not reproduce it — overlapping ones do. None may raise IntegrityError."""
    async with database.session() as s:
        await UserRepository(s).get_or_create(89, "dave", "Dave")
    until = datetime.now(UTC) + timedelta(seconds=60)

    async def _write(net: float) -> None:
        async with database.session() as s:
            await NotificationRepository(s).set_cooldown(89, "BTC/USDT|binance|okx",
                                                         net, until)

    results = await asyncio.gather(*(_write(n) for n in (0.1, 0.2, 0.3, 0.4, 0.5)),
                                   return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"concurrent UPSERT raised: {errors}"

    async with database.session() as s:
        row = await NotificationRepository(s).get_cooldown(89, "BTC/USDT|binance|okx")
    assert row is not None                       # exactly one row, not five


# ── §11.5 type_reliability metric (production-review fix E1) ─────────────────────────

def _hist_signal(i, expiry_reason, lifetime_sec):
    from decimal import Decimal
    from src.domain.enums import ArbitrageType
    s = Signal(arb_type=ArbitrageType.CEX_CEX, coin="ETH", trading_pair="ETH/USDT",
               buy_exchange="binance", sell_exchange="okx",
               buy_price=Decimal("1"), sell_price=Decimal("1"))
    s.id = f"rel-{i}"
    s.expiry_reason = expiry_reason
    now = datetime.now(UTC)
    s.timestamp = (now - timedelta(seconds=lifetime_sec)).timestamp()
    s.expired_at = now.timestamp()
    return s


async def test_reliability_counts_arbitraged_away_spreads_as_success(database):
    """SPREAD_CLOSED after a plausible lifetime = a real opportunity that got traded —
    it must NOT count against the route (the old metric suppressed exactly the
    most-executed real routes)."""
    async with database.session() as s:
        repo = HistoryRepository(s)
        for i, (reason, life) in enumerate([
            ("TTL_EXCEEDED", 120),      # held to TTL          -> good
            ("SPREAD_CLOSED", 45),      # traded away          -> good (was: bad)
            ("SPREAD_CLOSED", 2),       # phantom instant close -> bad
            ("VENUE_OFFLINE", 30),      # venue failure         -> bad
        ]):
            await repo.archive_signal(_hist_signal(i, reason, life))
    async with database.session() as s:
        rel = await HistoryRepository(s).type_reliability("CEX_CEX", "binance", "okx")
    assert rel == 50.0   # 2 good of 4; old metric would have said 25.0


async def test_reliability_defaults_without_history(database):
    async with database.session() as s:
        rel = await HistoryRepository(s).type_reliability("CEX_CEX", "nowhere", "never")
    assert rel == 60.0
