"""crypto payment provider: payment_events table + purchase asset/payload columns

Adds what the Telegram Crypto Pay integration needs on top of the purchase lifecycle:

* ``payment_events`` — append-only webhook history. One row per inbound provider callback
  (and per polling reconciliation), storing the raw payload, whether the signature
  verified, and whether it drove an activation. ``(provider, update_id)`` is unique: the
  duplicate-webhook guard, so a redelivered signed update cannot be recorded — or granted
  — twice. ``update_id`` is NULL for a poll (NULLs are distinct under the constraint).
* ``purchases.asset`` — the crypto asset a payer actually settled in (USDT/TON/BTC…).
* ``purchases.provider_payload`` — last provider snapshot of the invoice, for support/audit.

Guarded by schema inspection like 0003/0004: 0001_initial builds the schema with
``Base.metadata.create_all`` from the *current* models, so a database created after this
change already has the table/columns and must see this revision as a no-op.

Downgrade drops the table and columns. Webhook history is not recoverable afterwards.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_crypto_payments"
down_revision = "0004_purchases"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    if _has_table("purchases"):
        if not _has_column("purchases", "asset"):
            op.add_column("purchases",
                          sa.Column("asset", sa.String(length=16), nullable=True))
        if not _has_column("purchases", "provider_payload"):
            op.add_column("purchases",
                          sa.Column("provider_payload", sa.JSON(), nullable=True))

    if not _has_table("payment_events"):
        op.create_table(
            "payment_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("update_id", sa.BigInteger(), nullable=True),
            sa.Column("invoice_id", sa.String(length=64), nullable=True),
            sa.Column("purchase_id", sa.String(length=36), nullable=True),
            sa.Column("event_type", sa.String(length=32), nullable=False,
                      server_default=""),
            sa.Column("signature_valid", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("processed", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("provider", "update_id",
                                name="uq_payment_event_update"),
        )
        op.create_index("ix_payment_event_invoice", "payment_events", ["invoice_id"])
        op.create_index("ix_payment_event_purchase", "payment_events", ["purchase_id"])
        op.create_index("ix_payment_event_created", "payment_events", ["created_at"])


def downgrade() -> None:
    if _has_table("payment_events"):
        op.drop_index("ix_payment_event_created", table_name="payment_events")
        op.drop_index("ix_payment_event_purchase", table_name="payment_events")
        op.drop_index("ix_payment_event_invoice", table_name="payment_events")
        op.drop_table("payment_events")
    if _has_table("purchases"):
        if _has_column("purchases", "provider_payload"):
            op.drop_column("purchases", "provider_payload")
        if _has_column("purchases", "asset"):
            op.drop_column("purchases", "asset")
