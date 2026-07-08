"""Favorites repository — coins/exchanges/signals with tier-cap enforcement upstream."""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Favorite


class FavoritesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count(self, user_id: int, kind: str, include_frozen: bool = False) -> int:
        stmt = select(func.count()).select_from(Favorite).where(
            Favorite.user_id == user_id, Favorite.kind == kind)
        if not include_frozen:
            stmt = stmt.where(Favorite.frozen.is_(False))
        return int((await self._session.execute(stmt)).scalar_one())

    async def list(self, user_id: int, kind: str) -> list[Favorite]:
        stmt = select(Favorite).where(
            Favorite.user_id == user_id, Favorite.kind == kind
        ).order_by(Favorite.created_at.desc())
        return list((await self._session.execute(stmt)).scalars())

    async def exists(self, user_id: int, kind: str, value: str) -> bool:
        stmt = select(Favorite.id).where(
            Favorite.user_id == user_id, Favorite.kind == kind, Favorite.value == value)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def add(self, user_id: int, kind: str, value: str) -> None:
        if not await self.exists(user_id, kind, value):
            self._session.add(Favorite(user_id=user_id, kind=kind, value=value))
            await self._session.flush()

    async def remove(self, user_id: int, kind: str, value: str) -> None:
        await self._session.execute(delete(Favorite).where(
            Favorite.user_id == user_id, Favorite.kind == kind, Favorite.value == value))

    async def freeze_excess(self, user_id: int, kind: str, keep: int) -> None:
        """BR-SUB-3 — freeze (not delete) favorites beyond the new tier cap."""
        rows = await self.list(user_id, kind)
        for idx, row in enumerate(rows):
            row.frozen = idx >= keep if keep >= 0 else False

    async def unfreeze_all(self, user_id: int, kind: str) -> None:
        for row in await self.list(user_id, kind):
            row.frozen = False

    async def values(self, user_id: int, kind: str, include_frozen: bool = False) -> list[str]:
        rows = await self.list(user_id, kind)
        return [r.value for r in rows if include_frozen or not r.frozen]
