"""Tests for identity.py's rh-auth-detection + email-resolution helpers.

Unit-tests ``try_resolve_email`` directly against constructed claims dicts
(no DB, no MCP tool stack). Exercises the ``_is_rh_auth_token`` fail-closed
behavior only indirectly, through ``try_resolve_email``'s observable
behavior (which claim it resolves identity from) rather than by calling the
private helper directly — mirrors ``tests/test_scopes.py``'s
``TestIsInteractiveToken`` pattern, which is the scopes.py counterpart to
the ``iss is None`` fail-closed guard tested here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from identity import RH_AUTH_ISSUER, try_resolve_email, validate_sub_shape


def _fake_token(claims: dict[str, object]) -> MagicMock:
    """Minimal stand-in for FastMCP's AccessToken (only ``.claims`` is used)."""
    token = MagicMock()
    token.claims = claims
    return token


class TestTryResolveEmail:
    def test_rh_auth_token_resolves_via_sub_not_email(self) -> None:
        # rh-auth's `email` claim is attacker-controlled (`rh-auth issue`
        # accepts arbitrary extra claims) — must never be trusted.
        token = _fake_token(
            {
                "iss": RH_AUTH_ISSUER,
                "sub": "ea-agent-svc",
                "email": "forged@redesignhealth.com",
            }
        )
        assert try_resolve_email(token) == "ea-agent-svc"

    def test_missing_iss_with_forged_email_resolves_via_sub(self) -> None:
        # The higher-stakes case from the docstring: no `iss` claim at all,
        # plus a forged `email` claim. Must resolve to the sub-derived
        # identity, never the forged email.
        token = _fake_token({"sub": "svc-account-1", "email": "forged@redesignhealth.com"})
        assert try_resolve_email(token) == "svc-account-1"

    def test_explicit_none_iss_with_forged_email_resolves_via_sub(self) -> None:
        # Same fail-closed guard as the missing-iss case above, but with an
        # explicit `iss: None` claim rather than an absent key -- must not
        # fall through to trusting the forged `email` claim.
        token = _fake_token(
            {"iss": None, "sub": "svc-account-1", "email": "forged@redesignhealth.com"}
        )
        assert try_resolve_email(token) == "svc-account-1"

    def test_interactive_token_trusts_email_claim(self) -> None:
        # Contrast case: a genuinely interactive (non-rh-auth) token with an
        # Okta-like issuer SHOULD have its `email` claim trusted.
        token = _fake_token(
            {
                "iss": "https://redesignhealth.okta.com/oauth2/default",
                "sub": "00u1234okta",
                "email": "person@redesignhealth.com",
            }
        )
        assert try_resolve_email(token) == "person@redesignhealth.com"

    def test_interactive_token_falls_back_to_preferred_username(self) -> None:
        token = _fake_token(
            {
                "iss": "https://redesignhealth.okta.com/oauth2/default",
                "sub": "00u1234okta",
                "preferred_username": "person@redesignhealth.com",
            }
        )
        assert try_resolve_email(token) == "person@redesignhealth.com"

    def test_interactive_token_with_no_identity_claims_falls_back_to_sub(self) -> None:
        token = _fake_token(
            {"iss": "https://redesignhealth.okta.com/oauth2/default", "sub": "00u1234okta"}
        )
        assert try_resolve_email(token) == "00u1234okta"

    def test_rh_auth_token_missing_sub_returns_none(self) -> None:
        token = _fake_token({"iss": RH_AUTH_ISSUER, "email": "forged@redesignhealth.com"})
        assert try_resolve_email(token) is None

    def test_interactive_token_missing_all_identity_claims_returns_none(self) -> None:
        token = _fake_token({"iss": "https://redesignhealth.okta.com/oauth2/default"})
        assert try_resolve_email(token) is None

    def test_email_shaped_sub_fails_closed_to_none(self) -> None:
        # `rh-auth issue --sub alice@redesignhealth.com` impersonation
        # attempt: validate_sub_shape rejects it, try_resolve_email fails
        # open with None rather than raising or returning the impersonated
        # identity.
        token = _fake_token({"iss": RH_AUTH_ISSUER, "sub": "alice@redesignhealth.com"})
        assert try_resolve_email(token) is None

    def test_whitespace_sub_fails_closed_to_none(self) -> None:
        token = _fake_token({"iss": RH_AUTH_ISSUER, "sub": "   "})
        assert try_resolve_email(token) is None


class TestValidateSubShape:
    def test_missing_sub_is_permitted(self) -> None:
        validate_sub_shape({})  # must not raise

    def test_non_string_sub_is_stringified_before_validation(self) -> None:
        # An int sub has no "@" and isn't blank once stringified — permitted.
        validate_sub_shape({"sub": 12345})  # must not raise
