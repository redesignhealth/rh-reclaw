"""partial index for case-insensitive active-agent email lookup

Revision ID: bb1ea7d2a0cf
Revises: e1db7c2e6b70
Create Date: 2026-08-13 16:50:57.343015

TECH-5159: backs ``service.lookup_agent_by_email``'s
``func.lower(Agent.owner_email) == normalized AND Agent.status == "active"``
query, added the same round as this migration. Without it, that query
sequential-scans ``agents`` on every call -- fine at today's table size, but
worth having the index in place before ``comms_lookup_agent_by_email`` sees
real traffic rather than adding it reactively later.

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb1ea7d2a0cf"
down_revision: str | None = "e1db7c2e6b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Expression index on lower(owner_email), partial on status = 'active':
    # matches the exact predicate lookup_agent_by_email filters on, so the
    # planner can satisfy both the equality and the status filter from the
    # index alone. op.create_index doesn't support Postgres expression
    # indexes or WHERE clauses directly, so this is raw DDL.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_lower_owner_email_active "
        "ON agents (lower(owner_email)) WHERE status = 'active'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agents_lower_owner_email_active")
