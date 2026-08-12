"""Integration tests for the ``ea`` provider -- drives a full two-owner
negotiation through the REAL mounted server (auth middleware, scope
enforcement, tool dispatch), the same way reclaw-comms-mcp's
test_comms_tools.py exercises its own provider.

Both owners' ``Negotiator``s share the same process-wide ``FakeBoard``
(see providers/ea.py's module docstring) -- this is exactly what makes an
internal same-process pilot pair work end-to-end today, ahead of
TECH-5055's real board client.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

_MOCK_OIDC_CONFIG = MagicMock()
_OIDC_PATCH = patch(
    "fastmcp.server.auth.oidc_proxy.OIDCProxy.get_oidc_configuration",
    return_value=_MOCK_OIDC_CONFIG,
)

_CONVERSATION_ERROR_MESSAGE = "conversation not found or not accessible"


def _import_main() -> Any:
    sys.modules.pop("main", None)
    with _OIDC_PATCH:
        import main

        return main


def _token(sub: str, scopes: list[str] | None = None) -> MagicMock:
    token = MagicMock()
    token.claims = {
        "iss": "rh-auth",
        "sub": sub,
        "scopes": scopes if scopes is not None else ["ea:read", "ea:write", "ea:run"],
    }
    token.scopes = []
    token.client_id = sub
    return token


async def _call(main: Any, token: MagicMock, tool_name: str, args: dict[str, Any]) -> Any:
    with (
        patch("main.get_access_token", return_value=token),
        patch("providers.ea.get_access_token", return_value=token),
    ):
        async with Client(main.mcp) as client:
            result = await client.call_tool(tool_name, args)
            return result.data


def _window() -> dict[str, str]:
    start = datetime(2027, 3, 1, 14, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _slot_context(window: dict[str, str]) -> dict[str, Any]:
    return {
        "start": window["start"],
        "end": window["end"],
        "situation": {},
    }


@pytest.fixture
def main() -> Any:
    return _import_main()


class TestWhoami:
    async def test_service_caller_reports_identity_from_sub(self, main: Any) -> None:
        result = await _call(main, _token("alice-agent"), "ea_whoami", {})
        assert result["identity"] == "alice-agent"
        assert result["caller_type"] == "service"


class TestRequireOwnerIdentityRejectionPaths:
    """`require_owner_identity` is the primary impersonation defense for
    this service (Argus round 1 finding: previously untested through any
    mounted tool) -- these exercise it via a real tool call, not the bare
    function."""

    async def test_email_shaped_sub_rejected(self, main: Any) -> None:
        token = _token("alice@example.com")  # rh-auth sub must never be email-shaped
        with pytest.raises(ToolError):
            await _call(main, token, "ea_whoami", {})

    async def test_empty_sub_rejected(self, main: Any) -> None:
        token = _token("")
        with pytest.raises(ToolError):
            await _call(main, token, "ea_whoami", {})

    async def test_whitespace_sub_rejected(self, main: Any) -> None:
        token = _token("   ")
        with pytest.raises(ToolError):
            await _call(main, token, "ea_whoami", {})


class TestScopeToolRegistryParity:
    async def test_every_registered_ea_tool_has_a_scope_entry(self, main: Any) -> None:
        """Argus round 1 finding: a future tool that omits its TOOL_SCOPES
        entry is silently unreachable for M2M callers with no test-time
        signal -- this asserts the two stay in lockstep."""
        from scopes import TOOL_SCOPES

        token = _token("alice-agent")
        with patch("main.get_access_token", return_value=token):
            async with Client(main.mcp) as client:
                tools = await client.list_tools()
        registered = {tool.name for tool in tools}
        assert registered == set(TOOL_SCOPES)


class TestCrossOwnerIsolation:
    async def test_non_participant_cannot_touch_anothers_conversation(self, main: Any) -> None:
        """The load-bearing auth invariant (TECH-5065): an owner who is
        not a participant in a conversation must not be able to read or
        act on it, even knowing its conversation_id (Argus round 1
        finding: previously untested -- the only multi-owner test had both
        owners legitimately in the same conversation)."""
        bob = _token("bob3-agent")
        mallory = _token("mallory3-agent")
        window = _window()

        opened = await _call(
            main,
            bob,
            "ea_negotiate",
            {
                "to_agent_identity": "charlie3-agent",
                "window": window,
                "duration_minutes": 30,
                "modality": "video",
                "priority": 3,
            },
        )
        cid = opened["conversation_id"]

        with pytest.raises(ToolError, match=_CONVERSATION_ERROR_MESSAGE):
            await _call(main, mallory, "ea_check_completion", {"conversation_id": cid})

        # maybe_finalize short-circuits on the owner check BEFORE touching
        # the board at all for a non-owner -- it never raises, but also
        # never reveals anything (booked=False, pending_approval=False is
        # indistinguishable from "not complete yet"). Different shape from
        # the raise-based tools, same non-oracle property.
        booking = await _call(main, mallory, "ea_request_booking", {"conversation_id": cid})
        assert booking == {
            "conversation_id": cid,
            "booked": False,
            "slot": None,
            "pending_approval": False,
        }

        with pytest.raises(ToolError, match=_CONVERSATION_ERROR_MESSAGE):
            await _call(
                main,
                mallory,
                "ea_react_to_conversation",
                {"conversation_id": cid, "my_candidates": [_slot_context(window)]},
            )

    async def test_unknown_conversation_id_and_not_a_participant_raise_identically(
        self, main: Any
    ) -> None:
        """The two failure modes must be indistinguishable to the caller
        (Argus round 1 finding: previously a three-way oracle via raw
        exception propagation)."""
        alice = _token("alice4-agent")

        with pytest.raises(ToolError, match=_CONVERSATION_ERROR_MESSAGE):
            await _call(main, alice, "ea_check_completion", {"conversation_id": "conv-nonexistent"})


class TestFullNegotiationFlow:
    async def test_two_owners_negotiate_and_book_with_approval(self, main: Any) -> None:
        alice = _token("alice-agent")
        bob = _token("bob-agent")
        window = _window()

        opened = await _call(
            main,
            alice,
            "ea_negotiate",
            {
                "to_agent_identity": "bob-agent",
                "window": window,
                "duration_minutes": 30,
                "modality": "video",
                "priority": 3,
            },
        )
        cid = opened["conversation_id"]
        assert cid

        # Bob reacts to Alice's opening request, proposing his own slots.
        bob_react_1 = await _call(
            main,
            bob,
            "ea_react_to_conversation",
            {"conversation_id": cid, "my_candidates": [_slot_context(window)]},
        )
        assert bob_react_1["conversation_id"] == cid

        # Alice reacts to Bob's proposal -- her candidate matches his, so
        # this should move toward confirmation.
        await _call(
            main,
            alice,
            "ea_react_to_conversation",
            {"conversation_id": cid, "my_candidates": [_slot_context(window)]},
        )

        # Bob reacts again to complete the confirm handshake.
        await _call(
            main,
            bob,
            "ea_react_to_conversation",
            {"conversation_id": cid, "my_candidates": [_slot_context(window)]},
        )

        completion = await _call(main, alice, "ea_check_completion", {"conversation_id": cid})
        assert completion["slot"] is not None
        assert completion["slot"]["start"] == window["start"]

        # First booking request for a fresh (owner, internal) neighborhood
        # is ask_first -- no approval history yet (booking_gate.py).
        booking = await _call(main, alice, "ea_request_booking", {"conversation_id": cid})
        assert booking["booked"] is False
        assert booking["pending_approval"] is True

        approval = await _call(
            main, alice, "ea_respond_to_approval", {"conversation_id": cid, "approved": True}
        )
        assert approval["booked"] is True
        assert approval["slot"]["start"] == window["start"]

    async def test_booking_rejection_releases_hold_without_booking(self, main: Any) -> None:
        alice = _token("alice2-agent")
        bob = _token("bob2-agent")
        window = _window()

        opened = await _call(
            main,
            alice,
            "ea_negotiate",
            {
                "to_agent_identity": "bob2-agent",
                "window": window,
                "duration_minutes": 30,
                "modality": "video",
                "priority": 3,
            },
        )
        cid = opened["conversation_id"]

        for owner in (bob, alice, bob):
            await _call(
                main,
                owner,
                "ea_react_to_conversation",
                {"conversation_id": cid, "my_candidates": [_slot_context(window)]},
            )

        booking = await _call(main, alice, "ea_request_booking", {"conversation_id": cid})
        assert booking["pending_approval"] is True

        rejection = await _call(
            main, alice, "ea_respond_to_approval", {"conversation_id": cid, "approved": False}
        )
        assert rejection["booked"] is False
        assert rejection["slot"] is None
