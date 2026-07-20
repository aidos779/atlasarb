"""two-plan model: Free (5 signals) + Pro Lifetime; retire recurring billing

The product moved from monthly Basic/Pro subscriptions to a single one-time Pro
Lifetime purchase, with Free capped by *delivered signals* rather than by features.

Data decisions:

* Every previously paying row (basic/pro) becomes pro_lifetime. They paid for access
  and the new Pro never expires, so granting it is both the generous and the correct
  reading — and it is what the code now assumes, since `SubscriptionTier` no longer has
  a member for the old values. Anything else lands on free.
* The recurring-billing columns are dropped: with no renewals there is no period to
  end, no charge to retry and no auto-renew to honour. `subscriptions.status` goes with
  them — under a lifetime grant the tier alone is the entitlement.
* `users.signals_viewed_month` is dropped. It was written but never read, never reset,
  and counted *details opens* rather than deliveries; the new quota is backed by the
  signal_deliveries ledger, which is exact.

Every step is guarded by an inspection of the live schema. That is not defensive
padding — 0001_initial creates the schema with ``Base.metadata.create_all`` against the
*current* models, so a database created after this change already has the new shape and
must see this revision as a no-op, while one created before it needs the full migration.
Without the guards, `alembic upgrade head` on a fresh database fails on the first
duplicate column.

The downgrade is intentionally lossy and says so: the dropped columns come back with
defaults, but the original tier/period/renewal state is gone, so every restored row
reads as an active never-expiring 'pro'. Rolling back past this revision is a schema
rollback, not a billing-history restore.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_two_plan_lifetime"
down_revision = "0002_drop_kazakh_language"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    subs = _columns("subscriptions")

    # ── data first: remap tiers while any old values are still present ──
    op.execute(sa.text(
        "UPDATE subscriptions SET tier = 'pro_lifetime' WHERE tier IN ('basic', 'pro')"))
    op.execute(sa.text(
        "UPDATE subscriptions SET tier = 'free' "
        "WHERE tier NOT IN ('free', 'pro_lifetime')"))

    if "purchased_at" not in subs:
        with op.batch_alter_table("subscriptions") as batch:
            batch.add_column(sa.Column("purchased_at", sa.DateTime(timezone=True),
                                       nullable=True))
        # Best-effort provenance for grandfathered rows: they were paying before this
        # migration, so date their purchase at the row's last update.
        op.execute(sa.text(
            "UPDATE subscriptions SET purchased_at = updated_at "
            "WHERE tier = 'pro_lifetime'"))

    retired = [c for c in ("status", "period_end", "auto_renew", "retries_used")
               if c in subs]
    if retired:
        # Indexes must go first. batch_alter_table rebuilds the table and replays the
        # reflected indexes onto the copy, so an index over a column being dropped
        # (subscriptions.status / .period_end were both indexed) fails the rebuild with
        # "no such column" rather than being cleaned up along with it.
        inspector = sa.inspect(op.get_bind())
        for index in inspector.get_indexes("subscriptions"):
            name = index.get("name")
            if name and set(index["column_names"] or ()) & set(retired):
                op.drop_index(name, table_name="subscriptions")
        with op.batch_alter_table("subscriptions") as batch:
            for column in retired:
                batch.drop_column(column)

    if "signals_viewed_month" in _columns("users"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("signals_viewed_month")

    # ── the Free-quota ledger ──
    if not _has_table("signal_deliveries"):
        op.create_table(
            "signal_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("signal_id", sa.String(length=36), nullable=False),
            sa.Column("channel", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "signal_id", name="uq_signal_delivery"),
        )
        op.create_index("ix_signal_deliveries_user_id", "signal_deliveries", ["user_id"])
        op.create_index("ix_signal_delivery_user", "signal_deliveries",
                        ["user_id", "created_at"])

    # ── payment idempotency for the (not yet wired) crypto webhook ──
    if "external_payment_id" not in _columns("subscription_events"):
        with op.batch_alter_table("subscription_events") as batch:
            batch.add_column(sa.Column("external_payment_id", sa.String(length=128),
                                       nullable=True))
            batch.create_unique_constraint("uq_sub_event_payment", ["external_payment_id"])


def downgrade() -> None:
    if "external_payment_id" in _columns("subscription_events"):
        with op.batch_alter_table("subscription_events") as batch:
            batch.drop_constraint("uq_sub_event_payment", type_="unique")
            batch.drop_column("external_payment_id")

    if _has_table("signal_deliveries"):
        op.drop_index("ix_signal_delivery_user", table_name="signal_deliveries")
        op.drop_index("ix_signal_deliveries_user_id", table_name="signal_deliveries")
        op.drop_table("signal_deliveries")

    if "signals_viewed_month" not in _columns("users"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("signals_viewed_month", sa.Integer(),
                                       nullable=False, server_default="0"))

    subs = _columns("subscriptions")
    with op.batch_alter_table("subscriptions") as batch:
        if "status" not in subs:
            batch.add_column(sa.Column("status", sa.String(length=16), nullable=False,
                                       server_default="active"))
        if "period_end" not in subs:
            batch.add_column(sa.Column("period_end", sa.DateTime(timezone=True),
                                       nullable=True))
        if "auto_renew" not in subs:
            batch.add_column(sa.Column("auto_renew", sa.Boolean(), nullable=False,
                                       server_default=sa.true()))
        if "retries_used" not in subs:
            batch.add_column(sa.Column("retries_used", sa.Integer(), nullable=False,
                                       server_default="0"))
        if "purchased_at" in subs:
            batch.drop_column("purchased_at")

    # See the module docstring: the pre-migration tier/period detail is unrecoverable.
    op.execute(sa.text("UPDATE subscriptions SET tier = 'pro' WHERE tier = 'pro_lifetime'"))
