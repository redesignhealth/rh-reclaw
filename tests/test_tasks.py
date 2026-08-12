"""Service-layer tests for tasks (TECH-5094) — real Postgres only.

Mirrors ``tests/test_service.py``'s idiom: never mocks the database, runs
the full Alembic migration chain once per module against a live Postgres,
and skips the module (with a clear reason) if Postgres is unreachable.

Ownership lookups go through a fake ``OwnershipClient`` (per TECH-5094 §3:
"an injected async OwnershipClient protocol ... so tests fake it") rather
than the real ``AgentTableOwnershipClient`` — this decouples the admission
matrix tests from the interim client's specific wrapping-of-owner_sub
behavior, which is covered separately in ``TestAgentTableOwnershipClient``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from exceptions import AccessDeniedError, InvalidConversationStateError, RateLimitExceededError
from models import Agent, AuditLog, Task
from schemas import PayloadValidationError
from service import (
    MAX_TASK_CREATES_PER_HOUR,
    AgentTableOwnershipClient,
    add_task,
    decline_invite,
    get_tasks,
    leave,
    may_assign,
    register_agent,
    start_conversation,
    update_task,
)

SERVICE_ROOT = Path(__file__).parent.parent
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
    except Exception:
        return False


@pytest.fixture(scope="module")
def database_url() -> str:
    url = _test_database_url()
    if not asyncio.run(_can_connect(url)):
        pytest.skip(
            f"Postgres unreachable at {url!r} — run `docker compose up -d postgres` "
            "(or set DATABASE_URL) to exercise the real-database task tests."
        )
    return url


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(database_url: str) -> None:
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


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_log, tasks, messages, participants, conversations, agents "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


# --- Test data helpers -----------------------------------------------------


async def _register(session: AsyncSession, sub: str, **overrides: Any) -> Agent:
    kwargs: dict[str, Any] = {
        "sub": sub,
        "owner_sub": overrides.pop("owner_sub", f"owner-{sub}"),
        "owner_email": f"{sub}@example.com",
        "display_name": sub,
        "accepted_types": ["open"],
    }
    kwargs.update(overrides)
    return await register_agent(session, **kwargs)


def _task_spec(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "action": "gather_availability",
        "window": {"start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
        "duration_min": 30,
    }
    payload.update(overrides)
    return payload


class FakeOwnershipClient:
    """Test double for ``service.OwnershipClient`` — an in-memory owners map."""

    def __init__(self, owners_by_agent_id: dict[uuid.UUID, dict[str, Any]]) -> None:
        self._owners_by_agent_id = owners_by_agent_id

    async def get_agent_owners(self, agent_id: uuid.UUID) -> dict[str, Any]:
        if agent_id not in self._owners_by_agent_id:
            raise LookupError(f"unknown agent {agent_id}")
        return self._owners_by_agent_id[agent_id]


class FailingOwnershipClient:
    """Test double that always raises — simulates a platform lookup failure."""

    async def get_agent_owners(self, agent_id: uuid.UUID) -> dict[str, Any]:
        raise RuntimeError("platform unreachable")


async def _audit_actions(session: AsyncSession, task_id: uuid.UUID | None = None) -> list[str]:
    stmt = select(AuditLog.action)
    if task_id is not None:
        stmt = stmt.where(AuditLog.task_id == task_id)
    return list((await session.execute(stmt)).scalars().all())


# --- may_assign (pure predicate) --------------------------------------------


class TestMayAssign:
    def test_same_single_owner_matches(self) -> None:
        assert may_assign({"dan@example.com"}, {"dan@example.com"})

    def test_disjoint_owners_do_not_match(self) -> None:
        assert not may_assign({"dan@example.com"}, {"someone-else@example.com"})

    def test_shared_agent_intersection_matches_symmetrically(self) -> None:
        # A shared agent's owner set has more than one entry; admission is
        # granted whenever the two sets share any owner, in either direction.
        assert may_assign({"dan@example.com"}, {"dan@example.com", "priya@example.com"})
        assert may_assign({"dan@example.com", "priya@example.com"}, {"priya@example.com"})

    def test_empty_sets_never_match(self) -> None:
        assert not may_assign(set(), {"dan@example.com"})
        assert not may_assign({"dan@example.com"}, set())


# --- AgentTableOwnershipClient (interim implementation) ---------------------


class TestAgentTableOwnershipClient:
    async def test_wraps_owner_sub_as_singleton_owner_set(self, session: AsyncSession) -> None:
        agent = await _register(session, "agent-a", owner_sub="dan-sub")
        client = AgentTableOwnershipClient(session)

        result = await client.get_agent_owners(agent.id)

        assert result == {"is_shared": False, "owners": ["dan-sub"]}

    async def test_unknown_agent_raises(self, session: AsyncSession) -> None:
        client = AgentTableOwnershipClient(session)
        with pytest.raises(LookupError):
            await client.get_agent_owners(uuid.uuid4())


# --- add_task: admission matrix ---------------------------------------------


class TestAddTaskAdmission:
    async def test_same_owner_agents_admitted(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="dan-sub")
        client = FakeOwnershipClient(
            {
                creator.id: {"is_shared": False, "owners": ["dan-sub"]},
                assignee.id: {"is_shared": False, "owners": ["dan-sub"]},
            }
        )

        task = await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=creator.id,
            assignee_agent_id=assignee.id,
            task=_task_spec(),
            ownership_client=client,
        )

        assert task["status"] == "open"
        assert task["role"] == "created"
        assert task["created_by"] == str(creator.id)
        assert task["assignee_agent_id"] == str(assignee.id)
        assert "task.create" in await _audit_actions(session, uuid.UUID(task["task_id"]))

    async def test_owner_sub_freeze_prevents_forged_reregistration_admission(
        self, session: AsyncSession
    ) -> None:
        """End-to-end regression for the B1 security fix (TECH-5094 Argus
        round 1/2): re-registering under a different (forged) owner_sub
        must NOT change the persisted owner_sub, and must NOT grant
        add_task admission into the impersonated owner's agents. Uses the
        real AgentTableOwnershipClient (not a fake) since this is exactly
        the seam the vulnerability lived in."""
        victim = await _register(session, "victim-agent", owner_sub="alice")
        attacker = await _register(session, "attacker-agent", owner_sub="mallory")

        # Attacker re-registers under alice's owner_sub, attempting to
        # forge admission into victim's tasks.
        reregistered = await _register(session, "attacker-agent", owner_sub="alice")
        assert reregistered.id == attacker.id
        assert reregistered.owner_sub == "mallory"  # frozen, NOT overwritten to "alice"

        client = AgentTableOwnershipClient(session)
        with pytest.raises(AccessDeniedError) as exc_info:
            await add_task(
                session,
                actor_sub="attacker-agent",
                creator_agent_id=attacker.id,
                assignee_agent_id=victim.id,
                task=_task_spec(),
                ownership_client=client,
            )
        assert exc_info.value.reason == "denied.not_same_owner"

    async def test_different_owner_agents_denied(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="priya-sub")
        client = FakeOwnershipClient(
            {
                creator.id: {"is_shared": False, "owners": ["dan-sub"]},
                assignee.id: {"is_shared": False, "owners": ["priya-sub"]},
            }
        )

        with pytest.raises(AccessDeniedError) as exc_info:
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(),
                ownership_client=client,
            )
        assert exc_info.value.reason == "denied.not_same_owner"

        actions = (await session.execute(select(AuditLog.action))).scalars().all()
        assert "denied.not_same_owner" in actions
        detail = (
            await session.execute(
                select(AuditLog.detail).where(AuditLog.action == "denied.not_same_owner")
            )
        ).scalar_one()
        assert detail == {
            "creator_is_shared": False,
            "assignee_is_shared": False,
            "matched": False,
        }

    async def test_shared_agent_intersection_admitted(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        shared_ea = await _register(session, "shared-ea", owner_sub="platform-sub")
        client = FakeOwnershipClient(
            {
                creator.id: {"is_shared": False, "owners": ["dan-sub"]},
                shared_ea.id: {"is_shared": True, "owners": ["dan-sub", "priya-sub"]},
            }
        )

        task = await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=creator.id,
            assignee_agent_id=shared_ea.id,
            task=_task_spec(),
            ownership_client=client,
        )
        assert task["status"] == "open"

    async def test_ownership_lookup_failure_fails_closed(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="dan-sub")

        with patch("service.logger") as mock_logger:
            with pytest.raises(AccessDeniedError) as exc_info:
                await add_task(
                    session,
                    actor_sub="cos",
                    creator_agent_id=creator.id,
                    assignee_agent_id=assignee.id,
                    task=_task_spec(),
                    ownership_client=FailingOwnershipClient(),
                )
            mock_logger.error.assert_called_once()
            assert mock_logger.error.call_args.kwargs.get("exc_info") is True
        assert exc_info.value.reason == "denied.ownership_unverified"

        actions = (await session.execute(select(AuditLog.action))).scalars().all()
        assert "denied.ownership_unverified" in actions
        detail = (
            await session.execute(
                select(AuditLog.detail).where(AuditLog.action == "denied.ownership_unverified")
            )
        ).scalar_one()
        assert detail == {"assignee_agent_id": str(assignee.id), "error_type": "RuntimeError"}

    async def test_ownership_lookup_failure_on_assignee_branch_fails_closed(
        self, session: AsyncSession
    ) -> None:
        """The except block covers both get_agent_owners() calls -- a
        creator-side failure alone (the other test) doesn't exercise the
        assignee-side call raising instead."""
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="dan-sub")
        client = FakeOwnershipClient({creator.id: {"is_shared": False, "owners": ["dan-sub"]}})

        with patch("service.logger") as mock_logger:
            with pytest.raises(AccessDeniedError) as exc_info:
                await add_task(
                    session,
                    actor_sub="cos",
                    creator_agent_id=creator.id,
                    assignee_agent_id=assignee.id,
                    task=_task_spec(),
                    ownership_client=client,
                )
            mock_logger.error.assert_called_once()
        assert exc_info.value.reason == "denied.ownership_unverified"

    async def test_unknown_assignee_denied(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        client = FakeOwnershipClient({creator.id: {"is_shared": False, "owners": ["dan-sub"]}})

        with pytest.raises(AccessDeniedError) as exc_info:
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=uuid.uuid4(),
                task=_task_spec(),
                ownership_client=client,
            )
        assert exc_info.value.reason == "denied.unknown_agent"

    async def test_assignee_same_as_creator_rejected(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        client = FakeOwnershipClient({creator.id: {"is_shared": False, "owners": ["dan-sub"]}})

        with pytest.raises(ValueError, match="must differ"):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=creator.id,
                task=_task_spec(),
                ownership_client=client,
            )


# --- add_task: payload validation --------------------------------------------


class TestAddTaskPayloadValidation:
    async def _pair(self, session: AsyncSession) -> tuple[Agent, Agent, FakeOwnershipClient]:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="dan-sub")
        client = FakeOwnershipClient(
            {
                creator.id: {"is_shared": False, "owners": ["dan-sub"]},
                assignee.id: {"is_shared": False, "owners": ["dan-sub"]},
            }
        )
        return creator, assignee, client

    async def test_gather_availability_requires_window_and_duration(
        self, session: AsyncSession
    ) -> None:
        creator, assignee, client = await self._pair(session)
        with pytest.raises(PayloadValidationError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task={"action": "gather_availability"},
                ownership_client=client,
            )

    async def test_report_status_needs_no_window(self, session: AsyncSession) -> None:
        creator, assignee, client = await self._pair(session)
        task = await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=creator.id,
            assignee_agent_id=assignee.id,
            task={"action": "report_status"},
            ownership_client=client,
        )
        assert task["payload"]["action"] == "report_status"

    async def test_duplicate_constraints_rejected(self, session: AsyncSession) -> None:
        creator, assignee, client = await self._pair(session)
        with pytest.raises(PayloadValidationError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(constraints=["mornings_only", "mornings_only"]),
                ownership_client=client,
            )

    async def test_free_text_field_rejected(self, session: AsyncSession) -> None:
        creator, assignee, client = await self._pair(session)
        with pytest.raises(PayloadValidationError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(notes="please handle this ASAP"),
                ownership_client=client,
            )

    async def test_related_conversation_id_requires_creator_membership(
        self, session: AsyncSession
    ) -> None:
        creator, assignee, client = await self._pair(session)
        with pytest.raises(PayloadValidationError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(related_conversation_id=str(uuid.uuid4())),
                ownership_client=client,
            )

    async def test_related_conversation_id_accepted_when_creator_is_participant(
        self, session: AsyncSession
    ) -> None:
        creator, assignee, client = await self._pair(session)
        target = await _register(session, "counterparty", owner_sub="other-sub")
        conversation = await start_conversation(
            session,
            actor_sub="cos",
            initiator_agent_id=creator.id,
            conversation_type="open",
            target_agent_ids=[target.id],
            initial_message={
                "window": {
                    "start": datetime.now(UTC).isoformat(),
                    "end": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
                "duration_min": 30,
                "modality": "video",
                "priority": "normal",
                "constraints": [],
            },
        )

        task = await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=creator.id,
            assignee_agent_id=assignee.id,
            task=_task_spec(related_conversation_id=str(conversation.id)),
            ownership_client=client,
        )
        assert task["payload"]["related_conversation_id"] == str(conversation.id)

    async def test_related_conversation_id_rejects_left_participant(
        self, session: AsyncSession
    ) -> None:
        """A left/declined former participant must not satisfy the
        membership check (TECH-5094 Argus round 1, authorization/B2) --
        only a currently-active participant counts."""
        creator, assignee, client = await self._pair(session)
        target = await _register(session, "counterparty", owner_sub="other-sub")
        conversation = await start_conversation(
            session,
            actor_sub="cos",
            initiator_agent_id=creator.id,
            conversation_type="open",
            target_agent_ids=[target.id],
            initial_message={
                "window": {
                    "start": datetime.now(UTC).isoformat(),
                    "end": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
                "duration_min": 30,
                "modality": "video",
                "priority": "normal",
                "constraints": [],
            },
        )
        await leave(session, actor_sub="cos", agent_id=creator.id, conversation_id=conversation.id)

        with pytest.raises(PayloadValidationError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(related_conversation_id=str(conversation.id)),
                ownership_client=client,
            )

    async def test_related_conversation_id_rejects_declined_participant(
        self, session: AsyncSession
    ) -> None:
        creator, assignee, client = await self._pair(session)
        other = await _register(session, "other-owner-agent", owner_sub="other-sub")
        conversation = await start_conversation(
            session,
            actor_sub="other-owner-agent",
            initiator_agent_id=other.id,
            conversation_type="open",
            target_agent_ids=[creator.id],
            initial_message={
                "window": {
                    "start": datetime.now(UTC).isoformat(),
                    "end": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
                "duration_min": 30,
                "modality": "video",
                "priority": "normal",
                "constraints": [],
            },
        )
        await decline_invite(
            session, actor_sub="cos", agent_id=creator.id, conversation_id=conversation.id
        )

        with pytest.raises(PayloadValidationError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(related_conversation_id=str(conversation.id)),
                ownership_client=client,
            )


# --- add_task: rate limiting -------------------------------------------------


class TestAddTaskRateLimit:
    async def test_exceeding_hourly_cap_denied(self, session: AsyncSession) -> None:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="dan-sub")
        client = FakeOwnershipClient(
            {
                creator.id: {"is_shared": False, "owners": ["dan-sub"]},
                assignee.id: {"is_shared": False, "owners": ["dan-sub"]},
            }
        )
        for _ in range(MAX_TASK_CREATES_PER_HOUR):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(),
                ownership_client=client,
            )

        with pytest.raises(RateLimitExceededError):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(),
                ownership_client=client,
            )


# --- get_tasks ---------------------------------------------------------------


class TestGetTasks:
    async def _seed(self, session: AsyncSession) -> tuple[Agent, Agent, Agent]:
        cos = await _register(session, "cos", owner_sub="dan-sub")
        ea = await _register(session, "ea", owner_sub="dan-sub")
        other = await _register(session, "other", owner_sub="other-sub")
        return cos, ea, other

    async def _client(self, *agents: Agent) -> FakeOwnershipClient:
        owner_sub_by_agent = {a.id: a.owner_sub for a in agents}
        return FakeOwnershipClient(
            {
                agent_id: {"is_shared": False, "owners": [owner_sub]}
                for agent_id, owner_sub in owner_sub_by_agent.items()
            }
        )

    async def test_visible_only_to_creator_and_assignee(self, session: AsyncSession) -> None:
        cos, ea, other = await self._seed(session)
        client = await self._client(cos, ea, other)
        await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=cos.id,
            assignee_agent_id=ea.id,
            task=_task_spec(),
            ownership_client=client,
        )

        result = await get_tasks(session, caller_agent_id=other.id)
        assert result["tasks"] == []
        assert result["total_count"] == 0
        assert result["has_more"] is False
        assert result["next_cursor"] is None

    async def test_role_filters(self, session: AsyncSession) -> None:
        cos, ea, _other = await self._seed(session)
        client = await self._client(cos, ea)
        await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=cos.id,
            assignee_agent_id=ea.id,
            task=_task_spec(),
            ownership_client=client,
        )

        as_creator = await get_tasks(session, caller_agent_id=cos.id, role="created")
        assert len(as_creator["tasks"]) == 1
        assert as_creator["tasks"][0]["role"] == "created"

        as_assignee = await get_tasks(session, caller_agent_id=ea.id, role="assigned")
        assert len(as_assignee["tasks"]) == 1
        assert as_assignee["tasks"][0]["role"] == "assigned"

        assert await get_tasks(session, caller_agent_id=cos.id, role="assigned") == {
            "tasks": [],
            "total_count": 0,
            "has_more": False,
            "next_cursor": None,
        }

    async def test_status_filter(self, session: AsyncSession) -> None:
        cos, ea, _other = await self._seed(session)
        client = await self._client(cos, ea)
        await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=cos.id,
            assignee_agent_id=ea.id,
            task=_task_spec(),
            ownership_client=client,
        )

        open_tasks = await get_tasks(session, caller_agent_id=cos.id, status="open")
        assert len(open_tasks["tasks"]) == 1

        done_tasks = await get_tasks(session, caller_agent_id=cos.id, status="done")
        assert done_tasks["tasks"] == []

    async def test_pagination(self, session: AsyncSession) -> None:
        cos, ea, _other = await self._seed(session)
        client = await self._client(cos, ea)
        for _ in range(5):
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=cos.id,
                assignee_agent_id=ea.id,
                task=_task_spec(),
                ownership_client=client,
            )

        page1 = await get_tasks(session, caller_agent_id=cos.id, limit=2)
        assert len(page1["tasks"]) == 2
        assert page1["has_more"] is True
        assert page1["total_count"] == 5
        assert page1["next_cursor"] is not None

        page2 = await get_tasks(
            session, caller_agent_id=cos.id, limit=2, cursor=page1["next_cursor"]
        )
        assert len(page2["tasks"]) == 2
        assert page2["has_more"] is True

        page3 = await get_tasks(
            session, caller_agent_id=cos.id, limit=2, cursor=page2["next_cursor"]
        )
        assert len(page3["tasks"]) == 1
        assert page3["has_more"] is False
        assert page3["next_cursor"] is None

        seen_ids = {t["task_id"] for t in page1["tasks"] + page2["tasks"] + page3["tasks"]}
        assert len(seen_ids) == 5

    async def test_no_audit_rows_for_reads(self, session: AsyncSession) -> None:
        cos, ea, _other = await self._seed(session)
        client = await self._client(cos, ea)
        await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=cos.id,
            assignee_agent_id=ea.id,
            task=_task_spec(),
            ownership_client=client,
        )
        before = len((await session.execute(select(AuditLog.action))).scalars().all())

        await get_tasks(session, caller_agent_id=cos.id)

        after = len((await session.execute(select(AuditLog.action))).scalars().all())
        assert before == after


# --- update_task ---------------------------------------------------------------


class TestUpdateTask:
    async def _open_task(self, session: AsyncSession) -> tuple[Agent, Agent, str]:
        creator = await _register(session, "cos", owner_sub="dan-sub")
        assignee = await _register(session, "ea", owner_sub="dan-sub")
        client = FakeOwnershipClient(
            {
                creator.id: {"is_shared": False, "owners": ["dan-sub"]},
                assignee.id: {"is_shared": False, "owners": ["dan-sub"]},
            }
        )
        task = await add_task(
            session,
            actor_sub="cos",
            creator_agent_id=creator.id,
            assignee_agent_id=assignee.id,
            task=_task_spec(),
            ownership_client=client,
        )
        return creator, assignee, task["task_id"]

    async def test_creator_marks_done(self, session: AsyncSession) -> None:
        creator, _assignee, task_id = await self._open_task(session)
        # >= against created_at would be a no-op guard here (a stale value
        # would still satisfy it); compare against a real pre-transition
        # read instead (TECH-5099 Argus round 2).
        before = (await get_tasks(session, caller_agent_id=creator.id))["tasks"][0]["updated_at"]

        result = await update_task(
            session,
            actor_sub="cos",
            caller_agent_id=creator.id,
            task_id=uuid.UUID(task_id),
            status="done",
        )
        assert result["status"] == "done"
        assert result["role"] == "created"
        assert result["updated_at"] > before

    async def test_assignee_marks_done(self, session: AsyncSession) -> None:
        _creator, assignee, task_id = await self._open_task(session)
        result = await update_task(
            session,
            actor_sub="ea",
            caller_agent_id=assignee.id,
            task_id=uuid.UUID(task_id),
            status="done",
        )
        assert result["status"] == "done"
        assert result["role"] == "assigned"

    async def test_assignee_declines(self, session: AsyncSession) -> None:
        _creator, assignee, task_id = await self._open_task(session)
        result = await update_task(
            session,
            actor_sub="ea",
            caller_agent_id=assignee.id,
            task_id=uuid.UUID(task_id),
            status="declined",
        )
        assert result["status"] == "declined"

    async def test_creator_cannot_decline(self, session: AsyncSession) -> None:
        creator, _assignee, task_id = await self._open_task(session)
        with pytest.raises(AccessDeniedError) as exc_info:
            await update_task(
                session,
                actor_sub="cos",
                caller_agent_id=creator.id,
                task_id=uuid.UUID(task_id),
                status="declined",
            )
        assert exc_info.value.reason == "denied.not_assignee"
        assert "denied.not_assignee" in await _audit_actions(session, uuid.UUID(task_id))

    async def test_non_party_denied(self, session: AsyncSession) -> None:
        _creator, _assignee, task_id = await self._open_task(session)
        outsider = await _register(session, "outsider", owner_sub="other-sub")
        with pytest.raises(AccessDeniedError) as exc_info:
            await update_task(
                session,
                actor_sub="outsider",
                caller_agent_id=outsider.id,
                task_id=uuid.UUID(task_id),
                status="done",
            )
        assert exc_info.value.reason == "denied.not_party"
        assert "denied.not_party" in await _audit_actions(session, uuid.UUID(task_id))

    async def test_non_party_denied_for_declined_too(self, session: AsyncSession) -> None:
        """A non-party attempting the assignee-only ``declined`` transition
        must still get ``denied.not_party`` — never ``denied.not_assignee``,
        which would confirm the caller found a real task with a real
        assignee (anti-enumeration; TECH-5099 Argus round 1)."""
        _creator, _assignee, task_id = await self._open_task(session)
        outsider = await _register(session, "outsider", owner_sub="other-sub")
        with pytest.raises(AccessDeniedError) as exc_info:
            await update_task(
                session,
                actor_sub="outsider",
                caller_agent_id=outsider.id,
                task_id=uuid.UUID(task_id),
                status="declined",
            )
        assert exc_info.value.reason == "denied.not_party"
        assert "denied.not_party" in await _audit_actions(session, uuid.UUID(task_id))

    async def test_non_party_denied_uniformly_even_on_terminal_task(
        self, session: AsyncSession
    ) -> None:
        """Guard ordering is a security property: a non-party hitting an
        already-terminal task must get the uniform ``denied.not_party``,
        never the specific ``InvalidConversationStateError`` (which would
        leak the task's current status to a stranger)."""
        creator, _assignee, task_id = await self._open_task(session)
        await update_task(
            session,
            actor_sub="cos",
            caller_agent_id=creator.id,
            task_id=uuid.UUID(task_id),
            status="done",
        )
        outsider = await _register(session, "outsider", owner_sub="other-sub")
        with pytest.raises(AccessDeniedError) as exc_info:
            await update_task(
                session,
                actor_sub="outsider",
                caller_agent_id=outsider.id,
                task_id=uuid.UUID(task_id),
                status="done",
            )
        assert exc_info.value.reason == "denied.not_party"
        assert "denied.not_party" in await _audit_actions(session, uuid.UUID(task_id))

    async def test_party_on_terminal_task_gets_bad_state_not_not_assignee(
        self, session: AsyncSession
    ) -> None:
        """Concrete regression for the round-2 guard-ordering bug: a party
        (here, the creator) hitting an already-terminal task with
        status='declined' must get InvalidConversationStateError, never
        AccessDeniedError('denied.not_assignee') -- the terminal-state
        check must fire before the assignee-only restriction, for every
        party, not just the assignee."""
        creator, assignee, task_id = await self._open_task(session)
        await update_task(
            session,
            actor_sub="ea",
            caller_agent_id=assignee.id,
            task_id=uuid.UUID(task_id),
            status="done",
        )
        with pytest.raises(InvalidConversationStateError):
            await update_task(
                session,
                actor_sub="cos",
                caller_agent_id=creator.id,
                task_id=uuid.UUID(task_id),
                status="declined",
            )
        actions = await _audit_actions(session, uuid.UUID(task_id))
        assert "denied.bad_state" in actions
        assert "denied.not_assignee" not in actions

    async def test_suspended_agent_denied(self, session: AsyncSession) -> None:
        creator, _assignee, task_id = await self._open_task(session)
        creator.status = "suspended"
        await session.commit()

        with pytest.raises(AccessDeniedError) as exc_info:
            await update_task(
                session,
                actor_sub="cos",
                caller_agent_id=creator.id,
                task_id=uuid.UUID(task_id),
                status="done",
            )
        assert exc_info.value.reason == "denied.unknown_agent"
        # No task_id filter: _require_active_agent now threads task_id
        # through, but confirm via the unfiltered helper too so this test
        # doesn't assume that wiring stays correct.
        assert "denied.unknown_agent" in await _audit_actions(session)
        assert "denied.unknown_agent" in await _audit_actions(session, uuid.UUID(task_id))

    async def test_unknown_task_id_denied_uniformly(self, session: AsyncSession) -> None:
        outsider = await _register(session, "outsider", owner_sub="other-sub")
        with pytest.raises(AccessDeniedError) as exc_info:
            await update_task(
                session,
                actor_sub="outsider",
                caller_agent_id=outsider.id,
                task_id=uuid.uuid4(),
                status="done",
            )
        assert exc_info.value.reason == "denied.not_party"
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
        assert "denied.not_party" in actions

    async def test_no_transition_out_of_done(self, session: AsyncSession) -> None:
        creator, _assignee, task_id = await self._open_task(session)
        await update_task(
            session,
            actor_sub="cos",
            caller_agent_id=creator.id,
            task_id=uuid.UUID(task_id),
            status="done",
        )
        with pytest.raises(InvalidConversationStateError):
            await update_task(
                session,
                actor_sub="cos",
                caller_agent_id=creator.id,
                task_id=uuid.UUID(task_id),
                status="done",
            )
        assert "denied.bad_state" in await _audit_actions(session, uuid.UUID(task_id))

    async def test_no_transition_out_of_declined(self, session: AsyncSession) -> None:
        _creator, assignee, task_id = await self._open_task(session)
        await update_task(
            session,
            actor_sub="ea",
            caller_agent_id=assignee.id,
            task_id=uuid.UUID(task_id),
            status="declined",
        )
        with pytest.raises(InvalidConversationStateError):
            await update_task(
                session,
                actor_sub="ea",
                caller_agent_id=assignee.id,
                task_id=uuid.UUID(task_id),
                status="done",
            )

    async def test_invalid_status_rejected(self, session: AsyncSession) -> None:
        creator, _assignee, task_id = await self._open_task(session)
        with pytest.raises(ValueError, match="status must be"):
            await update_task(
                session,
                actor_sub="cos",
                caller_agent_id=creator.id,
                task_id=uuid.UUID(task_id),
                status="open",
            )

    async def test_audit_completeness(self, session: AsyncSession) -> None:
        creator, _assignee, task_id = await self._open_task(session)
        await update_task(
            session,
            actor_sub="cos",
            caller_agent_id=creator.id,
            task_id=uuid.UUID(task_id),
            status="done",
        )
        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.task_id == uuid.UUID(task_id))
                )
            )
            .scalars()
            .all()
        )
        assert "task.update_status" in actions


# --- Task model constraints ---------------------------------------------------


class TestTaskDbConstraints:
    async def test_distinct_parties_check_constraint_enforced(self, session: AsyncSession) -> None:
        """Belt-and-suspenders: even if a future code path skipped the
        service-layer ``assignee == creator`` check, the DB CHECK backstops
        it (TECH-5094 §2)."""
        agent = await _register(session, "solo", owner_sub="dan-sub")
        session.add(
            Task(
                created_by=agent.id,
                assignee_id=agent.id,
                status="open",
                schema_version=1,
                payload={"type": "task_spec", "action": "report_status"},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
