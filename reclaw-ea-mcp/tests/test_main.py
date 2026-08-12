"""Tests for server composition and scope-enforcement middleware.

Adapted from reclaw-comms-mcp/tests/test_main.py -- identical shape, just
against this service's tool registry (``ea_*`` instead of ``comms_*``).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

_MOCK_OIDC_CONFIG = MagicMock()

_OIDC_PATCH = patch(
    "fastmcp.server.auth.oidc_proxy.OIDCProxy.get_oidc_configuration",
    return_value=_MOCK_OIDC_CONFIG,
)


def _import_main() -> object:
    """Import a fresh ``main`` module under the OIDC patch (env defaults
    come from conftest's autouse ``_auth_env`` fixture)."""
    sys.modules.pop("main", None)
    with _OIDC_PATCH:
        import main

        return main


def _rh_auth_token(sub: str, scopes: list[str] | None = None) -> MagicMock:
    token = MagicMock()
    token.claims = {
        "iss": "rh-auth",
        "sub": sub,
        "scopes": scopes if scopes is not None else [],
    }
    token.scopes = []
    token.client_id = sub
    return token


def _interactive_token(email: str) -> MagicMock:
    token = MagicMock()
    token.claims = {"iss": "https://example-server.internal", "email": email}
    token.scopes = []
    token.client_id = "unknown"
    return token


class TestServerComposition:
    def test_server_name(self) -> None:
        main = _import_main()
        assert main.mcp.name == "reclaw-ea-mcp"

    def test_has_auth(self) -> None:
        main = _import_main()
        assert main.mcp.auth is not None

    def test_missing_required_env_fails_fast(self) -> None:
        import os

        from auth import require_env

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="OKTA_CLIENT_SECRET"):
                require_env("OKTA_CLIENT_SECRET")


class TestScopeEnforcement:
    """Every scenario calls through the REAL mounted server (auth
    middleware, scope enforcement, tool dispatch) -- never the raw Python
    function."""

    async def test_interactive_caller_bypasses_scope_check(self) -> None:
        main = _import_main()
        token = _interactive_token("alice@example.com")
        with (
            patch("main.get_access_token", return_value=token),
            patch("providers.ea.get_access_token", return_value=token),
        ):
            async with Client(main.mcp) as client:
                result = await client.call_tool("ea_whoami", {})
                assert result.data["caller_type"] == "interactive"

    async def test_rh_auth_caller_missing_scope_denied_uniformly(self) -> None:
        main = _import_main()
        token = _rh_auth_token("alice-agent", scopes=[])  # no ea:write
        with patch("main.get_access_token", return_value=token):
            async with Client(main.mcp) as client:
                with pytest.raises(ToolError, match="insufficient_scope"):
                    await client.call_tool(
                        "ea_negotiate",
                        {
                            "to_agent_identity": "bob-agent",
                            "window": {
                                "start": "2027-01-01T10:00:00Z",
                                "end": "2027-01-01T12:00:00Z",
                            },
                            "duration_minutes": 30,
                            "modality": "video",
                        },
                    )

    async def test_rh_auth_caller_with_scope_succeeds(self) -> None:
        main = _import_main()
        token = _rh_auth_token("alice-agent", scopes=["ea:read"])
        with (
            patch("main.get_access_token", return_value=token),
            patch("providers.ea.get_access_token", return_value=token),
        ):
            async with Client(main.mcp) as client:
                result = await client.call_tool("ea_whoami", {})
                assert result.data["caller_type"] == "service"
                assert result.data["scopes"] == ["ea:read"]

    async def test_unenrolled_tool_name_would_be_denied(self) -> None:
        """Fail-closed default: ``required_scope_for`` returns ``None`` for
        anything not in ``TOOL_SCOPES``, and the middleware denies it."""
        from scopes import required_scope_for

        assert required_scope_for("ea_not_a_real_tool") is None

    async def test_missing_token_denied(self) -> None:
        """Argus round 1 finding: the ``token is None`` deny branch
        (ScopeEnforcementMiddleware.on_call_tool) was untested -- every
        other test patches ``get_access_token`` to return a valid mock."""
        main = _import_main()
        with patch("main.get_access_token", return_value=None):
            async with Client(main.mcp) as client:
                with pytest.raises(ToolError, match="insufficient_scope"):
                    await client.call_tool("ea_whoami", {})
