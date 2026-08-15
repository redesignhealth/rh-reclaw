"""add is_shared to agents

Revision ID: a1b2c3d4e5f6
Revises: e1db7c2e6b70
Create Date: 2026-08-14 00:00:00.000000

DEPLOYMENT: no stop-then-start needed (Argus round 2 correction of an
earlier, overstated claim here) -- this is a plain additive, backward-
compatible column with a ``server_default``, and ``entrypoint.sh`` already
runs ``agent-comms-mcp-migrate`` before ``agent-comms-mcp`` starts serving,
atomically, on every container startup. A normal rolling deploy is safe:
each new container migrates-then-serves before taking traffic, and an
old container still running (with code that never references
``is_shared``) tolerates the column's presence fine. The only real
ordering constraint is the one ``entrypoint.sh`` already enforces
per-container: migrate before serve, not a cluster-wide stop-then-start.

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
