"""Tests for the reclaw-comms-mcp scope registry (rh-mcp TECH-2752 pattern)."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from scopes import (
    TOOL_SCOPES,
    is_interactive_token,
    required_scope_for,
    required_scope_for_resource,
    safe_client_id,
    scopes_for_token,
)

# Format: ``<service>:<verb>`` or ``<service>:<sub>:<verb>``. No wildcards —
# every scope is a concrete leaf.
_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*){1,2}$")


def _fake_access_token(claims: dict[str, object], scopes: list[str] | None = None) -> MagicMock:
    """Minimal stand-in for ``fastmcp.server.auth.AccessToken``.

    ``scopes`` is an explicit parameter (not read from ``claims["scopes"]``)
    because the real JWTVerifier populates ``AccessToken.scopes`` only from
    the OAuth ``scope``/``scp`` claims — rh-auth tokens leave it empty.
    """
    token = MagicMock()
    token.claims = claims
    token.scopes = scopes if scopes is not None else []
    return token


class TestToolScopesRegistry:
    def test_registry_non_empty(self) -> None:
        assert TOOL_SCOPES, "TOOL_SCOPES must list at least one tool"

    def test_every_scope_matches_pattern(self) -> None:
        bad = {
            tool: scope for tool, scope in TOOL_SCOPES.items() if not _SCOPE_PATTERN.match(scope)
        }
        assert not bad, f"scopes must match service:verb pattern: {bad}"

    def test_tool_names_use_mount_prefix(self) -> None:
        """Every key is the mount-prefixed form (``<namespace>_<tool>``)."""
        bare = [name for name in TOOL_SCOPES if "_" not in name]
        assert not bare, f"unprefixed tool names in registry: {bare}"

    def test_whoami_uses_comms_read(self) -> None:
        assert TOOL_SCOPES["comms_whoami"] == "comms:read"


class TestRequiredScopeFor:
    def test_known_tool(self) -> None:
        assert required_scope_for("comms_whoami") == "comms:read"

    def test_unmapped_tool_returns_none(self) -> None:
        assert required_scope_for("definitely_not_a_real_tool") is None

    def test_unmapped_resource_returns_none(self) -> None:
        # RESOURCE_SCOPES is empty today — every resource URI is unmapped
        # and therefore fail-closed for rh-auth callers.
        assert required_scope_for_resource("schema://anything") is None


class TestIsInteractiveToken:
    def test_none_token_is_not_interactive(self) -> None:
        # None must fail closed — middleware rejects rather than bypassing.
        assert is_interactive_token(None) is False

    def test_rh_auth_issuer_is_not_interactive(self) -> None:
        token = _fake_access_token({"iss": "rh-auth", "sub": "bot-1"})
        assert is_interactive_token(token) is False

    def test_okta_issuer_is_interactive(self) -> None:
        # OIDCProxy mints tokens whose iss is the server's own URL.
        token = _fake_access_token({"iss": "https://reclaw-comms.example/mcp"})
        assert is_interactive_token(token) is True


class TestScopesForToken:
    """``scopes_for_token`` reads the rh-auth ``scopes`` LIST claim."""

    def test_reads_scopes_claim_not_token_scopes(self) -> None:
        # token.scopes is deliberately different to prove the claim is the
        # source of truth, not the (empty, for rh-auth) AccessToken.scopes.
        token = _fake_access_token(
            {"iss": "rh-auth", "sub": "test-svc", "scopes": ["comms:read"]},
            scopes=["should-be-ignored"],
        )
        assert scopes_for_token(token) == ["comms:read"]

    def test_missing_scopes_claim_returns_empty(self) -> None:
        token = _fake_access_token({"iss": "rh-auth", "sub": "test-svc"})
        assert scopes_for_token(token) == []

    def test_string_scalar_scopes_claim_returns_empty(self) -> None:
        # A string scalar must NOT be iterated char-by-char into bogus scopes.
        token = _fake_access_token({"iss": "rh-auth", "sub": "test-svc", "scopes": "comms:read"})
        assert scopes_for_token(token) == []

    def test_non_rh_auth_issuer_returns_empty(self) -> None:
        # Defense-in-depth issuer guard: even with a populated `scopes`
        # claim, a non-rh-auth token yields no rh-auth scopes.
        token = _fake_access_token(
            {"iss": "https://reclaw-comms.example/mcp", "scopes": ["comms:read"]}
        )
        assert scopes_for_token(token) == []

    def test_email_shaped_sub_fails_closed(self) -> None:
        # ``rh-auth issue --sub alice@redesignhealth.com`` impersonation
        # shape — must yield no scopes.
        token = _fake_access_token(
            {
                "iss": "rh-auth",
                "sub": "alice@redesignhealth.com",
                "scopes": ["comms:read"],
            }
        )
        assert scopes_for_token(token) == []

    def test_missing_sub_fails_closed(self) -> None:
        token = _fake_access_token({"iss": "rh-auth", "scopes": ["comms:read"]})
        assert scopes_for_token(token) == []

    def test_well_formed_sub_passes_guard(self) -> None:
        token = _fake_access_token(
            {"iss": "rh-auth", "sub": "ea-agent-svc", "scopes": ["comms:read"]}
        )
        assert scopes_for_token(token) == ["comms:read"]


class TestSafeClientId:
    """client_id redaction + single emission point for auth_rejected."""

    def test_rh_auth_email_shaped_sub_redacts_and_emits(self) -> None:
        token = _fake_access_token({"iss": "rh-auth", "sub": "alice@redesignhealth.com"})
        token.client_id = "alice@redesignhealth.com"
        with patch("scopes.log_auth_rejected") as mock_emit:
            result = safe_client_id(token)
        assert result == "invalid_sub"
        mock_emit.assert_called_once_with(reason="sub_shape", issuer="rh-auth")

    def test_rh_auth_missing_sub_redacts_and_emits_sub_missing(self) -> None:
        token = _fake_access_token({"iss": "rh-auth"})
        token.client_id = "unknown"
        with patch("scopes.log_auth_rejected") as mock_emit:
            result = safe_client_id(token)
        assert result == "invalid_sub"
        mock_emit.assert_called_once_with(reason="sub_missing", issuer="rh-auth")

    def test_rh_auth_well_formed_sub_passes_through_without_emit(self) -> None:
        # Legitimate denials must stay attributable, and legitimate
        # missing_scope denials must not inflate the auth_rejected counter.
        token = _fake_access_token({"iss": "rh-auth", "sub": "ea-agent-svc"})
        token.client_id = "ea-agent-svc"
        with patch("scopes.log_auth_rejected") as mock_emit:
            result = safe_client_id(token)
        assert result == "ea-agent-svc"
        mock_emit.assert_not_called()

    def test_okta_token_passes_through_unchanged_without_emit(self) -> None:
        # Okta's client_id is a registered app id, not user-input sub —
        # and Okta subs are legitimately email-shaped.
        token = _fake_access_token(
            {
                "iss": "https://redesignhealth.okta.com/oauth2/default",
                "sub": "alice@redesignhealth.com",
            }
        )
        token.client_id = "0oa1234abc"
        with patch("scopes.log_auth_rejected") as mock_emit:
            result = safe_client_id(token)
        assert result == "0oa1234abc"
        mock_emit.assert_not_called()
