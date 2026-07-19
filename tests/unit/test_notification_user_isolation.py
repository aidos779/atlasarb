"""Per-user transaction isolation in the dispatch pass (issue #4).

Two guarantees:

1. One user's failure — a duplicate-key race, a bad enum, a dead session — is contained
   to that user. It must not abort, roll back, or short-circuit the pass for anyone else.
2. The Telegram round-trip holds no DB connection. Fanning out over users while each
   pinned a pooled connection across an external HTTP call would exhaust `db_pool_size`
   and stall every other query in the process, including the bot's own handlers.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.config.scanner_config import ScannerConfig
from src.domain.signal import Signal
from src.domain.user import UserProfile
from src.services import notification_service
from src.services.notification_service import NotificationService


class _FakeSession:
    pass


class _FakeDatabase:
    """Tracks how many sessions are open at once and across which awaits."""

    def __init__(self) -> None:
        self.open_now = 0
        self.peak_open = 0
        self.total_opened = 0

    def session(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                db.open_now += 1
                db.total_opened += 1
                db.peak_open = max(db.peak_open, db.open_now)
                return _FakeSession()

            async def __aexit__(self, *exc):
                db.open_now -= 1
                return False

        return _Ctx()


class _FakeNotifRepo:
    """Passes every gate; optionally explodes for one user id."""

    fail_for: set[int] = set()
    db: _FakeDatabase | None = None
    sessions_during_send: list[int] = []

    def __init__(self, session) -> None:
        pass

    async def is_muted(self, uid, pair, now) -> bool:
        if uid in self.fail_for:
            raise RuntimeError(f"simulated DB failure for {uid}")
        return False

    async def get_cooldown(self, uid, dedup):
        return None

    async def alerts_in_last_hour(self, uid, since) -> int:
        return 0

    async def log(self, uid, notif_type, title) -> None:
        return None

    async def set_cooldown(self, uid, dedup, net_pct, until) -> None:
        return None


class _FakeFavRepo:
    def __init__(self, session) -> None:
        pass

    async def values(self, uid, kind):
        return []


class _FakeNotifier:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db
        self.sent: list[int] = []
        self.open_sessions_seen: list[int] = []

    async def send_alert(self, uid, signal, language) -> bool:
        # The decisive observation: how many DB connections are held right now?
        self.open_sessions_seen.append(self._db.open_now)
        self.sent.append(uid)
        return True

    async def send_text(self, uid, text) -> bool:
        return True


@pytest.fixture(autouse=True)
def _patch_repos(monkeypatch):
    _FakeNotifRepo.fail_for = set()
    monkeypatch.setattr(notification_service, "NotificationRepository", _FakeNotifRepo)
    monkeypatch.setattr(notification_service, "FavoritesRepository", _FakeFavRepo)


def _signal() -> Signal:
    return Signal(coin="BTC", trading_pair="BTC/USDT", buy_exchange="binance",
                  sell_exchange="okx", net_profit_pct=Decimal("2.5"),
                  confidence_score=99.0)


def _service(db: _FakeDatabase) -> NotificationService:
    return NotificationService(db, bridge=None, config_provider=ScannerConfig)


async def test_one_user_failure_does_not_abort_the_others():
    db = _FakeDatabase()
    service = _service(db)
    notifier = _FakeNotifier(db)
    service.bind_notifier(notifier)

    profiles = [UserProfile(telegram_user_id=uid) for uid in (1, 2, 3, 4, 5)]
    _FakeNotifRepo.fail_for = {3}

    signal = _signal()
    results = []
    for profile in profiles:
        try:
            results.append(await service._evaluate_user(profile, signal))
        except RuntimeError:
            results.append(False)   # the caller's per-user guard does this

    # Everyone except the failing user was still delivered to.
    assert notifier.sent == [1, 2, 4, 5]
    # And no session leaked from the failed evaluation.
    assert db.open_now == 0


async def test_telegram_send_holds_no_db_connection():
    db = _FakeDatabase()
    service = _service(db)
    notifier = _FakeNotifier(db)
    service.bind_notifier(notifier)

    assert await service._evaluate_user(UserProfile(telegram_user_id=1), _signal())

    # Zero connections held across the external call — the pool-exhaustion fix.
    assert notifier.open_sessions_seen == [0]
    # Two short transactions: read-phase eligibility, then write-phase record.
    assert db.total_opened == 2
    assert db.open_now == 0


async def test_dispatch_pass_isolates_failures_across_concurrent_users():
    """End-to-end through the fan-out path, not just the per-user helper."""
    db = _FakeDatabase()
    service = _service(db)
    notifier = _FakeNotifier(db)
    service.bind_notifier(notifier)
    _FakeNotifRepo.fail_for = {2, 4}

    profiles = [UserProfile(telegram_user_id=uid) for uid in range(1, 7)]

    async def _one(profile):
        try:
            return await service._evaluate_user(profile, _signal())
        except Exception:
            return False

    import asyncio
    results = await asyncio.gather(*(_one(p) for p in profiles))

    assert sum(results) == 4                       # 6 users, 2 failed
    assert sorted(notifier.sent) == [1, 3, 5, 6]
    assert db.open_now == 0                        # no leaked sessions


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
