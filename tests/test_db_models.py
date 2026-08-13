"""Schema tests for the comms domain models — real Postgres only.

Per the RH backend standard (topics/02-backend.md: "real databases in
tests"), this module never mocks the database. It runs the Alembic
migration chain against a live Postgres (the ``postgres`` service in
docker-compose.yml, or whatever ``DATABASE_URL`` points at) and asserts
that all five tables, their key columns, and the indexes called out in
DESIGN.md §5 actually exist afterward.

If Postgres is unreachable (e.g. ``docker compose up -d postgres`` was
never run), every test in this module is skipped with a clear reason
rather than failing — there is no in-memory/sqlite fallback, since that
would defeat the point of testing against the real dialect (JSONB,
ARRAY, gen_random_uuid(), etc).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from schemas import MAX_DISPLAY_NAME_LENGTH

SERVICE_ROOT = Path(__file__).parent.parent

# Same default as docker-compose.yml's `postgres` service.
_DEFAULT_TEST_DATABASE_URL = "postgresql://postgres:postgres@localhost:55432/reclaw_comms"


def _test_database_url() -> str:
    url = os.environ.get("DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _can_connect(url: str) -> bool:
    try:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:  # any connection failure just means "skip this module"
        return False


@pytest.fixture(scope="module")
def database_url() -> str:
    url = _test_database_url()
    if not asyncio.run(_can_connect(url)):
        pytest.skip(
            f"Postgres unreachable at {url!r} — run `docker compose up -d postgres` "
            "(or set DATABASE_URL) to exercise the real-database schema tests."
        )
    return url


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(database_url: str) -> None:
    """Run the full Alembic chain (downgrade base -> upgrade head) once per module.

    Runs `alembic` as a subprocess (rather than calling into Alembic's API
    in-process) so migrations/env.py's own `asyncio.run()` never collides
    with pytest-asyncio's event loop.
    """
    env = {**os.environ, "DATABASE_URL": database_url.replace("+asyncpg", "")}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=SERVICE_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


async def _columns(engine: AsyncEngine, table: str) -> dict[str, str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        )
        return {row.column_name: row.data_type for row in result}


async def _column_max_length(engine: AsyncEngine, table: str, column: str) -> int | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        return result.scalar_one_or_none()


async def _indexes(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :table"
            ),
            {"table": table},
        )
        return {row.indexname for row in result}


class TestSchema:
    """All five DESIGN.md §5 tables exist with their expected shape."""

    @pytest.mark.parametrize(
        "table",
        ["agents", "conversations", "participants", "messages", "audit_log", "tasks"],
    )
    async def test_table_exists(self, engine: AsyncEngine, table: str) -> None:
        cols = await _columns(engine, table)
        assert cols, f"expected table {table!r} to exist with columns"

    async def test_agents_columns(self, engine: AsyncEngine) -> None:
        cols = await _columns(engine, "agents")
        for expected in (
            "id",
            "sub",
            "owner_sub",
            "owner_email",
            "display_name",
            "accepted_types",
            "status",
            "created_at",
            "updated_at",
        ):
            assert expected in cols, f"agents.{expected} missing"
        assert cols["accepted_types"] == "ARRAY"
        assert cols["created_at"] == "timestamp with time zone"
        assert cols["display_name"] == "character varying"
        max_length = await _column_max_length(engine, "agents", "display_name")
        assert max_length == MAX_DISPLAY_NAME_LENGTH, (
            "agents.display_name character_maximum_length is None or wrong "
            "-- has migration 18f2d7735523 been applied?"
        )

    async def test_agents_accepted_types_check_constraint(self, engine: AsyncEngine) -> None:
        # DB-level backstop (migrations/versions/18f2d7735523...) capping
        # agents.accepted_types at 20 entries via cardinality(), not
        # array_length() — cardinality() never returns NULL for an empty
        # array, so the constraint can't be silently satisfied for that edge
        # case the way array_length() would.
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'agents'::regclass "
                    "AND conname = 'ck_agents_accepted_types_max'"
                )
            )
            constraint_def = result.scalar_one_or_none()
        assert constraint_def is not None, "ck_agents_accepted_types_max constraint missing"
        assert "cardinality" in constraint_def

    async def test_conversations_columns(self, engine: AsyncEngine) -> None:
        cols = await _columns(engine, "conversations")
        for expected in (
            "id",
            "type",
            "state",
            "created_by",
            "expires_at",
            "created_at",
            "updated_at",
        ):
            assert expected in cols, f"conversations.{expected} missing"

    async def test_participants_columns_and_invite_accept_model(self, engine: AsyncEngine) -> None:
        cols = await _columns(engine, "participants")
        for expected in (
            "conversation_id",
            "agent_id",
            "role",
            "status",
            "invited_by",
            "invited_at",
            "joined_at",
            "last_read_seq",
        ):
            assert expected in cols, f"participants.{expected} missing"

        # DESIGN.md §4: 'invited' must be an allowed status (invite/accept
        # model), and there must be no standalone surrogate PK column —
        # (conversation_id, agent_id) is the composite key.
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_participants_status'"
                )
            )
            constraint_def = result.scalar_one()
        assert "invited" in constraint_def

        async with engine.connect() as conn:
            pk_cols = await conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'participants'::regclass AND i.indisprimary"
                )
            )
            pk_col_names = {row.attname for row in pk_cols}
        assert pk_col_names == {"conversation_id", "agent_id"}

    async def test_messages_columns_and_append_only_shape(self, engine: AsyncEngine) -> None:
        cols = await _columns(engine, "messages")
        for expected in (
            "id",
            "conversation_id",
            "seq",
            "sender_id",
            "type",
            "schema_version",
            "payload",
            "created_at",
        ):
            assert expected in cols, f"messages.{expected} missing"
        assert cols["payload"] == "jsonb"

    async def test_audit_log_columns(self, engine: AsyncEngine) -> None:
        cols = await _columns(engine, "audit_log")
        for expected in (
            "id",
            "at",
            "actor_sub",
            "action",
            "agent_id",
            "conversation_id",
            "message_id",
            "task_id",
            "detail",
        ):
            assert expected in cols, f"audit_log.{expected} missing"
        assert cols["detail"] == "jsonb"

    async def test_tasks_columns_and_check_constraints(self, engine: AsyncEngine) -> None:
        cols = await _columns(engine, "tasks")
        for expected in (
            "id",
            "created_by",
            "assignee_id",
            "status",
            "schema_version",
            "payload",
            "created_at",
            "updated_at",
        ):
            assert expected in cols, f"tasks.{expected} missing"
        assert cols["payload"] == "jsonb"

        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'tasks'::regclass AND contype = 'c'"
                )
            )
            constraints = {row.conname: row.pg_get_constraintdef for row in result}
        assert "open" in constraints["ck_tasks_status"]
        assert "created_by" in constraints["ck_tasks_distinct_parties"]

    async def test_designed_indexes_exist(self, engine: AsyncEngine) -> None:
        participant_indexes = await _indexes(engine, "participants")
        assert "idx_participants_agent_id_status" in participant_indexes

        conversation_indexes = await _indexes(engine, "conversations")
        assert "idx_conversations_state_expires_at" in conversation_indexes
        assert "idx_conversations_created_by_created_at" in conversation_indexes

        audit_indexes = await _indexes(engine, "audit_log")
        assert "idx_audit_log_conversation_id" in audit_indexes
        assert "idx_audit_log_at" in audit_indexes
        assert "idx_audit_log_task_id" in audit_indexes

        message_indexes = await _indexes(engine, "messages")
        assert "idx_messages_conversation_id_sender_id_created_at" in message_indexes

        task_indexes = await _indexes(engine, "tasks")
        assert "idx_tasks_assignee_id_status" in task_indexes
        assert "idx_tasks_created_by_status" in task_indexes
        assert "idx_tasks_created_at_id" in task_indexes

        async with engine.connect() as conn:
            indexdef = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' AND indexname = 'idx_tasks_created_at_id'"
                    )
                )
            ).scalar_one()
        assert "created_at DESC" in indexdef
        assert "id DESC" in indexdef

    async def test_messages_seq_unique_per_conversation(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'messages'::regclass AND contype = 'u'"
                )
            )
            unique_constraints = {row.conname for row in result}
        assert "uq_messages_conversation_id_seq" in unique_constraints
