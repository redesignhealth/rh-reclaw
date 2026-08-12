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

from exceptions import AccessDeniedError, RateLimitExceededError
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
        "accepted_types": ["scheduling.availability"],
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

        with pytest.raises(AccessDeniedError) as exc_info:
            await add_task(
                session,
                actor_sub="cos",
                creator_agent_id=creator.id,
                assignee_agent_id=assignee.id,
                task=_task_spec(),
                ownership_client=FailingOwnershipClient(),
            )
        assert exc_info.value.reason == "denied.ownership_unverified"

        actions = (await session.execute(select(AuditLog.action))).scalars().all()
        assert "denied.ownership_unverified" in actions

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
            conversation_type="scheduling.availability",
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
            conversation_type="scheduling.availability",
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
            conversation_type="scheduling.availability",
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
