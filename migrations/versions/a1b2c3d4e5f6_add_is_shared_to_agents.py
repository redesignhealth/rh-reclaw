"""add is_shared to agents

Revision ID: a1b2c3d4e5f6
Revises: e1db7c2e6b70
Create Date: 2026-08-14 00:00:00.000000

DEPLOYMENT: this migration must run BEFORE the new application code is
deployed, and the deploy requires stop-then-start (not a rolling deploy):
the new code's ``Agent`` ORM model declares ``is_shared`` unconditionally,
so any container running the new code against a database that has not yet
run this migration will error on every query that touches the ``agents``
table (SELECT of an undefined column). The old code never references
``is_shared``, so it tolerates the column's presence fine — this migration
is safe to apply ahead of the code that needs it, just never after.

Rollback: dropping the column is safe. ``server_default=false`` means every
existing row already has a concrete, non-null value, so a downgrade loses
no data other than the flag itself (which no pre-existing agent could have
set anyway, since the column didn't exist for it to write to).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e1db7c2e6b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "is_shared",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "is_shared")
