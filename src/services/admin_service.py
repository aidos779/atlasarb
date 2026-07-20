"""Admin service (PRD §16) — user/subscription management, broadcast, monitoring, support.

Every mutating action writes an immutable audit entry with a mandatory reason
(R-ADMIN-2/NFR-SEC-05). Large broadcasts require a second admin's confirmation
(R-ADMIN-3 two-person rule). Support role is limited to templates + tickets (FR-ADM-04).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from src.config import get_logger
from src.database.base import Database
from src.database.repositories.misc_repos import (
    AuditRepository,
    BillingRepository,
    BroadcastRepository,
    SupportRepository,
)
from src.database.repositories.user_repo import UserRepository
from src.domain.enums import SubscriptionTier, UserRole
from src.domain.user import UserFilter, UserProfile
from src.scanner.engine import ScanningEngine

log = get_logger("services.admin")

_LARGE_BROADCAST_THRESHOLD = 10_000


@dataclass
class BroadcastCreation:
    job_id: int
    recipients: int
    needs_second_admin: bool


class AdminService:
    def __init__(self, database: Database, engine: ScanningEngine) -> None:
        self._db = database
        self._engine = engine

    # ── User Management (§16.2) ──
    async def lookup(self, query: str) -> UserProfile | None:
        async with self._db.session() as session:
            repo = UserRepository(session)
            if query.lstrip("-").isdigit():
                return await repo.get(int(query))
            return await repo.find_by_username(query)

    async def set_suspended(self, admin_id: int, user_id: int, suspended: bool,
                            reason: str) -> bool:
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                return False
            before = {"suspended": profile.suspended}
            profile.suspended = suspended
            await repo.save_profile(profile)
            await AuditRepository(session).record(
                admin_id, "suspend" if suspended else "reactivate", user_id, reason,
                before, {"suspended": suspended})
            return True

    async def override_tier(self, admin_id: int, user_id: int, tier: SubscriptionTier,
                            reason: str) -> bool:
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                return False
            before = {"tier": profile.subscription.tier.value}
            profile.subscription.tier = tier
            if tier == SubscriptionTier.PRO_LIFETIME and not profile.subscription.purchased_at:
                profile.subscription.purchased_at = time.time()
            if profile.role not in (UserRole.ADMIN, UserRole.SUPPORT):
                profile.role = UserRole.PAID if tier != SubscriptionTier.FREE else UserRole.FREE
            await repo.save_profile(profile)
            await AuditRepository(session).record(
                admin_id, "tier_override", user_id, reason, before, {"tier": tier.value})
            # Comped, not bought: no external_payment_id and no revenue attached, so an
            # admin grant can never be mistaken for a paid conversion.
            await BillingRepository(session).record(user_id, "comp", tier.value)
            return True

    async def grant_pro(self, admin_id: int, user_id: int, reason: str) -> bool:
        """Comp Pro Lifetime. Intent-revealing alias over the audited tier override —
        the grant path itself is not duplicated."""
        return await self.override_tier(
            admin_id, user_id, SubscriptionTier.PRO_LIFETIME, reason)

    async def revoke_pro(self, admin_id: int, user_id: int, reason: str) -> bool:
        """Return a user to Free. Their delivered-signal ledger is untouched, so a
        previously exhausted Free user stays exhausted rather than getting a fresh 5."""
        return await self.override_tier(admin_id, user_id, SubscriptionTier.FREE, reason)

    async def reset_filters(self, admin_id: int, user_id: int, reason: str) -> bool:
        async with self._db.session() as session:
            repo = UserRepository(session)
            profile = await repo.get(user_id)
            if profile is None:
                return False
            profile.filter = UserFilter()
            await repo.save_profile(profile)
            await AuditRepository(session).record(
                admin_id, "reset_filters", user_id, reason, {}, {})
            return True

    async def audit_for(self, user_id: int) -> list:
        async with self._db.session() as session:
            return await AuditRepository(session).for_user(user_id)

    # ── Broadcast (§16.4) ──
    async def create_broadcast(self, admin_id: int, target_type: str,
                               target_value: str | None, body: str,
                               is_template: bool, is_support: bool) -> BroadcastCreation:
        async with self._db.session() as session:
            job = await BroadcastRepository(session).create(
                admin_id, target_type, target_value, body, is_template)
            recipients = await self._count_recipients(session, target_type, target_value)
            job.recipients = recipients
            needs_second = (target_type == "all"
                            and recipients >= _LARGE_BROADCAST_THRESHOLD
                            and not is_support)
            return BroadcastCreation(job.id, recipients, needs_second)

    async def confirm_broadcast(self, job_id: int, admin_id: int) -> bool:
        """Returns True when the job has enough confirmations to send."""
        async with self._db.session() as session:
            repo = BroadcastRepository(session)
            job = await repo.add_confirmation(job_id, admin_id)
            if job is None:
                return False
            needs_second = (job.target_type == "all"
                            and job.recipients >= _LARGE_BROADCAST_THRESHOLD)
            required = 2 if needs_second else 1
            if len(job.confirmations or []) >= required:
                job.status = "approved"
                return True
            return False

    async def recipients_for(self, job_id: int) -> list[int]:
        async with self._db.session() as session:
            job = await BroadcastRepository(session).get(job_id)
            if job is None or job.status != "approved":
                return []
            job.status = "sent"
            return await self._resolve_recipients(session, job.target_type, job.target_value)

    async def _count_recipients(self, session, target_type, target_value) -> int:
        return len(await self._resolve_recipients(session, target_type, target_value))

    async def _resolve_recipients(self, session, target_type, target_value) -> list[int]:
        repo = UserRepository(session)
        users = await repo.alert_candidates()
        if target_type == "tier" and target_value:
            users = [u for u in users if u.effective_tier.value == target_value]
        elif target_type == "language" and target_value:
            users = [u for u in users if u.settings.language.value == target_value]
        return [u.telegram_user_id for u in users]

    # ── Signal Monitoring (§16.6) ──
    def monitoring(self) -> dict:
        return {
            "exchange_status": {v: s.value for v, s in self._engine.status_table().items()},
            "metrics": self._engine.metrics.snapshot(),
            "active_signals": len(self._engine.active_signals()),
        }

    def kill_switch(self, venue: str, disabled: bool) -> None:
        self._engine.kill_switch(venue, disabled)

    # ── Support Queue (§16.8) ──
    async def open_tickets(self) -> list:
        async with self._db.session() as session:
            return await SupportRepository(session).open_tickets()

    async def reply_ticket(self, ticket_id: int, admin_id: int, body: str) -> int | None:
        async with self._db.session() as session:
            support = SupportRepository(session)
            tickets = {t.id: t for t in await support.open_tickets()}
            ticket = tickets.get(ticket_id)
            if ticket is None:
                return None
            await support.add_reply(ticket_id, admin_id, body, is_staff=True)
            await support.set_status(ticket_id, "in_progress", assigned_to=admin_id)
            # R-ADMIN-4 — support replies visible in user activity/audit history.
            await AuditRepository(session).record(
                admin_id, "support_reply", ticket.user_id, body[:200], {}, {})
            return ticket.user_id

    async def revenue_usd(self) -> float:
        async with self._db.session() as session:
            return await BillingRepository(session).revenue_usd()
