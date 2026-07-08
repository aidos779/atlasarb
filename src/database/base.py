"""Database engine, session factory, and declarative base (async SQLAlchemy 2.0).

Connection pooling is configured from settings (NFR-SCALE). Sessions are provided via
an async context manager consumed by repositories. Encryption-at-rest (NFR-SEC-06) is a
deployment/storage concern (e.g., Postgres TDE / disk encryption) layered beneath this.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.config.settings import Settings


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings: Settings) -> None:
        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if not settings.database_url.startswith("sqlite"):
            kwargs.update(
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
            )
        self._engine: AsyncEngine = create_async_engine(settings.database_url, **kwargs)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        # Import models so metadata is populated before create.
        from src.database import models  # noqa: F401
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()
