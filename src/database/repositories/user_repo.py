"""User repository — persistence for the User aggregate (profile, subscription,
settings, filter) with mapping to/from domain objects (Clean Architecture boundary).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models import Subscription as SubRow
from src.database.models import User, UserFilterRow, UserSettingsRow
from src.domain.enums import (
    ArbitrageType,
    Currency,
    Language,
    SubscriptionStatus,
    SubscriptionTier,
    UserRole,
)
from src.domain.user import (
    Subscription,
    UserFilter,
    UserProfile,
    UserSettings,
)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> UserProfile | None:
        row = await self._load(user_id)
        return self._to_domain(row) if row else None

    async def _load(self, user_id: int) -> User | None:
        stmt = (
            select(User)
            .where(User.telegram_user_id == user_id)
            .options(
                selectinload(User.subscription),
                selectinload(User.settings),
                selectinload(User.user_filter),
            )
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(self, user_id: int, username: str | None,
                            first_name: str | None) -> UserProfile:
        row = await self._load(user_id)
        if row is None:
            row = User(
                telegram_user_id=user_id, username=username, first_name=first_name,
                role=UserRole.VISITOR.value, onboarding_step=0,
            )
            row.subscription = SubRow()
            row.settings = UserSettingsRow()
            row.user_filter = UserFilterRow()
            self._session.add(row)
            await self._session.flush()
        else:
            if username and row.username != username:
                row.username = username
            if first_name:
                row.first_name = first_name
        return self._to_domain(row)

    async def save_profile(self, profile: UserProfile) -> None:
        row = await self._load(profile.telegram_user_id)
        if row is None:
            return
        row.role = profile.role.value
        row.onboarding_step = profile.onboarding_step
        row.suspended = profile.suspended
        row.pending_deeplink = profile.pending_deeplink
        s = profile.settings
        row.settings.language = s.language.value
        row.settings.timezone = s.timezone
        row.settings.currency = s.currency.value
        row.settings.daily_summary_enabled = s.daily_summary_enabled
        row.settings.daily_summary_time = s.daily_summary_time
        row.settings.instant_alerts_enabled = s.instant_alerts_enabled
        row.settings.favorite_coin_alerts = s.favorite_coin_alerts
        row.settings.favorite_exchange_alerts = s.favorite_exchange_alerts
        f = profile.filter
        row.user_filter.min_profit_pct = float(f.min_profit_pct)
        row.user_filter.coins = sorted(f.coins)
        row.user_filter.exchanges = sorted(f.exchanges)
        row.user_filter.networks = sorted(f.networks)
        row.user_filter.min_liquidity_usd = float(f.min_liquidity_usd)
        row.user_filter.max_risk_numeric = f.max_risk_numeric
        row.user_filter.max_signal_age_sec = f.max_signal_age_sec
        row.user_filter.arb_types = [t.value for t in f.arb_types]
        row.user_filter.scan_all_assets = f.scan_all_assets
        sub = profile.subscription
        row.subscription.tier = sub.tier.value
        row.subscription.status = sub.status.value
        row.subscription.auto_renew = sub.auto_renew
        row.subscription.retries_used = sub.retries_used
        row.subscription.period_end = (
            datetime.fromtimestamp(sub.period_end, tz=UTC)
            if sub.period_end else None
        )

    async def set_role(self, user_id: int, role: UserRole) -> None:
        row = await self._load(user_id)
        if row:
            row.role = role.value

    async def find_by_username(self, username: str) -> UserProfile | None:
        stmt = select(User).where(User.username == username.lstrip("@")).options(
            selectinload(User.subscription), selectinload(User.settings),
            selectinload(User.user_filter))
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(row) if row else None

    async def increment_signals_viewed(self, user_id: int) -> None:
        row = await self._load(user_id)
        if row:
            row.signals_viewed_month += 1

    async def alert_candidates(self) -> list[UserProfile]:
        """Onboarded, non-suspended users eligible to receive instant alerts.

        For 100k+ users this pass is sharded (NFR-SCALE-01, documented future extension);
        the MVP evaluates eligibility in-process.
        """
        stmt = (
            select(User)
            .where(User.onboarding_step >= 3, User.suspended.is_(False))
            .options(
                selectinload(User.subscription),
                selectinload(User.settings),
                selectinload(User.user_filter),
            )
        )
        rows = (await self._session.execute(stmt)).scalars()
        return [self._to_domain(r) for r in rows]

    async def due_renewals(self, before_ts: float) -> list[int]:
        from datetime import datetime
        cutoff = datetime.fromtimestamp(before_ts, tz=UTC)
        stmt = select(SubRow.user_id).where(
            SubRow.tier != SubscriptionTier.FREE.value,
            SubRow.period_end.is_not(None),
            SubRow.period_end <= cutoff,
        )
        return [uid for uid in (await self._session.execute(stmt)).scalars()]

    async def daily_summary_users(self) -> list[UserProfile]:
        stmt = (
            select(User)
            .join(UserSettingsRow)
            .where(User.onboarding_step >= 3,
                   UserSettingsRow.daily_summary_enabled.is_(True))
            .options(
                selectinload(User.subscription),
                selectinload(User.settings),
                selectinload(User.user_filter),
            )
        )
        rows = (await self._session.execute(stmt)).scalars()
        return [self._to_domain(r) for r in rows]

    # ── mapping ──
    def _to_domain(self, row: User) -> UserProfile:
        sub = row.subscription or SubRow()
        st = row.settings or UserSettingsRow()
        fl = row.user_filter or UserFilterRow()
        subscription = Subscription(
            tier=SubscriptionTier(sub.tier),
            status=SubscriptionStatus(sub.status),
            period_end=sub.period_end.timestamp() if sub.period_end else None,
            auto_renew=sub.auto_renew, retries_used=sub.retries_used,
        )
        settings = UserSettings(
            language=Language(st.language), timezone=st.timezone,
            currency=Currency(st.currency),
            daily_summary_enabled=st.daily_summary_enabled,
            daily_summary_time=st.daily_summary_time,
            instant_alerts_enabled=st.instant_alerts_enabled,
            favorite_coin_alerts=st.favorite_coin_alerts,
            favorite_exchange_alerts=st.favorite_exchange_alerts,
        )
        user_filter = UserFilter(
            min_profit_pct=Decimal(str(fl.min_profit_pct)),
            coins=frozenset(fl.coins or []),
            exchanges=frozenset(fl.exchanges or []),
            networks=frozenset(fl.networks or []),
            min_liquidity_usd=Decimal(str(fl.min_liquidity_usd)),
            max_risk_numeric=fl.max_risk_numeric,
            max_signal_age_sec=fl.max_signal_age_sec,
            arb_types=frozenset(ArbitrageType(t) for t in (fl.arb_types or [])),
            scan_all_assets=fl.scan_all_assets,
        )
        return UserProfile(
            telegram_user_id=row.telegram_user_id, username=row.username,
            first_name=row.first_name, role=UserRole(row.role),
            subscription=subscription, settings=settings, filter=user_filter,
            onboarding_step=row.onboarding_step,
            created_at=row.created_at.timestamp() if row.created_at else None,
            suspended=row.suspended, pending_deeplink=row.pending_deeplink,
        )
