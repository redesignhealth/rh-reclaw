"""End-to-end tests for the comms MCP tool surface (providers/comms.py).

Mirrors ``tests/test_service.py``'s real-Postgres idiom (module-scoped
Alembic chain, function-scoped engine/session, autouse truncate, skip the
whole module with a clear reason if Postgres is unreachable) combined with
``tests/test_main.py``'s in-memory ``fastmcp.Client`` end-to-end idiom
(fresh ``main`` import under OIDC/env patches, ``get_access_token`` mocked
per simulated caller).

Every tool call goes through the REAL mounted server (auth middleware,
scope enforcement, tool dispatch) — never the raw Python function — so
these tests exercise the full stack this stage was built to wire up.
``providers.comms.get_session_factory`` is patched to the test database's
session factory (the documented test-injection seam, db.py's docstring).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SERVICE_ROOT = Path(__file__).parent.parent
_DEFAULT_TEST_DATABASE_URL = "postgresql://postgres:postgres@localhost:55432/reclaw_comms"

_MOCK_OIDC_CONFIG = MagicMock()
_OIDC_PATCH = patch(
    "fastmcp.server.auth.oidc_proxy.OIDCProxy.get_oidc_configuration",
    return_value=_MOCK_OIDC_CONFIG,
)
_ENV_PATCH = patch.dict(
    os.environ,
    {
        "OKTA_ISSUER_URL": "https://example.okta.com/oauth2/default",
        "OKTA_CLIENT_ID": "test-id",
        "OKTA_CLIENT_SECRET": "test-secret",
        "BASE_URL": "http://localhost:8080",
        "MCP_JWT_SECRET": "test-jwt-secret",
        "RH_AUTH_SECRET": "test-rh-auth-secret-long-enough-for-hs256",
    },
)


def _import_main() -> Any:
    """Import a fresh ``main`` module under the OIDC/env patches."""
    sys.modules.pop("main", None)
    with _OIDC_PATCH, _ENV_PATCH:
        import main

        return main


# --- Database fixtures (mirrors tests/test_service.py) ----------------------------


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
            "(or set DATABASE_URL) to exercise the real-database tool tests."
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


@pytest.fixture
def test_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# --- MCP client helpers -------------------------------------------------------------


def _token(
    sub: str,
    *,
    scopes: list[str] | None = None,
    owner_sub: str | None = None,
    owner_email: str | None = None,
) -> MagicMock:
    """A minimal rh-auth-shaped ``AccessToken`` stand-in for ``sub``."""
    claims: dict[str, Any] = {
        "iss": "rh-auth",
        "sub": sub,
        "scopes": scopes if scopes is not None else ["comms:read", "comms:write"],
    }
    if owner_sub is not None:
        claims["owner_sub"] = owner_sub
    if owner_email is not None:
        claims["owner_email"] = owner_email
    token = MagicMock()
    token.claims = claims
    token.scopes = []
    token.client_id = sub
    return token


async def _call(
    main: Any,
    test_session_factory: async_sessionmaker[AsyncSession],
    token: MagicMock,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    with (
        _OIDC_PATCH,
        _ENV_PATCH,
        patch("main.get_access_token", return_value=token),
        patch("providers.comms.get_access_token", return_value=token),
        patch("providers.comms.get_session_factory", return_value=test_session_factory),
    ):
        async with Client(main.mcp) as client:
            result = await client.call_tool(tool_name, args or {})
            return result.data


@pytest.fixture
def main() -> Any:
    return _import_main()


async def _register(
    main: Any,
    test_session_factory: async_sessionmaker[AsyncSession],
    sub: str,
    *,
    display_name: str | None = None,
    accepted_types: list[str] | None = None,
    owner_sub: str | None = None,
) -> dict[str, Any]:
    token = _token(sub, owner_sub=owner_sub)
    result: dict[str, Any] = await _call(
        main,
        test_session_factory,
        token,
        "comms_register",
        {
            "display_name": display_name or sub,
            "accepted_types": accepted_types or ["scheduling.availability"],
        },
    )
    return result


def _availability_request() -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return {
        "window": {"start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
        "duration_min": 30,
        "modality": "video",
        "priority": "normal",
        "constraints": [],
    }


def _availability_response() -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return {
        "slots": [
            {
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "preference": 0.8,
            }
        ]
    }


def _confirm_payload() -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return {"slot": {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat()}}


# --- Registration ---------------------------------------------------------------


class TestRegister:
    async def test_register_persists_and_is_visible_via_whoami(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        token = _token(
            "agent-a", owner_sub="owner-a-human", owner_email="ownera@redesignhealth.com"
        )
        result = await _call(
            main,
            test_session_factory,
            token,
            "comms_register",
            {"display_name": "Agent A", "accepted_types": ["scheduling.availability"]},
        )
        assert result["sub"] == "agent-a"
        assert result["display_name"] == "Agent A"
        assert result["accepted_types"] == ["scheduling.availability"]
        assert result["status"] == "active"
        assert result["owner_email"] == "ownera@redesignhealth.com"

        whoami = await _call(main, test_session_factory, token, "comms_whoami")
        assert whoami["identity"] == "agent-a"

    async def test_register_is_idempotent(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        token = _token("agent-b")
        first = await _call(
            main,
            test_session_factory,
            token,
            "comms_register",
            {"display_name": "B v1", "accepted_types": ["scheduling.availability"]},
        )
        second = await _call(
            main,
            test_session_factory,
            token,
            "comms_register",
            {"display_name": "B v2", "accepted_types": ["scheduling.availability"]},
        )
        assert first["agent_id"] == second["agent_id"]
        assert second["display_name"] == "B v2"

    async def test_register_without_owner_claims_falls_back_to_self(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        token = _token("agent-self-owned")
        result = await _call(
            main,
            test_session_factory,
            token,
            "comms_register",
            {"display_name": "Self", "accepted_types": ["scheduling.availability"]},
        )
        # No owner_sub/owner_email claims on the token — self-owned fallback.
        assert result["owner_email"] == "agent-self-owned"

    async def test_register_rh_auth_forged_email_claim_not_trusted(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An rh-auth (agent) token's ``email`` claim is caller-supplied and
        unverified (the ``rh-auth issue`` CLI accepts arbitrary extra
        claims) — it must never be trusted as ``owner_email``, even when
        present. This is the negative case the existing "no email claim at
        all" tests don't cover: here the token DOES carry an ``email``
        claim, and it must still be ignored in favor of the sub-derived
        self-owned fallback."""
        token = _token("agent-forged-email")
        token.claims["email"] = "forged@attacker.com"

        result = await _call(
            main,
            test_session_factory,
            token,
            "comms_register",
            {"display_name": "Forged", "accepted_types": ["scheduling.availability"]},
        )
        assert result["owner_email"] != "forged@attacker.com"
        assert result["owner_email"] == "agent-forged-email"

    async def test_register_unknown_accepted_type_names_valid_set_in_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Unlike a generic ``invalid_request`` ValueError, an unrecognized
        ``accepted_types`` entry surfaces a specific ``ToolError`` naming
        the actual valid set — a caller (e.g. an external agent probing
        the API) does not have to guess at ``schemas.CONVERSATION_TYPES``
        one rejected call at a time. See exceptions.py's module docstring
        for why this is deliberately not folded into the uniform-denial
        posture used for authorization failures."""
        token = _token("agent-probes-valid-types")
        with pytest.raises(ToolError, match=r"accepted_types must be a non-empty subset of"):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_register",
                {"display_name": "Prober", "accepted_types": ["__probe_invalid_type__"]},
            )

    async def test_register_empty_accepted_types_generic_tool_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Boundary-level counterpart to
        ``test_service.test_empty_accepted_types_raises_plain_value_error``:
        an empty ``accepted_types`` list is a bare ``ValueError`` at the
        service layer, which ``_map_service_errors`` maps to the generic
        ``invalid_request`` ``ToolError`` shape (not the specific
        ``UnknownConversationTypeError`` message) at the MCP boundary."""
        token = _token("agent-empty-types-boundary")
        with pytest.raises(ToolError, match="invalid_request"):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_register",
                {"display_name": "Empty Types", "accepted_types": []},
            )

    async def test_register_over_count_accepted_types_generic_tool_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Boundary-level counterpart to
        ``test_service.test_oversized_accepted_types_of_unknown_values_still_hits_count_cap``:
        21 entries hits the count cap (a bare ``ValueError``) before any
        entry is checked against ``CONVERSATION_TYPES``, so the MCP layer
        sees the generic ``invalid_request`` shape, not the specific
        unknown-type error."""
        token = _token("agent-oversized-types-boundary")
        with pytest.raises(ToolError, match="invalid_request"):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_register",
                {
                    "display_name": "Oversized Types",
                    "accepted_types": [f"bogus-{i}" for i in range(21)],
                },
            )

    async def test_register_oversized_single_accepted_type_entry_generic_tool_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Boundary test for the per-entry length cap (Argus round 3):
        a single oversized entry (101 chars) must be rejected at the MCP
        boundary as generic invalid_request, not echoed verbatim."""
        token = _token("agent-oversized-entry-boundary")
        with pytest.raises(ToolError, match="invalid_request"):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_register",
                {"display_name": "Entry Length Test", "accepted_types": ["x" * 101]},
            )


# --- AXI empty-state / shape spot checks --------------------------------------------


class TestAxiShapes:
    async def test_inbox_empty_state_is_explicit(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "lonely-agent")
        result = await _call(main, test_session_factory, _token("lonely-agent"), "comms_inbox")
        assert result == {"unread": [], "pending_invites": [], "total_count": 0}

    async def test_list_agents_includes_total_count(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "dir-agent-1")
        await _register(main, test_session_factory, "dir-agent-2")
        result = await _call(main, test_session_factory, _token("dir-agent-1"), "comms_list_agents")
        assert result["total_count"] == 2
        assert result["has_more"] is False
        assert {a["sub"] for a in result["agents"]} == {"dir-agent-1", "dir-agent-2"}


# --- Unregistered-caller path --------------------------------------------------------


class TestNotRegistered:
    async def test_unregistered_caller_gets_distinct_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(ToolError, match="not_registered"):
            await _call(main, test_session_factory, _token("never-registered"), "comms_inbox")


# --- Full happy-path negotiation ------------------------------------------------------


class TestFullNegotiationFlow:
    async def test_full_flow(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "agent-a")
        await _register(main, test_session_factory, "agent-b")
        await _register(main, test_session_factory, "agent-c")

        token_a = _token("agent-a")
        token_b = _token("agent-b")
        token_c = _token("agent-c")

        # A starts a conversation with B and C.
        list_result = await _call(main, test_session_factory, token_a, "comms_list_agents")
        by_sub = {a["sub"]: a["agent_id"] for a in list_result["agents"]}

        started = await _call(
            main,
            test_session_factory,
            token_a,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": [by_sub["agent-b"], by_sub["agent-c"]],
                "initial_message": _availability_request(),
            },
        )
        conversation_id = started["conversation_id"]
        assert started["state"] == "active"

        # B accepts, then sees full history (the seq-1 availability_request).
        accepted = await _call(
            main,
            test_session_factory,
            token_b,
            "comms_accept",
            {"conversation_id": conversation_id},
        )
        assert accepted["status"] == "active"

        b_view = await _call(
            main,
            test_session_factory,
            token_b,
            "comms_get_conversation",
            {"conversation_id": conversation_id},
        )
        assert b_view["invited"] is False
        assert [m["type"] for m in b_view["messages"]] == ["availability_request"]
        # Tool-boundary rename: the count key is ``messages_returned``, not
        # ``total_count`` (which would misleadingly imply the conversation's
        # total message count rather than this since_seq-filtered slice).
        assert "messages_returned" in b_view
        assert b_view["messages_returned"] == 1
        assert "total_count" not in b_view

        # C never accepts — metadata-only, no message content.
        c_view = await _call(
            main,
            test_session_factory,
            token_c,
            "comms_get_conversation",
            {"conversation_id": conversation_id},
        )
        assert c_view["invited"] is True
        assert c_view["messages"] == []
        # Metadata-only path: no message-count field at all (neither the
        # renamed ``messages_returned`` nor the original ``total_count``),
        # unlike the active-member path asserted above.
        assert "messages_returned" not in c_view
        assert "total_count" not in c_view

        # B posts an availability_response.
        b_response = await _call(
            main,
            test_session_factory,
            token_b,
            "comms_post_message",
            {
                "conversation_id": conversation_id,
                "message_type": "availability_response",
                "payload": _availability_response(),
            },
        )
        assert b_response["seq"] == 2

        # A confirms — conversation completes.
        a_confirm = await _call(
            main,
            test_session_factory,
            token_a,
            "comms_post_message",
            {
                "conversation_id": conversation_id,
                "message_type": "confirm",
                "payload": _confirm_payload(),
            },
        )
        assert a_confirm["seq"] == 3

        final_view = await _call(
            main,
            test_session_factory,
            token_a,
            "comms_get_conversation",
            {"conversation_id": conversation_id},
        )
        assert final_view["conversation"]["state"] == "completed"

        # Further posts are rejected — a state-machine violation, NOT the
        # uniform denial (the caller is still an authorized active member).
        with pytest.raises(ToolError) as exc_info:
            await _call(
                main,
                test_session_factory,
                token_b,
                "comms_post_message",
                {
                    "conversation_id": conversation_id,
                    "message_type": "availability_response",
                    "payload": _availability_response(),
                },
            )
        assert "access_denied" not in str(exc_info.value)
        assert "completed" in str(exc_info.value)

    async def test_uniform_denial_identical_for_non_member_and_uninvited_caller(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "owner-x")
        await _register(main, test_session_factory, "invitee-x")
        await _register(main, test_session_factory, "outsider-x")

        token_owner = _token("owner-x")
        token_invitee = _token("invitee-x")
        token_outsider = _token("outsider-x")

        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        invitee_id = next(a["agent_id"] for a in list_result["agents"] if a["sub"] == "invitee-x")

        started = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": [invitee_id],
                "initial_message": _availability_request(),
            },
        )
        conversation_id = started["conversation_id"]

        # invitee-x is INVITED but has not accepted — posting is denied.
        with pytest.raises(ToolError) as invitee_exc:
            await _call(
                main,
                test_session_factory,
                token_invitee,
                "comms_post_message",
                {
                    "conversation_id": conversation_id,
                    "message_type": "availability_response",
                    "payload": _availability_response(),
                },
            )

        # outsider-x is a registered agent with NO participant row at all.
        with pytest.raises(ToolError) as outsider_exc:
            await _call(
                main,
                test_session_factory,
                token_outsider,
                "comms_post_message",
                {
                    "conversation_id": conversation_id,
                    "message_type": "availability_response",
                    "payload": _availability_response(),
                },
            )

        # Anti-enumeration: byte-identical denial message for both causes.
        assert str(invitee_exc.value) == str(outsider_exc.value)
        assert str(invitee_exc.value) == "access_denied: not authorized for this resource"

        # Same uniform message reading a conversation the outsider was
        # never named on at all.
        with pytest.raises(ToolError) as outsider_read_exc:
            await _call(
                main,
                test_session_factory,
                token_outsider,
                "comms_get_conversation",
                {"conversation_id": conversation_id},
            )
        assert str(outsider_read_exc.value) == str(invitee_exc.value)


# --- Rate limit / schema validation: distinct, informative messages -----------------


class TestRateLimitAndSchemaErrors:
    async def test_rate_limit_error_is_specific_not_uniform(
        self,
        main: Any,
        test_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import service

        monkeypatch.setattr(service, "MAX_CONVERSATION_STARTS_PER_HOUR", 1)

        await _register(main, test_session_factory, "rl-owner")
        await _register(main, test_session_factory, "rl-target")
        token_owner = _token("rl-owner")

        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        target_id = next(a["agent_id"] for a in list_result["agents"] if a["sub"] == "rl-target")

        # First start succeeds and consumes the (patched) budget of 1.
        await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": [target_id],
                "initial_message": _availability_request(),
            },
        )

        with pytest.raises(ToolError) as exc_info:
            await _call(
                main,
                test_session_factory,
                token_owner,
                "comms_start_conversation",
                {
                    "conversation_type": "scheduling.availability",
                    "target_agent_ids": [target_id],
                    "initial_message": _availability_request(),
                },
            )
        message = str(exc_info.value)
        assert "rate_limited" in message
        assert message != "access_denied: not authorized for this resource"

    async def test_schema_validation_error_is_specific_not_uniform(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "sv-owner")
        await _register(main, test_session_factory, "sv-target")
        token_owner = _token("sv-owner")

        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        target_id = next(a["agent_id"] for a in list_result["agents"] if a["sub"] == "sv-target")

        with pytest.raises(ToolError) as exc_info:
            await _call(
                main,
                test_session_factory,
                token_owner,
                "comms_start_conversation",
                {
                    "conversation_type": "scheduling.availability",
                    "target_agent_ids": [target_id],
                    # missing required fields (duration_min, modality, priority)
                    "initial_message": {"window": _availability_request()["window"]},
                },
            )
        message = str(exc_info.value)
        assert "payload failed schema validation" in message
        assert message != "access_denied: not authorized for this resource"

    async def test_unknown_conversation_type_error_is_specific_not_uniform(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An unsupported ``conversation_type`` surfaces a specific
        ``ToolError`` naming the actual valid set, the same
        discoverability fix as ``comms_register``'s ``accepted_types``
        (see ``TestRegister.test_register_unknown_accepted_type_names_valid_set_in_error``).
        Checked before any target lookup, so a bogus type doesn't need a
        real target to reproduce."""
        await _register(main, test_session_factory, "uct-owner")
        token_owner = _token("uct-owner")

        with pytest.raises(ToolError, match=r"unknown conversation_type 'bogus'"):
            await _call(
                main,
                test_session_factory,
                token_owner,
                "comms_start_conversation",
                {
                    "conversation_type": "bogus",
                    "target_agent_ids": [str(uuid.uuid4())],
                    "initial_message": _availability_request(),
                },
            )

    async def test_negative_since_seq_rejected(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "neg-seq-owner")
        token_owner = _token("neg-seq-owner")

        # No conversation needs to exist yet — this is a pure input-shape
        # check the tool boundary performs before ever touching the DB.
        with pytest.raises(ToolError, match=re.escape("invalid_request: since_seq must be >= 0")):
            await _call(
                main,
                test_session_factory,
                token_owner,
                "comms_get_conversation",
                {"conversation_id": str(uuid.uuid4()), "since_seq": -1},
            )

    async def test_target_agent_ids_over_participant_cap_rejected(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from schemas import MAX_PARTICIPANTS_PER_CONVERSATION

        await _register(main, test_session_factory, "cap-owner")
        token_owner = _token("cap-owner")
        too_many_ids = [str(uuid.uuid4()) for _ in range(MAX_PARTICIPANTS_PER_CONVERSATION + 1)]

        with pytest.raises(
            ToolError,
            match=re.escape(
                "invalid_request: target_agent_ids exceeds the participant cap "
                f"({MAX_PARTICIPANTS_PER_CONVERSATION})"
            ),
        ):
            await _call(
                main,
                test_session_factory,
                token_owner,
                "comms_start_conversation",
                {
                    "conversation_type": "scheduling.availability",
                    "target_agent_ids": too_many_ids,
                    "initial_message": _availability_request(),
                },
            )


# --- Membership mutation tools: invite / leave / decline_invite ---------------------


class TestMembershipTools:
    async def test_invite_leave_decline_round_trip(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "mem-owner")
        await _register(main, test_session_factory, "mem-b")
        await _register(main, test_session_factory, "mem-c")

        token_owner = _token("mem-owner")
        token_b = _token("mem-b")
        token_c = _token("mem-c")

        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        ids = {a["sub"]: a["agent_id"] for a in list_result["agents"]}

        started = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": [ids["mem-b"]],
                "initial_message": _availability_request(),
            },
        )
        conversation_id = started["conversation_id"]

        await _call(
            main,
            test_session_factory,
            token_b,
            "comms_accept",
            {"conversation_id": conversation_id},
        )

        # B (now active) invites C.
        invite_result = await _call(
            main,
            test_session_factory,
            token_b,
            "comms_invite",
            {"conversation_id": conversation_id, "target_agent_id": ids["mem-c"]},
        )
        assert invite_result["status"] == "invited"

        # C declines — terminal, no access granted.
        decline_result = await _call(
            main,
            test_session_factory,
            token_c,
            "comms_decline_invite",
            {"conversation_id": conversation_id},
        )
        assert decline_result["status"] == "declined"

        with pytest.raises(
            ToolError, match=re.escape("access_denied: not authorized for this resource")
        ):
            await _call(
                main,
                test_session_factory,
                token_c,
                "comms_get_conversation",
                {"conversation_id": conversation_id},
            )

        # B leaves.
        leave_result = await _call(
            main, test_session_factory, token_b, "comms_leave", {"conversation_id": conversation_id}
        )
        assert leave_result["status"] == "left"

        with pytest.raises(
            ToolError, match=re.escape("access_denied: not authorized for this resource")
        ):
            await _call(
                main,
                test_session_factory,
                token_b,
                "comms_post_message",
                {
                    "conversation_id": conversation_id,
                    "message_type": "availability_response",
                    "payload": _availability_response(),
                },
            )


class TestTasks:
    """End-to-end coverage for comms_add_task / comms_get_tasks (TECH-5094)."""

    async def test_same_owner_agents_can_task_each_other(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Mirrors the real-world case this admission policy exists for: two
        # of one person's agents (e.g. a Chief-of-Staff agent and an EA
        # agent) share an owner_sub and should be admitted without any
        # shared-agent machinery.
        creator = await _register(
            main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com"
        )
        assignee = await _register(
            main, test_session_factory, "pepper-potts", owner_sub="owner-dan@example.com"
        )
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        result = await _call(
            main,
            test_session_factory,
            token,
            "comms_add_task",
            {
                "assignee_agent_id": assignee["agent_id"],
                "task": {"action": "report_status"},
            },
        )

        assert result["status"] == "open"
        assert result["created_by"] == creator["agent_id"]
        assert result["assignee_agent_id"] == assignee["agent_id"]
        assert result["role"] == "created"
        assert result["created_by_sub"] == "bond-007"
        assert result["assignee_sub"] == "pepper-potts"
        assert result["updated_at"] is not None

        assignee_view = await _call(
            main,
            test_session_factory,
            _token("pepper-potts", owner_sub="owner-dan@example.com"),
            "comms_get_tasks",
        )
        assert assignee_view["tasks"][0]["role"] == "assigned"

    async def test_different_owner_agents_uniformly_denied(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        other = await _register(
            main, test_session_factory, "someone-elses-ea", owner_sub="owner-priya@example.com"
        )
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        with pytest.raises(
            ToolError, match=re.escape("access_denied: not authorized for this resource")
        ):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_add_task",
                {
                    "assignee_agent_id": other["agent_id"],
                    "task": {"action": "report_status"},
                },
            )

    async def test_get_tasks_visible_only_to_creator_and_assignee(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        assignee = await _register(
            main, test_session_factory, "pepper-potts", owner_sub="owner-dan@example.com"
        )
        outsider = await _register(
            main, test_session_factory, "outsider", owner_sub="owner-priya@example.com"
        )
        creator_token = _token("bond-007", owner_sub="owner-dan@example.com")
        assignee_token = _token("pepper-potts", owner_sub="owner-dan@example.com")
        outsider_token = _token("outsider", owner_sub="owner-priya@example.com")

        await _call(
            main,
            test_session_factory,
            creator_token,
            "comms_add_task",
            {"assignee_agent_id": assignee["agent_id"], "task": {"action": "report_status"}},
        )

        creator_view = await _call(main, test_session_factory, creator_token, "comms_get_tasks")
        assert creator_view["total_count"] == 1
        assert creator_view["tasks"][0]["role"] == "created"

        assignee_view = await _call(main, test_session_factory, assignee_token, "comms_get_tasks")
        assert assignee_view["total_count"] == 1
        assert assignee_view["tasks"][0]["role"] == "assigned"

        outsider_view = await _call(main, test_session_factory, outsider_token, "comms_get_tasks")
        assert outsider_view == {
            "tasks": [],
            "total_count": 0,
            "has_more": False,
            "next_cursor": None,
        }
        assert outsider["agent_id"]  # registered, just not a party to the task

    async def test_add_task_rejects_free_text_payload(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        assignee = await _register(
            main, test_session_factory, "pepper-potts", owner_sub="owner-dan@example.com"
        )
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        with pytest.raises(ToolError):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_add_task",
                {
                    "assignee_agent_id": assignee["agent_id"],
                    "task": {"action": "report_status", "notes": "call me back"},
                },
            )

    async def test_get_tasks_cursor_pages_through_tool_layer(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        assignee = await _register(
            main, test_session_factory, "pepper-potts", owner_sub="owner-dan@example.com"
        )
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        for _ in range(3):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_add_task",
                {
                    "assignee_agent_id": assignee["agent_id"],
                    "task": {"action": "report_status"},
                },
            )

        page1 = await _call(main, test_session_factory, token, "comms_get_tasks", {"limit": 2})
        assert len(page1["tasks"]) == 2
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None

        page2 = await _call(
            main,
            test_session_factory,
            token,
            "comms_get_tasks",
            {"limit": 2, "cursor": page1["next_cursor"]},
        )
        assert len(page2["tasks"]) == 1
        assert page2["has_more"] is False
        assert page2["next_cursor"] is None

        seen_ids = {t["task_id"] for t in page1["tasks"] + page2["tasks"]}
        assert len(seen_ids) == 3

    async def test_get_tasks_malformed_cursor_maps_to_tool_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        with pytest.raises(ToolError, match="invalid_request: the request could not be processed"):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_get_tasks",
                {"cursor": "not-a-valid-cursor"},
            )

    async def test_update_task_end_to_end(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        assignee = await _register(
            main, test_session_factory, "pepper-potts", owner_sub="owner-dan@example.com"
        )
        creator_token = _token("bond-007", owner_sub="owner-dan@example.com")
        assignee_token = _token("pepper-potts", owner_sub="owner-dan@example.com")

        added = await _call(
            main,
            test_session_factory,
            creator_token,
            "comms_add_task",
            {"assignee_agent_id": assignee["agent_id"], "task": {"action": "report_status"}},
        )

        with pytest.raises(
            ToolError, match=re.escape("access_denied: not authorized for this resource")
        ):
            await _call(
                main,
                test_session_factory,
                creator_token,
                "comms_update_task",
                {"task_id": added["task_id"], "status": "declined"},
            )

        updated = await _call(
            main,
            test_session_factory,
            assignee_token,
            "comms_update_task",
            {"task_id": added["task_id"], "status": "declined"},
        )
        assert updated["status"] == "declined"

        with pytest.raises(
            ToolError,
            match=re.escape("task cannot transition to 'done' while its status is 'declined'"),
        ):
            await _call(
                main,
                test_session_factory,
                assignee_token,
                "comms_update_task",
                {"task_id": added["task_id"], "status": "done"},
            )

        # status='done' through the full tool stack too -- the above only
        # exercised 'declined'.
        added_2 = await _call(
            main,
            test_session_factory,
            creator_token,
            "comms_add_task",
            {"assignee_agent_id": assignee["agent_id"], "task": {"action": "report_status"}},
        )
        done_result = await _call(
            main,
            test_session_factory,
            creator_token,
            "comms_update_task",
            {"task_id": added_2["task_id"], "status": "done"},
        )
        assert done_result["status"] == "done"

    async def test_update_task_malformed_task_id_maps_to_tool_error(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        with pytest.raises(ToolError):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_update_task",
                {"task_id": "not-a-uuid", "status": "done"},
            )

    async def test_update_task_unknown_task_id_matches_non_party_denial_message(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Anti-enumeration at the MCP boundary: an unknown task_id must
        produce the exact same ToolError text as a genuine non-party
        denial (TECH-5099 Argus round 1)."""
        await _register(main, test_session_factory, "bond-007", owner_sub="owner-dan@example.com")
        token = _token("bond-007", owner_sub="owner-dan@example.com")

        with pytest.raises(
            ToolError, match=re.escape("access_denied: not authorized for this resource")
        ):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_update_task",
                {"task_id": str(uuid.uuid4()), "status": "done"},
            )


# --- Registry parity / scope enforcement still intact --------------------------------


class TestScopesUnaffected:
    async def test_all_new_tools_are_registry_enrolled(self, main: Any) -> None:
        from scopes import TOOL_SCOPES

        tools = await main.mcp.list_tools()
        mounted = {t.name for t in tools}
        expected = {
            "comms_register",
            "comms_list_agents",
            "comms_start_conversation",
            "comms_post_message",
            "comms_get_conversation",
            "comms_inbox",
            "comms_accept",
            "comms_decline_invite",
            "comms_invite",
            "comms_leave",
            "comms_add_task",
            "comms_get_tasks",
            "comms_update_task",
        }
        assert expected <= mounted
        assert expected <= set(TOOL_SCOPES)

    async def test_missing_scope_still_denied_for_new_write_tool(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # comms:read only — comms_register requires comms:write.
        token = _token("scope-test-agent", scopes=["comms:read"])
        with pytest.raises(ToolError, match="requires elevated permissions"):
            await _call(
                main,
                test_session_factory,
                token,
                "comms_register",
                {"display_name": "x", "accepted_types": ["scheduling.availability"]},
            )

    async def test_unenrolled_tool_still_rejected(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        token = _token("scope-test-agent-2", scopes=["comms:read", "comms:write"])
        with pytest.raises(ToolError, match="requires elevated permissions"):
            await _call(main, test_session_factory, token, "comms_not_a_real_tool")


# --- availability_response's none_available branch, end-to-end -------------------


class TestAvailabilityResponseNoneAvailable:
    async def test_none_available_with_reason_accepted_and_round_trips(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _register(main, test_session_factory, "na-owner")
        await _register(main, test_session_factory, "na-target")
        token_owner = _token("na-owner")
        token_target = _token("na-target")

        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        target_id = next(a["agent_id"] for a in list_result["agents"] if a["sub"] == "na-target")

        started = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": [target_id],
                "initial_message": _availability_request(),
            },
        )
        conversation_id = started["conversation_id"]
        await _call(
            main,
            test_session_factory,
            token_target,
            "comms_accept",
            {"conversation_id": conversation_id},
        )

        posted = await _call(
            main,
            test_session_factory,
            token_target,
            "comms_post_message",
            {
                "conversation_id": conversation_id,
                "message_type": "availability_response",
                "payload": {"none_available": True, "reason": "no_overlap"},
            },
        )
        assert posted["payload"]["none_available"] is True
        assert posted["payload"]["reason"] == "no_overlap"
        assert posted["payload"].get("slots") is None

        view = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_get_conversation",
            {"conversation_id": conversation_id},
        )
        response_message = next(m for m in view["messages"] if m["type"] == "availability_response")
        assert response_message["payload"]["none_available"] is True
        assert response_message["payload"]["reason"] == "no_overlap"


# --- lazy expiry, end-to-end -----------------------------------------------------


class TestLazyExpiryEndToEnd:
    async def test_get_conversation_reflects_expired_state(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from datetime import UTC, datetime, timedelta

        await _register(main, test_session_factory, "exp-owner")
        await _register(main, test_session_factory, "exp-target")
        token_owner = _token("exp-owner")
        token_target = _token("exp-target")

        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        target_id = next(a["agent_id"] for a in list_result["agents"] if a["sub"] == "exp-target")

        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        started = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": [target_id],
                "initial_message": _availability_request(),
                "expires_at": past,
            },
        )
        conversation_id = started["conversation_id"]
        await _call(
            main,
            test_session_factory,
            token_target,
            "comms_accept",
            {"conversation_id": conversation_id},
        )

        view = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_get_conversation",
            {"conversation_id": conversation_id},
        )
        assert view["conversation"]["state"] == "expired"


# --- concurrent seq assignment, exercised through the full tool stack ------------


class TestConcurrentPostMessageToolLayer:
    async def test_concurrent_posts_get_distinct_contiguous_seqs(
        self, main: Any, test_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # ``_call``'s module-level ``_OIDC_PATCH``/``_ENV_PATCH``/``patch(...)``
        # context managers are singleton objects that raise "Patch is already
        # started" if entered twice concurrently, so ``asyncio.gather`` over
        # several ``_call`` invocations is not viable here. Instead, patch
        # ``get_access_token`` ONCE (outside the gather) with a resolver keyed
        # off a ``contextvars.ContextVar`` — asyncio.Task copies the calling
        # context at creation, so each gathered task's own ``.set()`` is
        # invisible to its siblings, giving per-task caller identity under
        # true concurrency without re-entering any patch.
        import contextvars

        await _register(main, test_session_factory, "race-owner")
        member_subs = [f"race-member-{i}" for i in range(4)]
        for sub in member_subs:
            await _register(main, test_session_factory, sub)

        token_owner = _token("race-owner")
        list_result = await _call(main, test_session_factory, token_owner, "comms_list_agents")
        ids_by_sub = {a["sub"]: a["agent_id"] for a in list_result["agents"]}
        member_ids = [ids_by_sub[sub] for sub in member_subs]

        started = await _call(
            main,
            test_session_factory,
            token_owner,
            "comms_start_conversation",
            {
                "conversation_type": "scheduling.availability",
                "target_agent_ids": member_ids,
                "initial_message": _availability_request(),
            },
        )
        conversation_id = started["conversation_id"]

        for sub in member_subs:
            await _call(
                main,
                test_session_factory,
                _token(sub),
                "comms_accept",
                {"conversation_id": conversation_id},
            )

        current_token: contextvars.ContextVar[MagicMock] = contextvars.ContextVar("current_token")

        async def _post(sub: str) -> int:
            current_token.set(_token(sub))
            async with Client(main.mcp) as client:
                result = await client.call_tool(
                    "comms_post_message",
                    {
                        "conversation_id": conversation_id,
                        "message_type": "availability_response",
                        "payload": _availability_response(),
                    },
                )
            seq: int = result.data["seq"]
            return seq

        with (
            _OIDC_PATCH,
            _ENV_PATCH,
            patch("main.get_access_token", side_effect=current_token.get),
            patch("providers.comms.get_access_token", side_effect=current_token.get),
            patch("providers.comms.get_session_factory", return_value=test_session_factory),
        ):
            seqs = await asyncio.gather(*[_post(sub) for sub in member_subs])

        assert sorted(seqs) == [2, 3, 4, 5]
        assert len(set(seqs)) == len(seqs)
