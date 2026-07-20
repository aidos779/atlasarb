"""purchase lifecycle table

Adds `purchases`: one row per attempt to buy a product, carrying the state machine
(created/pending/paid/failed/cancelled/expired) that a payment provider drives.

`external_payment_id` is unique when set. That constraint is the reason a replayed
provider callback is harmless: it resolves to the purchase it already settled instead
of opening a second one. The purchase id itself is a UUID we generate and hand to the
provider as the invoice payload, so a callback can be matched either way.

Guarded by schema inspection for the same reason as 0003: 0001_initial builds the schema
with `Base.metadata.create_all` from the *current* models, so a database created after
this change already has the table and must see this revision as a no-op.

Downgrade drops the table. Purchase history is not recoverable afterwards — the
subscription_events ledger still records what was granted and for how much.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_purchases"
down_revision = "0003_two_plan_lifetime"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("purchases"):
        return
    op.create_table(
        "purchases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("product_code", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="created"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("external_payment_id", sa.String(length=128), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("external_payment_id", name="uq_purchase_external_id"),
    )
    op.create_index("ix_purchases_user_id", "purchases", ["user_id"])
    op.create_index("ix_purchase_user_created", "purchases", ["user_id", "created_at"])
    op.create_index("ix_purchase_status", "purchases", ["status"])


def downgrade() -> None:
    if not _has_table("purchases"):
        return
    op.drop_index("ix_purchase_status", table_name="purchases")
    op.drop_index("ix_purchase_user_created", table_name="purchases")
    op.drop_index("ix_purchases_user_id", table_name="purchases")
    op.drop_table("purchases")
