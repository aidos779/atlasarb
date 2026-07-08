"""Support service (PRD §8.2, §16.8) — ticket creation from the user side."""
from __future__ import annotations

from src.database.base import Database
from src.database.repositories.misc_repos import SupportRepository


class SupportService:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def create_ticket(self, user_id: int, body: str,
                            subject: str = "Support request") -> int:
        async with self._db.session() as session:
            ticket = await SupportRepository(session).create_ticket(user_id, subject, body)
            return ticket.id
