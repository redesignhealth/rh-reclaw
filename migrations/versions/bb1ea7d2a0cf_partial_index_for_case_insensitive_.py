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
    # Expression index on (lower(owner_email), bound_at DESC NULLS LAST),
    # partial on status = 'active': matches lookup_agent_by_email's WHERE
    # predicate AND its ORDER BY bound_at DESC NULLS LAST LIMIT 1, so a
    # multi-active-agent owner_email (an expected, not exceptional, state --
    # see that function's docstring) resolves via a single index scan
    # instead of a heap-fetch-then-sort. op.create_index doesn't support
    # Postgres expression indexes, partial WHERE clauses, or per-column
    # NULLS ordering directly, so this is raw DDL.
    #
    # Not CREATE INDEX CONCURRENTLY: that can't run inside a transaction,
    # and Alembic's env.py wraps every migration in one by default. The
    # ShareLock this acquires blocks writes to `agents` for the duration of
    # the build, which is a negligible window at today's table size --
    # revisit if `agents` grows large enough for that lock to matter.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_lower_owner_email_active "
        "ON agents (lower(owner_email), bound_at DESC NULLS LAST) "
        "WHERE status = 'active'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agents_lower_owner_email_active")
