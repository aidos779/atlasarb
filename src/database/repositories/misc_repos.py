"""Remaining repositories: billing events, support tickets, admin audit/broadcast,
notification log + mutes + per-user alert cooldown (PRD §16, §13, §18)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    AlertCooldown,
    AuditLog,
    BroadcastJob,
    MutedPair,
    NotificationLog,
    SubscriptionEvent,
    SupportTicket,
    TicketMessage,
)


class BillingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, user_id: int, event_type: str, tier: str,
                     amount_usd: float = 0.0, detail: str | None = None) -> None:
        self._session.add(SubscriptionEvent(
            user_id=user_id, event_type=event_type, tier=tier,
            amount_usd=amount_usd, detail=detail))

    async def mrr(self) -> float:
        stmt = select(func.coalesce(func.sum(SubscriptionEvent.amount_usd), 0)).where(
            SubscriptionEvent.event_type.in_(("purchase", "renewal")))
        return float((await self._session.execute(stmt)).scalar_one())


class SupportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_ticket(self, user_id: int, subject: str, body: str) -> SupportTicket:
        ticket = SupportTicket(user_id=user_id, subject=subject[:200])
        ticket.messages.append(TicketMessage(author_id=user_id, is_staff=False, body=body))
        self._session.add(ticket)
        await self._session.flush()
        return ticket

    async def open_tickets(self) -> list[SupportTicket]:
        stmt = select(SupportTicket).where(SupportTicket.status != "resolved").order_by(
            SupportTicket.created_at)
        return list((await self._session.execute(stmt)).scalars())

    async def add_reply(self, ticket_id: int, author_id: int, body: str,
                        is_staff: bool) -> None:
        self._session.add(TicketMessage(
            ticket_id=ticket_id, author_id=author_id, is_staff=is_staff, body=body))

    async def set_status(self, ticket_id: int, status: str,
                         assigned_to: int | None = None) -> None:
        ticket = await self._session.get(SupportTicket, ticket_id)
        if ticket:
            ticket.status = status
            if assigned_to is not None:
                ticket.assigned_to = assigned_to


class AuditRepository:
    """Append-only (NFR-SEC-05) — no update/delete methods exist by design."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, admin_id: int, action: str, target_user_id: int | None,
                     reason: str, before: dict, after: dict) -> None:
        self._session.add(AuditLog(
            admin_id=admin_id, action=action, target_user_id=target_user_id,
            reason=reason, before_state=before, after_state=after))

    async def for_user(self, target_user_id: int, limit: int = 20) -> list[AuditLog]:
        stmt = select(AuditLog).where(
            AuditLog.target_user_id == target_user_id
        ).order_by(AuditLog.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())


class BroadcastRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, admin_id: int, target_type: str, target_value: str | None,
                     body: str, is_template: bool) -> BroadcastJob:
        job = BroadcastJob(admin_id=admin_id, target_type=target_type,
                           target_value=target_value, body=body, is_template=is_template)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get(self, job_id: int) -> BroadcastJob | None:
        return await self._session.get(BroadcastJob, job_id)

    async def add_confirmation(self, job_id: int, admin_id: int) -> BroadcastJob | None:
        job = await self.get(job_id)
        if job and admin_id not in (job.confirmations or []):
            job.confirmations = [*(job.confirmations or []), admin_id]
        return job


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(self, user_id: int, notif_type: str, title: str) -> None:
        self._session.add(NotificationLog(
            user_id=user_id, notif_type=notif_type, title=title[:200]))

    async def recent(self, user_id: int, limit: int = 20) -> list[NotificationLog]:
        stmt = select(NotificationLog).where(
            NotificationLog.user_id == user_id
        ).order_by(NotificationLog.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def alerts_in_last_hour(self, user_id: int, since: datetime) -> int:
        stmt = select(func.count()).select_from(NotificationLog).where(
            NotificationLog.user_id == user_id,
            NotificationLog.notif_type == "instant_alert",
            NotificationLog.created_at >= since)
        return int((await self._session.execute(stmt)).scalar_one())

    # ── mutes (§13.5) ──
    async def mute_pair(self, user_id: int, pair: str, until: datetime) -> None:
        existing = await self._session.execute(select(MutedPair).where(
            MutedPair.user_id == user_id, MutedPair.pair == pair))
        row = existing.scalar_one_or_none()
        if row:
            row.until = until
        else:
            self._session.add(MutedPair(user_id=user_id, pair=pair, until=until))

    async def is_muted(self, user_id: int, pair: str, now: datetime) -> bool:
        # Honors both a per-pair mute and the global "*ALL*" pause (§13.5).
        stmt = select(MutedPair).where(
            MutedPair.user_id == user_id,
            MutedPair.pair.in_((pair, "*ALL*")),
            MutedPair.until > now)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def purge_expired_mutes(self, now: datetime) -> None:
        await self._session.execute(delete(MutedPair).where(MutedPair.until <= now))

    # ── per-user alert cooldown (§18) ──
    async def get_cooldown(self, user_id: int, dedup_key: str) -> AlertCooldown | None:
        stmt = select(AlertCooldown).where(
            AlertCooldown.user_id == user_id, AlertCooldown.dedup_key == dedup_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def set_cooldown(self, user_id: int, dedup_key: str, net_pct: float,
                           until: datetime) -> None:
        row = await self.get_cooldown(user_id, dedup_key)
        if row:
            row.last_net_pct = net_pct
            row.until = until
        else:
            self._session.add(AlertCooldown(
                user_id=user_id, dedup_key=dedup_key, last_net_pct=net_pct, until=until))
