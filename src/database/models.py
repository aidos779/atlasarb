"""ORM models — schema, indexes, constraints, relationships (PRD §25.2 + services).

Money/percentages stored as Numeric to preserve precision. JSON columns hold multi-select
filter sets. Audit log is append-only by policy (NFR-SEC-05) — no update/delete paths exist
in the repositories.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="visitor", index=True)
    onboarding_step: Mapped[int] = mapped_column(Integer, default=0)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_deeplink: Mapped[str | None] = mapped_column(String(128))
    ref_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    subscription: Mapped[Subscription] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan")
    settings: Mapped[UserSettingsRow] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan")
    user_filter: Mapped[UserFilterRow] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan")
    favorites: Mapped[list[Favorite]] = relationship(
        back_populates="user", cascade="all, delete-orphan")


class Subscription(Base):
    """Pro is a one-time lifetime purchase, so the row carries no expiry, auto-renew
    flag or retry counter — those columns were dropped by 0003_two_plan_lifetime.
    """

    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), primary_key=True)
    tier: Mapped[str] = mapped_column(String(16), default="free", index=True)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="subscription")


class UserSettingsRow(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), primary_key=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    timezone: Mapped[str] = mapped_column(String(48), default="UTC")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    daily_summary_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_summary_time: Mapped[str] = mapped_column(String(5), default="08:00")
    instant_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    favorite_coin_alerts: Mapped[bool] = mapped_column(Boolean, default=False)
    favorite_exchange_alerts: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="settings")


class UserFilterRow(Base):
    __tablename__ = "user_filters"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), primary_key=True)
    min_profit_pct: Mapped[float] = mapped_column(Numeric(12, 4), default=0.3)
    coins: Mapped[list] = mapped_column(JSON, default=list)
    exchanges: Mapped[list] = mapped_column(JSON, default=list)
    networks: Mapped[list] = mapped_column(JSON, default=list)
    min_liquidity_usd: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    max_risk_numeric: Mapped[int] = mapped_column(Integer, default=5)
    max_signal_age_sec: Mapped[int] = mapped_column(Integer, default=0)
    arb_types: Mapped[list] = mapped_column(JSON, default=list)
    scan_all_assets: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="user_filter")


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "value", name="uq_favorite"),
        Index("ix_favorite_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))         # coin | exchange | signal
    value: Mapped[str] = mapped_column(String(128))
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)  # BR-SUB-3
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="favorites")


class SignalHistory(Base):
    __tablename__ = "signal_history"
    __table_args__ = (
        Index("ix_signal_history_type_venues", "arb_type", "buy_exchange", "sell_exchange"),
        Index("ix_signal_history_coin", "coin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    arb_type: Mapped[str] = mapped_column(String(16), index=True)
    coin: Mapped[str] = mapped_column(String(32))
    trading_pair: Mapped[str] = mapped_column(String(32))
    network: Mapped[str | None] = mapped_column(String(24))
    buy_exchange: Mapped[str] = mapped_column(String(48))
    sell_exchange: Mapped[str] = mapped_column(String(48))
    buy_price: Mapped[float] = mapped_column(Numeric(30, 10))
    sell_price: Mapped[float] = mapped_column(Numeric(30, 10))
    spread_pct: Mapped[float] = mapped_column(Numeric(12, 4))
    net_profit_pct: Mapped[float] = mapped_column(Numeric(12, 4))
    net_profit_usd: Mapped[float] = mapped_column(Numeric(18, 2))
    liquidity_usd: Mapped[float] = mapped_column(Numeric(18, 2))
    risk_score: Mapped[str] = mapped_column(String(8))
    confidence_score: Mapped[int] = mapped_column(Integer)
    ranking: Mapped[str] = mapped_column(String(8))
    recommended_size_usd: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expiry_reason: Mapped[str | None] = mapped_column(String(32))


class SignalInteraction(Base):
    """Per-user viewed/favorited record → /history lists (PRD §17.4)."""

    __tablename__ = "signal_interactions"
    __table_args__ = (
        Index("ix_interaction_user_kind", "user_id", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    signal_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16))        # viewed | favorited | expired
    coin: Mapped[str] = mapped_column(String(32))
    trading_pair: Mapped[str] = mapped_column(String(32))
    buy_exchange: Mapped[str] = mapped_column(String(48))
    sell_exchange: Mapped[str] = mapped_column(String(48))
    net_profit_pct: Mapped[float] = mapped_column(Numeric(12, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SignalDelivery(Base):
    """Ledger of arbitrage signals actually delivered to a user — the Free quota counter.

    One row per (user, signal) is written *after* a confirmed delivery, never before.
    The unique constraint is what makes the quota exact rather than approximate: a
    re-render of the signal list, a retry, or the same opportunity arriving twice
    reconciles into the existing row instead of burning another free slot.
    """

    __tablename__ = "signal_deliveries"
    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_signal_delivery"),
        Index("ix_signal_delivery_user", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    signal_id: Mapped[str] = mapped_column(String(36))
    channel: Mapped[str] = mapped_column(String(16))      # list | alert
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Purchase(Base):
    """One attempt to buy one product, and the state machine it is in.

    ``external_payment_id`` is unique when set: it is the provider's own reference, and
    the uniqueness is what lets a replayed webhook resolve to the purchase it already
    settled instead of creating a second one.
    """

    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("external_payment_id", name="uq_purchase_external_id"),
        Index("ix_purchase_user_created", "user_id", "created_at"),
        Index("ix_purchase_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_code: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default="created")
    provider: Mapped[str] = mapped_column(String(32), default="")
    #: Provider-scoped id — the Crypto Pay invoice id. Unique when set, which is what
    #: makes a replayed callback resolve to the invoice it already settled.
    external_payment_id: Mapped[str | None] = mapped_column(String(128))
    #: Crypto asset the payer actually settled in (USDT/TON/BTC…), recorded on settlement.
    asset: Mapped[str | None] = mapped_column(String(16))
    #: Last provider snapshot of the invoice (JSON), kept for support/audit.
    provider_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentEvent(Base):
    """Append-only webhook history for the crypto payment provider.

    One row per inbound provider callback (and per polling reconciliation), storing the
    raw payload, whether its signature verified, and whether it drove an activation. It
    is never updated except to flip ``processed`` once, and never deleted — a payment
    dispute is resolved from this table, so webhook history must not be lost.

    ``(provider, update_id)`` is unique, which is the duplicate-webhook guard: a provider
    redelivering the same signed update cannot create a second history row, and the
    pre-insert check short-circuits it as already seen. ``update_id`` is NULL for events
    with no provider update id behind them (a polling reconciliation), and NULLs are
    distinct under the constraint so those never collide.
    """

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider", "update_id", name="uq_payment_event_update"),
        Index("ix_payment_event_invoice", "invoice_id"),
        Index("ix_payment_event_purchase", "purchase_id"),
        Index("ix_payment_event_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))
    update_id: Mapped[int | None] = mapped_column(BigInteger)
    invoice_id: Mapped[str | None] = mapped_column(String(64))
    purchase_id: Mapped[str | None] = mapped_column(String(36))
    event_type: Mapped[str] = mapped_column(String(32), default="")
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SubscriptionEvent(Base):
    __tablename__ = "subscription_events"
    __table_args__ = (
        Index("ix_sub_event_user", "user_id", "created_at"),
        # Idempotency key for payment webhooks: providers deliver at-least-once, so the
        # same confirmed payment can arrive twice. The unique index makes the second
        # insert fail rather than granting (and billing) a second time. NULL for events
        # with no external payment behind them (admin comps).
        UniqueConstraint("external_payment_id", name="uq_sub_event_payment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(32))   # purchase|refund|comp
    tier: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    #: Provider-scoped payment reference, e.g. "cryptobot:12345" / "nowpayments:abc".
    external_payment_id: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    subject: Mapped[str] = mapped_column(String(200))
    assigned_to: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    messages: Mapped[list[TicketMessage]] = relationship(
        cascade="all, delete-orphan", back_populates="ticket")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ticket: Mapped[SupportTicket] = relationship(back_populates="messages")


class AuditLog(Base):
    """Append-only admin audit log (R-ADMIN-2 / NFR-SEC-05)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_target", "target_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(48))
    target_user_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    before_state: Mapped[dict] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BroadcastJob(Base):
    __tablename__ = "broadcast_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    target_type: Mapped[str] = mapped_column(String(24))    # all|tier|language|activity|custom
    target_value: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text)
    button_text: Mapped[str | None] = mapped_column(String(64))
    button_url: Mapped[str | None] = mapped_column(String(256))
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|approved|sent
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    confirmations: Mapped[list] = mapped_column(JSON, default=list)   # admin ids (two-person)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    __table_args__ = (Index("ix_notif_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    notif_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MutedPair(Base):
    __tablename__ = "muted_pairs"
    __table_args__ = (UniqueConstraint("user_id", "pair", name="uq_mute"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    pair: Mapped[str] = mapped_column(String(32))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertCooldown(Base):
    """Per-user Instant Alert cooldown/dedup (PRD §18)."""

    __tablename__ = "alert_cooldowns"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key", name="uq_alert_cooldown"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    dedup_key: Mapped[str] = mapped_column(String(160))
    last_net_pct: Mapped[float] = mapped_column(Numeric(12, 4), default=0)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
