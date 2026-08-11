"""Service-layer tests for the comms domain (service.py) — real Postgres only.

Mirrors ``tests/test_db_models.py``'s idiom: never mocks the database, runs
the full Alembic migration chain once per module against a live Postgres,
and skips the entire module (with a clear reason) if Postgres is
unreachable — there is no in-memory/sqlite fallback.

Every test exercises ``service.py`` through its public functions only
(``register_agent``, ``start_conversation``, ``accept_invite``, ...) —
never by poking ORM rows directly — except for a handful of assertions
that read back rows (``participants``, ``audit_log``) to verify side
effects the return values don't expose.
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from exceptions import AccessDeniedError, InvalidConversationStateError, RateLimitExceededError
from models import Agent, AuditLog, Participant
from schemas import PayloadValidationError
from service import (
    MAX_CONVERSATION_STARTS_PER_HOUR,
    MAX_MESSAGES_PER_CONVERSATION_PER_HOUR,
    accept_invite,
    decline_invite,
    get_conversation,
    leave,
    post_message,
    register_agent,
    start_conversation,
)

SERVICE_ROOT = Path(__file__).parent.parent
_DEFAULT_TEST_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/reclaw_comms"


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
            "(or set DATABASE_URL) to exercise the real-database service-layer tests."
        )
    return url


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(database_url: str) -> None:
    """Run the full Alembic chain (downgrade base -> upgrade head) once per module."""
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
    """Function-scoped (NOT module-scoped): asyncpg connections cannot be
    reused across the distinct event loops pytest-asyncio spins up per test
    function (``asyncio_mode = "auto"``), so a fresh engine per test is
    required — same idiom as ``tests/test_db_models.py``. The Alembic
    migration chain itself still runs only once per module (see
    ``_migrated_schema``, a sync subprocess with no engine to reuse)."""
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate every domain table before each test — tests share one engine
    (module-scoped, since re-running the Alembic chain per test is slow) but
    must not see each other's rows."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_log, messages, participants, conversations, agents "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# --- Test data helpers -----------------------------------------------------


async def _register(session: AsyncSession, sub: str, **overrides: Any) -> Agent:
    kwargs: dict[str, Any] = {
        "sub": sub,
        "owner_sub": f"owner-{sub}",
        "owner_email": f"{sub}@example.com",
        "display_name": sub,
        "accepted_types": ["scheduling.availability"],
    }
    kwargs.update(overrides)
    return await register_agent(session, **kwargs)


def _request_payload(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "window": {"start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
        "duration_min": 30,
        "modality": "video",
        "priority": "normal",
        "constraints": [],
    }
    payload.update(overrides)
    return payload


def _decline_payload(reason: str = "owner_declined") -> dict[str, Any]:
    return {"reason": reason}


def _confirm_payload() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"slot": {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat()}}


def _counter_proposal_payload() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "slots": [
            {
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "preference": 0.5,
            }
        ]
    }


async def _audit_actions(session: AsyncSession, conversation_id: uuid.UUID) -> list[str]:
    rows = (
        (
            await session.execute(
                select(AuditLog.action).where(AuditLog.conversation_id == conversation_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# --- register_agent ----------------------------------------------------------


class TestRegisterAgent:
    async def test_idempotent_upsert(self, session: AsyncSession) -> None:
        first = await _register(session, "agent-a", display_name="A v1")
        second = await _register(session, "agent-a", display_name="A v2")

        assert first.id == second.id
        assert second.display_name == "A v2"

        rows = (await session.execute(select(Agent).where(Agent.sub == "agent-a"))).scalars().all()
        assert len(rows) == 1


# --- start_conversation --------------------------------------------------------


class TestStartConversation:
    async def test_happy_path(self, session: AsyncSession) -> None:
        owner = await _register(session, "owner-1")
        target = await _register(session, "target-1")

        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )
        assert conversation.state == "active"
        assert conversation.created_by == owner.id

        owner_row = await session.get(Participant, (conversation.id, owner.id))
        target_row = await session.get(Participant, (conversation.id, target.id))
        assert owner_row is not None and owner_row.role == "owner" and owner_row.status == "active"
        assert target_row is not None and target_row.role == "member"
        assert target_row.status == "invited"
        assert target_row.joined_at is None

        messages = (
            await session.execute(
                text("SELECT seq, type FROM messages WHERE conversation_id = :cid"),
                {"cid": conversation.id},
            )
        ).all()
        assert [(m.seq, m.type) for m in messages] == [(1, "availability_request")]

    async def test_unknown_target_denied(self, session: AsyncSession) -> None:
        owner = await _register(session, "owner-2")
        bogus_target_id = uuid.uuid4()

        with pytest.raises(AccessDeniedError) as exc_info:
            await start_conversation(
                session,
                actor_sub=owner.sub,
                initiator_agent_id=owner.id,
                conversation_type="scheduling.availability",
                target_agent_ids=[bogus_target_id],
                initial_message=_request_payload(),
            )
        unknown_message = str(exc_info.value)

        owner2 = await _register(session, "owner-3")
        # register_agent requires a non-empty, KNOWN accepted_types subset,
        # and v1 has exactly one conversation type — so there is no way to
        # register an agent that legitimately doesn't accept it via the
        # public API. Simulate "accepts nothing" directly on the row (e.g. a
        # future type-restriction change) rather than through register_agent.
        target_wrong_type = await _register(session, "target-wrong-type")
        target_wrong_type.accepted_types = []
        await session.commit()

        with pytest.raises(AccessDeniedError) as exc_info_2:
            await start_conversation(
                session,
                actor_sub=owner2.sub,
                initiator_agent_id=owner2.id,
                conversation_type="scheduling.availability",
                target_agent_ids=[target_wrong_type.id],
                initial_message=_request_payload(),
            )
        type_not_accepted_message = str(exc_info_2.value)

        # Anti-enumeration: identical client-visible message for "target
        # doesn't exist" and "target doesn't accept this type".
        assert unknown_message == type_not_accepted_message

        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.agent_id.in_([owner.id, owner2.id]))
                )
            )
            .scalars()
            .all()
        )
        assert "denied.unknown_agent" in actions
        assert "denied.type_not_accepted" in actions


# --- accept_invite / decline_invite -------------------------------------------


class TestAcceptDeclineInvite:
    async def _start(self, session: AsyncSession, owner_sub: str, target_sub: str) -> Any:
        owner = await _register(session, owner_sub)
        target = await _register(session, target_sub)
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )
        return owner, target, conversation

    async def test_accept_happy_path(self, session: AsyncSession) -> None:
        _, target, conversation = await self._start(session, "acc-owner-1", "acc-target-1")
        participant = await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        assert participant.status == "active"
        assert participant.joined_at is not None

    async def test_wrong_state_rejections_share_message_distinct_actions(
        self, session: AsyncSession
    ) -> None:
        _, target, conversation = await self._start(session, "acc-owner-2", "acc-target-2")

        # Baseline: happy-path acceptance message, for string comparison below.
        with pytest.raises(AccessDeniedError) as not_participant_exc:
            await accept_invite(
                session,
                actor_sub="ghost",
                agent_id=uuid.uuid4(),
                conversation_id=conversation.id,
            )

        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        with pytest.raises(AccessDeniedError) as already_active_exc:
            await accept_invite(
                session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
            )

        _, target2, conversation2 = await self._start(session, "acc-owner-3", "acc-target-3")
        await decline_invite(
            session,
            actor_sub=target2.sub,
            agent_id=target2.id,
            conversation_id=conversation2.id,
        )
        with pytest.raises(AccessDeniedError) as declined_exc:
            await accept_invite(
                session,
                actor_sub=target2.sub,
                agent_id=target2.id,
                conversation_id=conversation2.id,
            )

        _, target3, conversation3 = await self._start(session, "acc-owner-4", "acc-target-4")
        await accept_invite(
            session,
            actor_sub=target3.sub,
            agent_id=target3.id,
            conversation_id=conversation3.id,
        )
        await leave(
            session,
            actor_sub=target3.sub,
            agent_id=target3.id,
            conversation_id=conversation3.id,
        )
        with pytest.raises(AccessDeniedError) as left_exc:
            await accept_invite(
                session,
                actor_sub=target3.sub,
                agent_id=target3.id,
                conversation_id=conversation3.id,
            )

        messages = {
            str(not_participant_exc.value),
            str(already_active_exc.value),
            str(declined_exc.value),
            str(left_exc.value),
        }
        assert len(messages) == 1, "all four denials must share the identical uniform string"

        reasons = {
            not_participant_exc.value.reason,
            already_active_exc.value.reason,
            declined_exc.value.reason,
            left_exc.value.reason,
        }
        assert reasons == {
            "denied.not_member",
            "denied.wrong_state.active",
            "denied.wrong_state.declined",
            "denied.wrong_state.left",
        }

    async def test_decline_grants_no_access(self, session: AsyncSession) -> None:
        _, target, conversation = await self._start(session, "dec-owner-1", "dec-target-1")
        await decline_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )

        with pytest.raises(AccessDeniedError) as exc_info:
            await get_conversation(
                session,
                actor_sub=target.sub,
                caller_agent_id=target.id,
                conversation_id=conversation.id,
            )

        with pytest.raises(AccessDeniedError) as nonmember_exc:
            await get_conversation(
                session,
                actor_sub="ghost",
                caller_agent_id=uuid.uuid4(),
                conversation_id=conversation.id,
            )
        assert str(exc_info.value) == str(nonmember_exc.value)


# --- get_conversation ----------------------------------------------------------


class TestGetConversation:
    async def _start(self, session: AsyncSession, owner_sub: str, target_sub: str) -> Any:
        owner = await _register(session, owner_sub)
        target = await _register(session, target_sub)
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )
        return owner, target, conversation

    async def test_invited_caller_gets_metadata_only(self, session: AsyncSession) -> None:
        _, target, conversation = await self._start(session, "gc-owner-1", "gc-target-1")
        result = await get_conversation(
            session,
            actor_sub=target.sub,
            caller_agent_id=target.id,
            conversation_id=conversation.id,
        )
        assert result["invited"] is True
        # Documented shape: an empty list, never omitted, never message content.
        assert result["messages"] == []
        assert "conversation" in result
        assert "participants" in result

        row = await session.get(Participant, (conversation.id, target.id))
        assert row is not None
        assert row.last_read_seq == 0, "invited-only reads must not advance last_read_seq"

    async def test_active_caller_gets_full_history_and_advances_last_read_seq(
        self, session: AsyncSession
    ) -> None:
        _owner, target, conversation = await self._start(session, "gc-owner-2", "gc-target-2")
        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        result = await get_conversation(
            session,
            actor_sub=target.sub,
            caller_agent_id=target.id,
            conversation_id=conversation.id,
        )
        assert result["invited"] is False
        assert [m["seq"] for m in result["messages"]] == [1]
        assert result["last_read_seq"] == 1

        row = await session.get(Participant, (conversation.id, target.id))
        assert row is not None
        assert row.last_read_seq == 1

    async def test_former_member_denied_same_as_nonmember(self, session: AsyncSession) -> None:
        _owner, target, conversation = await self._start(session, "gc-owner-3", "gc-target-3")
        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        await leave(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )

        with pytest.raises(AccessDeniedError) as left_exc:
            await get_conversation(
                session,
                actor_sub=target.sub,
                caller_agent_id=target.id,
                conversation_id=conversation.id,
            )
        with pytest.raises(AccessDeniedError) as nonmember_exc:
            await get_conversation(
                session,
                actor_sub="ghost",
                caller_agent_id=uuid.uuid4(),
                conversation_id=conversation.id,
            )
        assert str(left_exc.value) == str(nonmember_exc.value)


# --- post_message --------------------------------------------------------------


class TestPostMessage:
    async def _active_pair(self, session: AsyncSession, owner_sub: str, target_sub: str) -> Any:
        owner = await _register(session, owner_sub)
        target = await _register(session, target_sub)
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )
        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        return owner, target, conversation

    async def test_non_member_denied(self, session: AsyncSession) -> None:
        _owner, _target, conversation = await self._active_pair(
            session, "pm-owner-1", "pm-target-1"
        )
        outsider = await _register(session, "pm-outsider-1")
        with pytest.raises(AccessDeniedError):
            await post_message(
                session,
                actor_sub=outsider.sub,
                sender_agent_id=outsider.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )

    async def test_invited_not_accepted_denied_same_message_as_non_member(
        self, session: AsyncSession
    ) -> None:
        owner = await _register(session, "pm-owner-2")
        target = await _register(session, "pm-target-2")
        outsider = await _register(session, "pm-outsider-2")
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )

        with pytest.raises(AccessDeniedError) as invited_exc:
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )
        with pytest.raises(AccessDeniedError) as outsider_exc:
            await post_message(
                session,
                actor_sub=outsider.sub,
                sender_agent_id=outsider.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )
        assert str(invited_exc.value) == str(outsider_exc.value)

    async def test_state_machine_violation_after_completion(self, session: AsyncSession) -> None:
        owner, target, conversation = await self._active_pair(session, "pm-owner-3", "pm-target-3")
        await post_message(
            session,
            actor_sub=owner.sub,
            sender_agent_id=owner.id,
            conversation_id=conversation.id,
            message_type="confirm",
            payload=_confirm_payload(),
        )
        with pytest.raises(InvalidConversationStateError):
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )

    async def test_confirm_transitions_to_completed(self, session: AsyncSession) -> None:
        owner, _target, conversation = await self._active_pair(session, "pm-owner-4", "pm-target-4")
        message = await post_message(
            session,
            actor_sub=owner.sub,
            sender_agent_id=owner.id,
            conversation_id=conversation.id,
            message_type="confirm",
            payload=_confirm_payload(),
        )
        assert message.type == "confirm"
        refreshed = await session.get(type(conversation), conversation.id)
        assert refreshed is not None
        assert refreshed.state == "completed"

    async def test_decline_cascades_when_all_non_owners_decline(
        self, session: AsyncSession
    ) -> None:
        owner = await _register(session, "pm-owner-5")
        member_a = await _register(session, "pm-member-5a")
        member_b = await _register(session, "pm-member-5b")
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[member_a.id, member_b.id],
            initial_message=_request_payload(),
        )
        await accept_invite(
            session, actor_sub=member_a.sub, agent_id=member_a.id, conversation_id=conversation.id
        )
        await accept_invite(
            session, actor_sub=member_b.sub, agent_id=member_b.id, conversation_id=conversation.id
        )

        await post_message(
            session,
            actor_sub=member_a.sub,
            sender_agent_id=member_a.id,
            conversation_id=conversation.id,
            message_type="decline",
            payload=_decline_payload(),
        )
        mid = await session.get(type(conversation), conversation.id)
        assert mid is not None
        assert mid.state == "active", "one of two non-owners declining must NOT cascade"

        await post_message(
            session,
            actor_sub=member_b.sub,
            sender_agent_id=member_b.id,
            conversation_id=conversation.id,
            message_type="decline",
            payload=_decline_payload(),
        )
        final = await session.get(type(conversation), conversation.id)
        assert final is not None
        assert final.state == "canceled", "all non-owners declining must cascade to canceled"


# --- seq race-safety -----------------------------------------------------------


class TestSeqRaceSafety:
    async def test_concurrent_posts_get_distinct_contiguous_seqs(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = await _register(session, "race-owner-1")
        members = [await _register(session, f"race-member-{i}") for i in range(4)]
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[m.id for m in members],
            initial_message=_request_payload(),
        )
        for member in members:
            await accept_invite(
                session, actor_sub=member.sub, agent_id=member.id, conversation_id=conversation.id
            )

        async def _post(member: Agent) -> int:
            async with session_factory() as sess:
                message = await post_message(
                    sess,
                    actor_sub=member.sub,
                    sender_agent_id=member.id,
                    conversation_id=conversation.id,
                    message_type="counter_proposal",
                    payload=_counter_proposal_payload(),
                )
                return message.seq

        seqs = await asyncio.gather(*[_post(member) for member in members])
        assert sorted(seqs) == [2, 3, 4, 5]
        assert len(set(seqs)) == len(seqs)


# --- rate limits -----------------------------------------------------------------


class TestRateLimits:
    async def test_message_rate_limit(self, session: AsyncSession) -> None:
        owner = await _register(session, "rl-owner-1")
        target = await _register(session, "rl-target-1")
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )
        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        for _ in range(MAX_MESSAGES_PER_CONVERSATION_PER_HOUR):
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )
        with pytest.raises(RateLimitExceededError):
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )
        actions = await _audit_actions(session, conversation.id)
        assert "denied.rate_limited" in actions

    async def test_conversation_start_rate_limit(self, session: AsyncSession) -> None:
        owner = await _register(session, "rl-owner-2")
        for i in range(MAX_CONVERSATION_STARTS_PER_HOUR):
            target = await _register(session, f"rl-target-2-{i}")
            await start_conversation(
                session,
                actor_sub=owner.sub,
                initiator_agent_id=owner.id,
                conversation_type="scheduling.availability",
                target_agent_ids=[target.id],
                initial_message=_request_payload(),
            )
        overflow_target = await _register(session, "rl-target-2-overflow")
        with pytest.raises(RateLimitExceededError):
            await start_conversation(
                session,
                actor_sub=owner.sub,
                initiator_agent_id=owner.id,
                conversation_type="scheduling.availability",
                target_agent_ids=[overflow_target.id],
                initial_message=_request_payload(),
            )
        actions = (
            (await session.execute(select(AuditLog.action).where(AuditLog.agent_id == owner.id)))
            .scalars()
            .all()
        )
        assert "denied.rate_limited" in actions


# --- expiry -----------------------------------------------------------------------


class TestExpiry:
    async def test_lazy_expiry_flip_and_write_rejection(self, session: AsyncSession) -> None:
        owner = await _register(session, "exp-owner-1")
        target = await _register(session, "exp-target-1")
        already_expired = datetime.now(UTC) - timedelta(seconds=1)
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
            expires_at=already_expired,
        )
        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )

        result = await get_conversation(
            session,
            actor_sub=target.sub,
            caller_agent_id=target.id,
            conversation_id=conversation.id,
        )
        assert result["conversation"]["state"] == "expired"

        with pytest.raises(InvalidConversationStateError):
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )


# --- audit completeness ------------------------------------------------------------


class TestAuditCompleteness:
    async def test_success_and_denial_paths_are_all_audited(self, session: AsyncSession) -> None:
        owner = await _register(session, "audit-owner-1")
        target = await _register(session, "audit-target-1")
        conversation = await start_conversation(
            session,
            actor_sub=owner.sub,
            initiator_agent_id=owner.id,
            conversation_type="scheduling.availability",
            target_agent_ids=[target.id],
            initial_message=_request_payload(),
        )
        actions = set(await _audit_actions(session, conversation.id))
        assert "conversation.start" in actions
        assert "message.post" in actions

        outsider_id = uuid.uuid4()
        with pytest.raises(AccessDeniedError):
            await get_conversation(
                session,
                actor_sub="ghost-1",
                caller_agent_id=outsider_id,
                conversation_id=conversation.id,
            )
        with pytest.raises(PayloadValidationError):
            await post_message(
                session,
                actor_sub=owner.sub,
                sender_agent_id=owner.id,
                conversation_id=conversation.id,
                message_type="confirm",
                payload={"slot": "not-a-valid-shape"},
            )
        await accept_invite(
            session, actor_sub=target.sub, agent_id=target.id, conversation_id=conversation.id
        )
        for _ in range(MAX_MESSAGES_PER_CONVERSATION_PER_HOUR):
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )
        with pytest.raises(RateLimitExceededError):
            await post_message(
                session,
                actor_sub=target.sub,
                sender_agent_id=target.id,
                conversation_id=conversation.id,
                message_type="counter_proposal",
                payload=_counter_proposal_payload(),
            )

        final_actions = set(await _audit_actions(session, conversation.id))
        assert "denied.not_member" in final_actions
        assert "denied.bad_schema" in final_actions
        assert "denied.rate_limited" in final_actions
        assert "message.post" in final_actions
