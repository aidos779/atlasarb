"""User service — onboarding, identity, role resolution (PRD §5, §21.1, §7.2 /start).

Role is derived, never self-declared (R-ROLE-1): Administrator/Support come from the
backend allow-list in settings; Visitor→Free auto-promotes on onboarding completion
(R-ROLE-2); Paid is derived from subscription state (R-ROLE-3).
"""
from __future__ import annotations

from src.config import get_logger
from src.config.settings import Settings
from src.database.base import Database
from src.database.repositories.user_repo import UserRepository
from src.domain.enums import Currency, Language, UserRole
from src.domain.user import UserProfile

log = get_logger("services.user")


class UserService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self._db = database
        self._settings = settings

    def _allowlist_role(self, user_id: int) -> UserRole | None:
        if user_id in self._settings.admin_user_ids:
            return UserRole.ADMIN
        if user_id in self._settings.support_user_ids:
            return UserRole.SUPPORT
        return None

    async def get_or_create(self, user_id: int, username: str | None,
                            first_name: str | None) -> UserProfile:
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get_or_create(user_id, username, first_name)
            # Enforce allow-list role on every touch (NFR-SEC-04 defense).
            staff_role = self._allowlist_role(user_id)
            if staff_role and profile.role != staff_role:
                profile.role = staff_role
                await repo.save_profile(profile)
            elif not staff_role and profile.role in (UserRole.ADMIN, UserRole.SUPPORT):
                # Removed from allow-list mid-session — demote.
                profile.role = UserRole.FREE if profile.onboarding_complete else UserRole.VISITOR
                await repo.save_profile(profile)
            return profile

    async def get(self, user_id: int) -> UserProfile | None:
        async with self._db.session() as session:
            return await UserRepository(session).get(user_id)

    async def save(self, profile: UserProfile) -> None:
        async with self._db.session() as session:
            await UserRepository(session).save_profile(profile)

    async def set_language(self, profile: UserProfile, language: Language) -> None:
        profile.settings.language = language
        if profile.onboarding_step < 1:
            profile.onboarding_step = 1
        await self.save(profile)

    async def set_timezone(self, profile: UserProfile, timezone: str) -> None:
        profile.settings.timezone = timezone
        if profile.onboarding_step < 2:
            profile.onboarding_step = 2
        await self.save(profile)

    async def set_currency(self, profile: UserProfile, currency: Currency) -> None:
        profile.settings.currency = currency
        completed = profile.onboarding_step < 3
        if completed:
            profile.onboarding_step = 3
            if profile.role == UserRole.VISITOR:
                profile.role = UserRole.FREE  # R-ROLE-2 auto-promote
        await self.save(profile)

    async def set_pending_deeplink(self, profile: UserProfile, payload: str | None) -> None:
        profile.pending_deeplink = payload
        await self.save(profile)

    async def note_signal_viewed(self, user_id: int) -> None:
        async with self._db.session() as session:
            await UserRepository(session).increment_signals_viewed(user_id)
