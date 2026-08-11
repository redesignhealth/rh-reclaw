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


def _fake_id_token(payload: dict[str, object]) -> str:
    """Build a well-formed-but-unsigned JWT string carrying ``payload``.

    ``_extract_upstream_claims`` decodes the id_token payload WITHOUT
    verifying its signature (safe only because the parent ``OIDCProxy`` has
    already verified it earlier in the OAuth exchange — see the comment
    added next to the real implementation), so the signature segment here
    is an arbitrary placeholder; only the header/payload base64 segments
    need to be well-formed.
    """

    def _b64(data: dict[str, object] | bytes) -> str:
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = _b64({"alg": "none", "typ": "JWT"})
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

        assert await proxy._extract_upstream_claims({}) is None
        assert await proxy._extract_upstream_claims({"access_token": "irrelevant"}) is None

    async def test_empty_id_token_value_returns_none(self) -> None:
        proxy = _build_proxy()

        assert await proxy._extract_upstream_claims({"id_token": ""}) is None

    async def test_malformed_id_token_returns_none_and_logs_error(self) -> None:
        proxy = _build_proxy()

        with patch("auth.logger") as mock_logger:
            claims = await proxy._extract_upstream_claims({"id_token": "not-a-jwt-at-all"})

        assert claims is None
        mock_logger.error.assert_called_once()

    async def test_truncated_base64_payload_returns_none(self) -> None:
        proxy = _build_proxy()
        # Well-formed 3-segment shape, but the payload segment is not valid
        # base64/JSON once decoded.
        truncated = "aGVhZGVy.not-valid-base64-json!!!.sig"

        with patch("auth.logger") as mock_logger:
            claims = await proxy._extract_upstream_claims({"id_token": truncated})

        assert claims is None
        mock_logger.error.assert_called_once()

    async def test_id_token_with_no_recognized_claims_returns_none(self) -> None:
        """Every claim key present but none of them in ``_UPSTREAM_CLAIM_KEYS``
        — the ``claims or None`` fallback must turn an empty dict into ``None``,
        not an empty-but-truthy-shaped dict."""
        proxy = _build_proxy()
        id_token = _fake_id_token({"iat": 1700000000, "exp": 1700003600, "aud": "test-id"})

        claims = await proxy._extract_upstream_claims({"id_token": id_token})

        assert claims is None
