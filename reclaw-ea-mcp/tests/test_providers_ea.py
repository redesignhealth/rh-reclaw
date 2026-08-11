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

_MOCK_OIDC_CONFIG = MagicMock()
_OIDC_PATCH = patch(
    "fastmcp.server.auth.oidc_proxy.OIDCProxy.get_oidc_configuration",
    return_value=_MOCK_OIDC_CONFIG,
)


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
