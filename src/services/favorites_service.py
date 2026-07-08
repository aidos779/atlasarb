"""Favorites service (PRD §14.3, BR-FAV-1) — tier-capped add/remove with upsell signal.

Returns a structured outcome so handlers can show an upsell prompt on cap breach rather
than silently failing (BR-FAV-1 / R-MENU-1).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.database.base import Database
from src.database.repositories.favorites_repo import FavoritesRepository
from src.domain.entitlements import entitlements_for
from src.domain.user import UserProfile


@dataclass
class FavoriteOutcome:
    ok: bool
    added: bool
    cap_reached: bool = False
    kind: str = ""


class FavoritesService:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def toggle(self, profile: UserProfile, kind: str, value: str) -> FavoriteOutcome:
        ent = entitlements_for(profile.effective_tier)
        async with self._db.session() as session:
            repo = FavoritesRepository(session)
            if await repo.exists(profile.telegram_user_id, kind, value):
                await repo.remove(profile.telegram_user_id, kind, value)
                return FavoriteOutcome(ok=True, added=False, kind=kind)
            count = await repo.count(profile.telegram_user_id, kind)
            if not ent.can_add_favorite(kind, count):
                return FavoriteOutcome(ok=False, added=False, cap_reached=True, kind=kind)
            await repo.add(profile.telegram_user_id, kind, value)
            return FavoriteOutcome(ok=True, added=True, kind=kind)

    async def list(self, user_id: int, kind: str) -> list:
        async with self._db.session() as session:
            return await FavoritesRepository(session).list(user_id, kind)

    async def values(self, user_id: int, kind: str) -> list[str]:
        async with self._db.session() as session:
            return await FavoritesRepository(session).values(user_id, kind)
