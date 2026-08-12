"""Tests for auth.py's OktaOIDCProxy._extract_upstream_claims -- the
Okta id_token decode/validation path, previously untested (Argus round 3
finding)."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from auth import OktaOIDCProxy


def _id_token(header: dict, payload: dict) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}.sig"


@pytest.fixture
def proxy() -> OktaOIDCProxy:
    return OktaOIDCProxy.__new__(OktaOIDCProxy)  # _extract_upstream_claims needs no state


class TestExtractUpstreamClaims:
    async def test_alg_none_rejected(self, proxy: OktaOIDCProxy) -> None:
        token = _id_token({"alg": "none"}, {"sub": "x", "email": "a@b.com"})
        with patch("auth.log_security_event") as mock_log:
            result = await proxy._extract_upstream_claims({"id_token": token})
        assert result is None
        mock_log.assert_called_once_with(
            "okta_id_token_rejected", reason="alg_none", severity="critical"
        )

    async def test_valid_token_extracts_claims(self, proxy: OktaOIDCProxy) -> None:
        token = _id_token(
            {"alg": "RS256"}, {"sub": "x", "email": "a@b.com", "preferred_username": "a"}
        )
        result = await proxy._extract_upstream_claims({"id_token": token})
        assert result == {"sub": "x", "email": "a@b.com", "preferred_username": "a"}

    async def test_non_object_header_rejected(self, proxy: OktaOIDCProxy) -> None:
        header_b64 = base64.urlsafe_b64encode(b'"just a string"').rstrip(b"=").decode()
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).rstrip(b"=").decode()
        )
        token = f"{header_b64}.{payload_b64}.sig"
        with patch("auth.log_security_event") as mock_log:
            result = await proxy._extract_upstream_claims({"id_token": token})
        assert result is None
        mock_log.assert_called_once_with("okta_id_token_rejected", reason="non_object_header")

    async def test_malformed_base64_logs_decode_failed_with_error_type(
        self, proxy: OktaOIDCProxy
    ) -> None:
        token = "not-valid-base64!!!.also-not-valid.sig"
        with patch("auth.log_security_event") as mock_log:
            result = await proxy._extract_upstream_claims({"id_token": token})
        assert result is None
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args == ("okta_id_token_rejected",)
        assert kwargs["reason"] == "decode_failed"
        assert "error_type" in kwargs
        assert "exc_info" not in kwargs  # Argus round 3: deliberately not passed here

    async def test_no_id_token_returns_none_without_logging(self, proxy: OktaOIDCProxy) -> None:
        with patch("auth.log_security_event") as mock_log:
            result = await proxy._extract_upstream_claims({})
        assert result is None
        mock_log.assert_not_called()
