"""Tests for auth.py's ``OktaOIDCProxy._extract_upstream_claims``.

Mirrors ``tests/test_main.py``'s idiom: the OIDC discovery fetch is patched
out so tests never touch the network, and the required auth env vars are
provided (via ``_ENV_PATCH``, on top of ``conftest.py``'s autouse
``_auth_env`` fixture) so ``build_okta_provider()`` can construct a real
``OktaOIDCProxy`` without hitting Okta.
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import MagicMock, patch

from auth import OktaOIDCProxy, build_okta_provider

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
    },
)


def _build_proxy() -> OktaOIDCProxy:
    with _OIDC_PATCH, _ENV_PATCH:
        return build_okta_provider()


def _fake_id_token(payload: dict[str, object], alg: str = "RS256") -> str:
    """Build a well-formed-but-unsigned JWT string carrying ``payload``.

    ``_extract_upstream_claims`` decodes the id_token payload WITHOUT
    verifying its signature (safe only because the parent ``OIDCProxy`` has
    already verified it earlier in the OAuth exchange — see the comment
    added next to the real implementation), so the signature segment here
    is an arbitrary placeholder; only the header/payload base64 segments
    need to be well-formed.

    ``alg`` defaults to ``"RS256"`` (Okta's real signing algorithm) rather
    than ``"none"`` — the ``alg: none`` guard in ``_extract_upstream_claims``
    now rejects tokens outright, so the "happy path" fixtures need a
    realistic header. Pass ``alg="none"`` explicitly to exercise that guard.
    """

    def _b64(data: dict[str, object] | bytes) -> str:
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = _b64({"alg": alg, "typ": "JWT"})
    body = _b64(payload)
    return f"{header}.{body}.fake-signature"


class TestExtractUpstreamClaims:
    async def test_valid_id_token_extracts_expected_claims(self) -> None:
        proxy = _build_proxy()
        id_token = _fake_id_token(
            {
                "sub": "okta-sub-123",
                "email": "person@redesignhealth.com",
                "preferred_username": "person@redesignhealth.com",
                "name": "Person Name",
                "iat": 1700000000,
                "exp": 1700003600,
            }
        )

        claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims == {
            "sub": "okta-sub-123",
            "email": "person@redesignhealth.com",
            "preferred_username": "person@redesignhealth.com",
            "name": "Person Name",
        }

    async def test_id_token_with_only_some_expected_claims(self) -> None:
        proxy = _build_proxy()
        id_token = _fake_id_token({"sub": "okta-sub-456", "iat": 1700000000})

        claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims == {"sub": "okta-sub-456"}

    async def test_missing_id_token_key_returns_none(self) -> None:
        proxy = _build_proxy()

        with patch("auth.log_security_event") as mock_log:
            assert await proxy._extract_upstream_claims({}) is None
            assert await proxy._extract_upstream_claims({"access_token": "irrelevant"}) is None
        # No id_token at all isn't a rejection -- there's nothing to reject,
        # so this must not emit a spurious okta_id_token_rejected event.
        mock_log.assert_not_called()

    async def test_empty_id_token_value_returns_none(self) -> None:
        proxy = _build_proxy()

        assert await proxy._extract_upstream_claims({"id_token": ""}) is None

    async def test_malformed_id_token_returns_none_and_logs_decode_failed(self) -> None:
        proxy = _build_proxy()

        with patch("auth.log_security_event") as mock_log:
            claims = await proxy._extract_upstream_claims({"id_token": "not-a-jwt-at-all"})

        assert claims is None
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args == ("okta_id_token_rejected",)
        assert kwargs["reason"] == "decode_failed"
        assert "error_type" in kwargs
        # JSONDecodeError.doc carries the raw (attacker-controlled) decoded
        # payload -- exc_info must not be passed here, unlike other
        # log_security_event call sites in this module.
        assert "exc_info" not in kwargs

    async def test_truncated_base64_payload_returns_none(self) -> None:
        proxy = _build_proxy()
        # Well-formed, valid-JSON header (so the alg=none guard's JSON
        # parse succeeds and execution reaches the payload-decode step),
        # but the payload segment is not valid base64/JSON once decoded.
        valid_header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            .decode()
            .rstrip("=")
        )
        truncated = f"{valid_header}.not-valid-base64-json!!!.sig"

        with patch("auth.log_security_event") as mock_log:
            claims = await proxy._extract_upstream_claims({"id_token": truncated})

        assert claims is None
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args == ("okta_id_token_rejected",)
        assert kwargs["reason"] == "decode_failed"
        assert "error_type" in kwargs

    async def test_non_dict_header_returns_none_and_logs_event(self) -> None:
        # A header segment that base64-decodes to valid-but-non-dict JSON
        # (e.g. a JSON array) must not reach `header.get(...)` — that would
        # raise an unhandled AttributeError instead of failing closed.
        proxy = _build_proxy()
        non_dict_header = (
            base64.urlsafe_b64encode(json.dumps(["alg", "RS256"]).encode()).decode().rstrip("=")
        )
        id_token = f"{non_dict_header}.eyJzdWIiOiJ4In0.sig"

        with patch("auth.log_security_event") as mock_log:
            claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims is None
        mock_log.assert_called_once_with("okta_id_token_rejected", reason="non_object_header")

    async def test_non_dict_payload_returns_none_and_logs_event(self) -> None:
        # A payload segment that base64-decodes to valid-but-non-dict JSON
        # (e.g. a JSON array) must not reach `payload[k]`-style dict access
        # -- that would raise instead of failing closed. Mirrors the header
        # guard's ``test_non_dict_header_returns_none_and_logs_event`` above.
        proxy = _build_proxy()
        valid_header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            .decode()
            .rstrip("=")
        )
        non_dict_payload = (
            base64.urlsafe_b64encode(json.dumps(["sub", "x"]).encode()).decode().rstrip("=")
        )
        id_token = f"{valid_header}.{non_dict_payload}.sig"

        with patch("auth.log_security_event") as mock_log:
            claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims is None
        mock_log.assert_called_once_with("okta_id_token_rejected", reason="non_object_payload")

    async def test_alg_none_id_token_returns_none_and_logs_critical_event(self) -> None:
        # Defense-in-depth guard: even though signature verification is the
        # parent OIDCProxy's job, an alg=none header must be rejected here
        # rather than have its claims extracted and trusted.
        proxy = _build_proxy()
        id_token = _fake_id_token({"sub": "attacker-controlled"}, alg="none")

        with patch("auth.log_security_event") as mock_log:
            claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims is None
        mock_log.assert_called_once_with(
            "okta_id_token_rejected", reason="alg_none", severity="critical"
        )

    async def test_alg_none_case_insensitive_returns_none(self) -> None:
        proxy = _build_proxy()
        id_token = _fake_id_token({"sub": "attacker-controlled"}, alg="None")

        claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims is None

    async def test_id_token_with_no_recognized_claims_returns_none(self) -> None:
        """Every claim key present but none of them in ``_UPSTREAM_CLAIM_KEYS``
        — the ``claims or None`` fallback must turn an empty dict into ``None``,
        not an empty-but-truthy-shaped dict."""
        proxy = _build_proxy()
        id_token = _fake_id_token({"iat": 1700000000, "exp": 1700003600, "aud": "test-id"})

        claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims is None
