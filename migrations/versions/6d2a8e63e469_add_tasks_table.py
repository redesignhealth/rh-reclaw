"""add tasks table

Revision ID: 6d2a8e63e469
Revises: 18f2d7735523
Create Date: 2026-08-12 00:09:33.389567

NOTE on in-place amendment (mirrors migrations/versions/18f2d7735523's own
note): this revision has been authored and iterated on entirely within
this single unmerged PR (reclaw-comms-mcp PR #9, TECH-5094) — it does not
exist on `main`, and it has never been applied to any persistent or shared
database. CI runs `alembic upgrade head` against a fresh, ephemeral
Postgres service container on every run; local review testing always ran
full `downgrade base` -> `upgrade head` cycles against the latest file
content. In-place amendment during code review is therefore safe — there
is no environment where this revision is recorded as already-applied in a
state older than the one below. Once this PR merges, treat this file as
frozen: any further schema change requires a NEW Alembic revision, never
an edit to this one.

Every operation below is written to be idempotent in EITHER direction
(if_not_exists on every create, if_exists on every corresponding drop,
raw `ADD COLUMN IF NOT EXISTS`/`DROP COLUMN IF EXISTS` SQL for the one
column add op.add_column/op.drop_column have no such flag for) —
belt-and-suspenders per Argus round 3's request, on top of the in-place-
amendment rationale above, so re-running either direction against a
partially-applied state never aborts. The two FK/CHECK constraint adds
are the sole exception: Postgres has no `ADD CONSTRAINT IF NOT EXISTS`
syntax at all (not a gap in this file), matching the same non-idempotent
constraint-add precedent already set by ef8394b37c8d/18f2d7735523.

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6d2a8e63e469"
down_revision: str | None = "18f2d7735523"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("assignee_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('open', 'done', 'declined')", name="ck_tasks_status"),
        sa.CheckConstraint("created_by <> assignee_id", name="ck_tasks_distinct_parties"),
        sa.ForeignKeyConstraint(["created_by"], ["agents.id"]),
        sa.ForeignKeyConstraint(["assignee_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_tasks_assignee_id_status",
        "tasks",
        ["assignee_id", "status"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "idx_tasks_created_by_status",
        "tasks",
        ["created_by", "status"],
        unique=False,
        if_not_exists=True,
    )
    # Descending on both columns to match get_tasks's ORDER BY created_at
    # DESC, id DESC (newest-first pagination) — a forward scan of a DESC
    # index serves that query directly, rather than relying on Postgres's
    # ability to walk an ascending index backward.
    op.create_index(
        "idx_tasks_created_at_id",
        "tasks",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
        if_not_exists=True,
    )
    op.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS task_id UUID")
    op.create_foreign_key("audit_log_task_id_fkey", "audit_log", "tasks", ["task_id"], ["id"])
    op.create_index(
        "idx_audit_log_task_id", "audit_log", ["task_id"], unique=False, if_not_exists=True
    )


def downgrade() -> None:
    op.drop_index("idx_audit_log_task_id", table_name="audit_log", if_exists=True)
    op.drop_constraint("audit_log_task_id_fkey", "audit_log", type_="foreignkey")
    op.execute("ALTER TABLE audit_log DROP COLUMN IF EXISTS task_id")
    op.drop_index("idx_tasks_created_at_id", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_created_by_status", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_assignee_id_status", table_name="tasks", if_exists=True)
    op.drop_table("tasks")
