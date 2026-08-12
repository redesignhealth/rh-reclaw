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
        assert result["owner_identity"] == "alice-agent"
        assert result["caller_type"] == "service"

    async def test_never_raises_even_when_owner_identity_unresolvable(self, main: Any) -> None:
        """Argus round 2 finding: a diagnostic tool that fails closed on
        the exact failure it exists to diagnose is useless. An interactive
        token with no email/preferred_username/sub claim at all makes
        require_owner_identity raise -- whoami must still return a result,
        with owner_identity=None flagging the problem. (An rh-auth token
        with a shape-invalid sub can't exercise this path: scopes_for_token
        fails the SAME shape check, so ScopeEnforcementMiddleware rejects
        it before the tool body ever runs -- see is_interactive_token's
        bypass, which this token uses instead.)"""
        token = MagicMock()
        token.claims = {"iss": "https://example-server.internal"}
        token.scopes = []
        token.client_id = "unknown"
        result = await _call(main, token, "ea_whoami", {})
        assert result["owner_identity"] is None
        assert result["caller_type"] == "interactive"


class TestRequireOwnerIdentityRejectionPaths:
    """`require_owner_identity` is the primary impersonation defense for
    this service (Argus round 1 finding: previously untested through any
    mounted tool) -- these exercise it via a real tool call, not the bare
    function. Uses `ea_check_completion`, not `ea_whoami` (round 2 finding:
    `ea_whoami` deliberately never raises on an unresolvable identity, so
    it can't be used to test the fail-closed path anymore -- every OTHER
    tool still calls `_require_identity()` and does raise)."""

    async def test_email_shaped_sub_rejected(self, main: Any) -> None:
        token = _token("alice@example.com")  # rh-auth sub must never be email-shaped
        with pytest.raises(ToolError):
            await _call(main, token, "ea_check_completion", {"conversation_id": "conv-1"})

    async def test_empty_sub_rejected(self, main: Any) -> None:
        token = _token("")
        with pytest.raises(ToolError):
            await _call(main, token, "ea_check_completion", {"conversation_id": "conv-1"})

    async def test_whitespace_sub_rejected(self, main: Any) -> None:
        token = _token("   ")
        with pytest.raises(ToolError):
            await _call(main, token, "ea_check_completion", {"conversation_id": "conv-1"})


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

        # ea_request_booking explicitly checks participants_of() before
        # ever calling maybe_finalize (Argus round 2 finding, fixed): a
        # non-participant must raise the same uniform error as the other
        # tools, not fall through to maybe_finalize's silent
        # non-owner-participant no-op (which is legitimate ONLY for a
        # caller who IS a participant, just not the owner).
        with pytest.raises(ToolError, match=_CONVERSATION_ERROR_MESSAGE):
            await _call(main, mallory, "ea_request_booking", {"conversation_id": cid})

        with pytest.raises(ToolError, match=_CONVERSATION_ERROR_MESSAGE):
            await _call(
                main,
                mallory,
                "ea_react_to_conversation",
                {"conversation_id": cid, "my_candidates": [_slot_context(window)]},
            )

    async def test_non_participant_cannot_respond_to_anothers_approval(self, main: Any) -> None:
        """Argus round 2 finding: `TestCrossOwnerIsolation` omitted
        `ea_respond_to_approval` -- the highest-privilege tool (the
        human-in-the-loop booking gate). Mallory has no pending approval
        for a conversation she was never part of, so `has_pending_booking_
        approval` returns False and `ToolError` is raised directly at that
        pre-check -- not the conversation-not-found path -- both are safe
        (neither reveals board state), just via different messages. (Argus
        round 5 finding: an earlier version of this docstring called the
        tool's `except ValueError` block "since-removed" -- round 4
        re-added it, for a different, narrower case: the race where
        `sweep_expired_booking_approvals` runs between this pre-check and
        the actual call. That branch is exercised by
        `test_race_between_precheck_and_call_is_denied_safely` below, not
        this test.)"""
        bob = _token("bob5-agent")
        mallory = _token("mallory5-agent")
        window = _window()

        opened = await _call(
            main,
            bob,
            "ea_negotiate",
            {
                "to_agent_identity": "charlie5-agent",
                "window": window,
                "duration_minutes": 30,
                "modality": "video",
                "priority": 3,
            },
        )
        cid = opened["conversation_id"]

        with (
            patch("providers.ea.log_security_event") as mock_log,
            pytest.raises(ToolError, match="no pending booking approval"),
        ):
            await _call(
                main, mallory, "ea_respond_to_approval", {"conversation_id": cid, "approved": True}
            )
        # Argus round 5 finding: nothing previously asserted that the
        # no_pending denial path actually emits an audit event -- a
        # silent removal of that log_security_event call would not have
        # failed this test.
        mock_log.assert_called_once_with(
            "booking_approval_call_denied",
            operation="ea_respond_to_approval",
            reason="no_pending",
            owner="mallory5-agent",
        )

    async def test_race_between_precheck_and_call_is_denied_safely(self, main: Any) -> None:
        """Argus round 5 finding: the race branch added in round 4 (
        `except ValueError` after the `has_pending_booking_approval`
        pre-check passes but `respond_to_booking_approval` itself still
        raises -- e.g. another request's `sweep_expired_booking_approvals`
        flips the hold to expired in between) had no direct test."""
        alice = _token("alice6-agent")
        bob = _token("bob6-agent")
        window = _window()

        opened = await _call(
            main,
            alice,
            "ea_negotiate",
            {
                "to_agent_identity": "bob6-agent",
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
        await _call(main, alice, "ea_request_booking", {"conversation_id": cid})

        import providers.ea as ea

        negotiator = ea._negotiator_for("alice6-agent")
        with (
            patch.object(
                negotiator,
                "respond_to_booking_approval",
                side_effect=ValueError("no pending booking approval for conversation (raced)"),
            ),
            patch("providers.ea.log_security_event") as mock_log,
            pytest.raises(ToolError, match="no pending booking approval"),
        ):
            await _call(
                main, alice, "ea_respond_to_approval", {"conversation_id": cid, "approved": True}
            )
        mock_log.assert_called_once_with(
            "booking_approval_call_denied",
            operation="ea_respond_to_approval",
            reason="lapsed_between_precheck_and_call",
            owner="alice6-agent",
            error_type="ValueError",
            exc_info=True,
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


class TestInputValidationBoundaries:
    """Argus round 2 finding: the round-1 constraints added to the tool
    schemas (energy_peak pairing, conversation_id length, duration_minutes
    and priority bounds) had no boundary-condition test coverage."""

    async def test_partial_energy_peak_rejected(self, main: Any) -> None:
        window = _window()
        candidate = _slot_context(window)
        candidate["energy_peak_start"] = "09:00:00"  # energy_peak_end omitted
        with pytest.raises(ToolError):
            await _call(
                main,
                _token("boundary1-agent"),
                "ea_react_to_conversation",
                {"conversation_id": "conv-1", "my_candidates": [candidate]},
            )

    async def test_conversation_id_over_max_length_rejected(self, main: Any) -> None:
        with pytest.raises(ToolError):
            await _call(
                main,
                _token("boundary2-agent"),
                "ea_check_completion",
                {"conversation_id": "x" * 257},
            )

    async def test_duration_minutes_zero_rejected(self, main: Any) -> None:
        with pytest.raises(ToolError):
            await _call(
                main,
                _token("boundary3-agent"),
                "ea_negotiate",
                {
                    "to_agent_identity": "someone-agent",
                    "window": _window(),
                    "duration_minutes": 0,
                    "modality": "video",
                    "priority": 3,
                },
            )

    async def test_duration_minutes_over_24h_rejected(self, main: Any) -> None:
        with pytest.raises(ToolError):
            await _call(
                main,
                _token("boundary4-agent"),
                "ea_negotiate",
                {
                    "to_agent_identity": "someone-agent",
                    "window": _window(),
                    "duration_minutes": 24 * 60 + 1,
                    "modality": "video",
                    "priority": 3,
                },
            )

    async def test_priority_out_of_range_rejected(self, main: Any) -> None:
        for bad_priority in (0, 5):
            with pytest.raises(ToolError):
                await _call(
                    main,
                    _token("boundary5-agent"),
                    "ea_negotiate",
                    {
                        "to_agent_identity": "someone-agent",
                        "window": _window(),
                        "duration_minutes": 30,
                        "modality": "video",
                        "priority": bad_priority,
                    },
                )


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
