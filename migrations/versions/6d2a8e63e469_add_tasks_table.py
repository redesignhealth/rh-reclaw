"""add tasks table

Revision ID: 6d2a8e63e469
Revises: 18f2d7735523
Create Date: 2026-08-12 00:09:33.389567

NOTE on in-place amendment (mirrors migrations/versions/18f2d7735523's own
note): this revision has been authored and iterated on entirely within
this single unmerged PR (reclaw-comms-mcp PR #9, TECH-5094) -- it does not
exist on `main`, and it has never been applied to any persistent or shared
database. CI runs `alembic upgrade head` against a fresh, ephemeral
Postgres service container on every run; local review testing always ran
full `downgrade base` -> `upgrade head` cycles against the latest file
content. In-place amendment during code review (e.g. the `idx_audit_log_
task_id` index added in Argus round 2, rather than splitting it into a new
revision) is therefore safe -- there is no environment where this
revision is recorded as already-applied without that index. Once this PR
merges, treat this file as frozen: any further schema change requires a
NEW Alembic revision, never an edit to this one.

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
    op.create_index(
        "idx_tasks_created_at_id",
        "tasks",
        ["created_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.add_column("audit_log", sa.Column("task_id", sa.UUID(), nullable=True))
    op.create_foreign_key("audit_log_task_id_fkey", "audit_log", "tasks", ["task_id"], ["id"])
    op.create_index(
        "idx_audit_log_task_id", "audit_log", ["task_id"], unique=False, if_not_exists=True
    )


def downgrade() -> None:
    op.drop_index("idx_audit_log_task_id", table_name="audit_log")
    op.drop_constraint("audit_log_task_id_fkey", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "task_id")
    op.drop_index("idx_tasks_created_at_id", table_name="tasks")
    op.drop_index("idx_tasks_created_by_status", table_name="tasks")
    op.drop_index("idx_tasks_assignee_id_status", table_name="tasks")
    op.drop_table("tasks")
