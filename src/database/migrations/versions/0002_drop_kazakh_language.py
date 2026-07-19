"""retire Kazakh (kk) UI language — migrate affected users to Russian

The bot now ships en/ru only. `Language` no longer has a KK member, so a row still
holding 'kk' would raise ValueError in UserRepository._to_domain the moment that user
sent a message — every request from them failing, not just their language rendering.

Russian (not English) is the target: kk users are overwhelmingly ru-literate, so it is
the least disruptive landing place. Users can switch in Settings at any time.

The downgrade is intentionally not a restore: once these rows read 'ru' there is no
record of which were originally 'kk', so reversing would have to guess. Downgrading the
schema is safe; the language values simply stay 'ru'.

Revision ID: 0002_drop_kazakh_language
Revises: 0001_initial
Create Date: 2026-07-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_drop_kazakh_language"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE user_settings SET language = 'ru' WHERE language = 'kk'")
    )


def downgrade() -> None:
    # No-op: the original kk assignments are not recoverable (see module docstring).
    pass
