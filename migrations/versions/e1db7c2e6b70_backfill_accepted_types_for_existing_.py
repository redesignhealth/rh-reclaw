"""backfill accepted_types for existing agents

Revision ID: e1db7c2e6b70
Revises: da3e1646c44d
Create Date: 2026-08-13 10:07:06.090851

TECH-5118-followup: this PR turns ``accepted_types`` from a purely
informational, unenforced declaration (DESIGN.md's own prior wording) into
a real per-message capability gate (``service._enforce_message_type_accepted``)
that denies a send when a recipient hasn't declared the message's type.

Any agent already registered before this PR merges declared its
``accepted_types`` at a time when that value had zero behavioral effect --
narrowing it cost nothing and communicated nothing except intent. Deploying
the new gate without backfilling would silently break every one of those
agents' *incoming* traffic for any type outside whatever narrow list they
happened to register with, the moment this ships -- not a bug in the new
code, but a real production incident caused by enforcing a field
retroactively against declarations made under a different contract.

This is a one-time grandfather clause, not a permanent behavior: it widens
every EXISTING agent row's ``accepted_types`` to the full registered
message-type set as of this revision, so nothing that worked yesterday
breaks today. Any agent registered AFTER this migration runs is unaffected
by it -- `register_agent`'s own validation/defaults apply to those as
normal, and a caller's own future re-registration (idempotent upsert on
`agents.sub`) can narrow its own declared set going forward, now that doing
so has a real, understood effect.

The literal list below is intentionally not imported from `schemas.py`:
migrations are frozen historical artifacts (see other revisions' own notes
on this), and `schemas.MESSAGE_TYPES` reflects the CURRENT registry, not
"whatever existed at this migration's authoring time" -- importing it would
make this file's behavior depend on future changes to that module.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1db7c2e6b70"
down_revision: str | None = "da3e1646c44d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literal snapshot of schemas.MESSAGE_TYPES at authoring time -- see the
# module docstring for why this isn't imported from schemas.py directly.
_MESSAGE_TYPES_AT_AUTHORING_TIME = (
    "availability_request",
    "availability_response",
    "confirm",
    "counter_proposal",
    "decline",
    "needs_clarification",
    "note",
    "task_assign",
    "task_cancel",
    "task_complete",
    "task_decline",
    "task_report",
)


def upgrade() -> None:
    op.execute(
        "UPDATE agents SET accepted_types = ARRAY["
        + ", ".join(f"'{t}'" for t in _MESSAGE_TYPES_AT_AUTHORING_TIME)
        + "]::text[], updated_at = now()"
    )


def downgrade() -> None:
    # Not reversed: there is no way to recover each agent's actual
    # pre-migration accepted_types (this UPDATE overwrites them
    # unconditionally, same lossy-downgrade posture as other revisions in
    # this migration chain). Downgrading this revision leaves every
    # agent's accepted_types at the widened set rather than restoring
    # narrower pre-migration declarations.
    pass
