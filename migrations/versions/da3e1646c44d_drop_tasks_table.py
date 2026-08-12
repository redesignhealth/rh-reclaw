"""drop tasks table

Revision ID: da3e1646c44d
Revises: 15ef34885e30
Create Date: 2026-08-12 14:42:56.308473

TECH-5118 phase 3 ("tasks-as-conversations"): drops the dedicated `tasks`
table and `audit_log.task_id` FK/index added by 6d2a8e63e469 (TECH-5094),
now that task lifecycle is represented as a conversation carrying
`task_assign`/`task_report`/`task_complete`/`task_decline`/`task_cancel`
messages instead. 6d2a8e63e469 is already applied on `main`/dev (confirmed:
its own `alembic_version` row exists in the local dev DB), so per that
revision's own frozen-file note, this is a NEW revision, not an edit to
6d2a8e63e469 in place.

NOTE on in-place amendment: authored and iterated on entirely within this
single unmerged PR (TECH-5118) -- it does not exist on `main`, and has
never been applied to any persistent or shared database. In-place
amendment during code review is therefore safe. Once this PR merges, treat
this file as frozen: any further schema change requires a NEW Alembic
revision, never an edit to this one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "da3e1646c44d"
down_revision: str | None = "15ef34885e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("idx_tasks_assignee_id_status", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_created_at_id", table_name="tasks", if_exists=True)
    op.drop_index("idx_tasks_created_by_status", table_name="tasks", if_exists=True)
    op.drop_index("idx_audit_log_task_id", table_name="audit_log", if_exists=True)
    # Raw SQL for IF EXISTS guards — op.drop_constraint/op.drop_column/op.drop_table
    # have no native IF EXISTS support, and this migration may run on a DB where
    # 6d2a8e63e469 was never applied (e.g. a fresh dev environment).
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_task_id_fkey")
    op.execute("ALTER TABLE audit_log DROP COLUMN IF EXISTS task_id")
    op.execute("DROP TABLE IF EXISTS tasks")


def downgrade() -> None:
    # WARNING: this downgrade is permanently lossy — all task data (now
    # represented as conversations with task_assign/etc messages) is
    # unrecoverable by reverting this migration. Downgrade is provided for
    # schema completeness only, not as a data-recovery path.
    op.create_table(
        "tasks",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("assignee_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column(
            "status",
            sa.TEXT(),
            server_default=sa.text("'open'::text"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("schema_version", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        # Use the original IN(...) form to match 6d2a8e63e469 exactly and
        # avoid schema-drift noise from autogenerate's array normalization.
        sa.CheckConstraint(
            "status IN ('open', 'done', 'declined')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint("created_by <> assignee_id", name="ck_tasks_distinct_parties"),
        sa.ForeignKeyConstraint(["assignee_id"], ["agents.id"], name="tasks_assignee_id_fkey"),
        sa.ForeignKeyConstraint(["created_by"], ["agents.id"], name="tasks_created_by_fkey"),
        sa.PrimaryKeyConstraint("id", name="tasks_pkey"),
    )
    op.create_index(
        "idx_tasks_created_by_status",
        "tasks",
        ["created_by", "status"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "idx_tasks_created_at_id",
        "tasks",
        [sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "idx_tasks_assignee_id_status",
        "tasks",
        ["assignee_id", "status"],
        unique=False,
        if_not_exists=True,
    )
    op.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS task_id UUID REFERENCES tasks(id)")
    op.create_index(
        "idx_audit_log_task_id", "audit_log", ["task_id"], unique=False, if_not_exists=True
    )
