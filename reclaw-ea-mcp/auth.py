"""Okta OIDC + rh-auth JWT authentication for reclaw-ea-mcp.

Same fleet pattern as reclaw-comms-mcp/auth.py one directory up (both
adapted from rh-data-platform ``services/rh-mcp/auth.py``, the reference
implementation named in the RH tech guide, topics/04-auth-and-identity.md
§MCP Server Auth). See that module's docstring for the full auth-path
writeup; only the differences are called out here.

KNOWN DIVERGENCE (not yet reconciled): reclaw-comms-mcp/auth.py has since
gained the refresh-token rotation-grace mechanism (``_ROTATION_*``) to fix
forced Okta re-auths under concurrent connections sharing one cached
refresh token. This service runs the identical FastMCP OIDCProxy + Okta
app combination and is subject to the same one-time-use rotation behavior,
but has not been ported yet. See docs/proposals/reclaw-ea-plugin-registry.md
§1 for the broader plan to reconcile these two auth.py files.

Auth paths
----------
Interactive users (Claude Code, Claude Desktop, browser):
    Okta OIDC via FastMCP ``OIDCProxy``.

Programmatic callers (the reclaw-ea agent run-loop host, TECH-5084):
    rh-auth HS256 Bearer token, minted per-owner per-run (TECH-5065's auth
    invariant: this service never accepts an owner identity as a request
    parameter -- only from a verified token's claims, resolved via
    ``identity.require_owner_identity``).

Health check (``/health``): unauthenticated -- handled by FastMCP before
auth middleware runs.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from mcp.server.auth.provider import AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from identity import RH_AUTH_ISSUER
from observability import log_auth_flow, log_security_event

_UPSTREAM_CLAIM_KEYS = ["sub", "email", "preferred_username", "name"]

_DEFAULT_TOKEN_STORAGE_PATH = "/data/fastmcp-tokens"  # EFS-backed on ECS


def require_env(name: str) -> str:
    """Return a required environment variable, failing fast if missing/empty.

    No silent defaults for secrets (RH security standard): a missing
    required value must crash at startup, not surface later as a broken
    auth path.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            "See .env.example for the full list of required configuration."
        )
    return value


class OktaOIDCProxy(OIDCProxy):
    """OIDC proxy configured for Okta SSO (copied idiom from rh-mcp /
    reclaw-comms-mcp). See reclaw-comms-mcp/auth.py's version of this class
    for the full security rationale (identical here, verbatim)."""

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        """Decode the Okta ID token and extract identity claims.

        SECURITY (call-ordering dependency): this decodes the id_token
        payload WITHOUT verifying its signature. That is safe ONLY because
        the parent ``OIDCProxy`` has already verified the token earlier in
        the OAuth exchange -- if that call ordering ever changes, this
        becomes a forgeable trust boundary.

        Defense-in-depth only: reject ``alg: none`` tokens outright.
        """
        id_token = idp_tokens.get("id_token")
        if not id_token:
            return None
        try:
            header_b64 = id_token.split(".")[0]
            header_b64 += "=" * (-len(header_b64) % 4)
            header = json.loads(base64.urlsafe_b64decode(header_b64))
            if not isinstance(header, dict):
                log_security_event("okta_id_token_rejected", reason="non_object_header")
                return None
            if str(header.get("alg", "")).lower() == "none":
                # `severity="critical"` (Argus round 2 finding): this is an
                # explicit signature-bypass attempt, qualitatively more
                # adversarial than a routine scope mismatch or a malformed
                # token -- `log_security_event` always logs at `warning`
                # (structlog has no separate "critical" level), so this
                # field lets a CloudWatch Metric Filter/alarm distinguish
                # it from the other `okta_id_token_rejected` reasons below
                # without needing a dedicated log-level-based alarm.
                log_security_event("okta_id_token_rejected", reason="alg_none", severity="critical")
                return None
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            if not isinstance(payload, dict):
                log_security_event("okta_id_token_rejected", reason="non_object_payload")
                return None
            claims = {k: payload[k] for k in _UPSTREAM_CLAIM_KEYS if k in payload}
            return claims or None
        except (IndexError, json.JSONDecodeError, ValueError) as exc:
            # Argus round 3 finding: no `exc_info=True` here, unlike other
            # log_security_event call sites -- `JSONDecodeError.doc` holds
            # the raw (attacker-controlled) decoded token payload that
            # failed to parse; today's ExceptionRenderer only formats a
            # traceback string and doesn't dump exception attributes, but
            # that safety is implicit in the current renderer, not a
            # property of this call site. `error_type` alone is enough to
            # distinguish this failure mode without depending on it.
            log_security_event(
                "okta_id_token_rejected", reason="decode_failed", error_type=type(exc).__name__
            )
            return None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        result = await super().exchange_authorization_code(client, authorization_code)
        log_auth_flow("new_auth")
        return result

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        log_auth_flow("token_refresh")
        return result


def _build_client_storage(storage_path: str) -> FileTreeStore:
    """Build a FileTreeStore with standard V1 sanitization for CIMD-safe keys."""
    storage_root = Path(storage_path)
    storage_root.mkdir(parents=True, exist_ok=True)
    return FileTreeStore(
        data_directory=storage_path,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_root),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_root),
    )


def build_okta_provider() -> OktaOIDCProxy:
    """Build an OktaOIDCProxy from environment variables.

    Required environment variables (fail-fast if missing):
        OKTA_ISSUER_URL, OKTA_CLIENT_ID, OKTA_CLIENT_SECRET, MCP_JWT_SECRET

    Optional environment variables:
        BASE_URL (default: http://localhost:8081, for local dev -- one port
            off reclaw-comms-mcp's 8080 default so both can run side by
            side locally without a port collision)
        MCP_TOKEN_STORAGE_PATH (default: /data/fastmcp-tokens, EFS-backed)
    """
    issuer_url = require_env("OKTA_ISSUER_URL")
    config_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    storage_path = os.environ.get("MCP_TOKEN_STORAGE_PATH", _DEFAULT_TOKEN_STORAGE_PATH)

    return OktaOIDCProxy(
        config_url=config_url,
        client_id=require_env("OKTA_CLIENT_ID"),
        client_secret=require_env("OKTA_CLIENT_SECRET"),
        base_url=os.environ.get("BASE_URL", "http://localhost:8081"),
        required_scopes=["openid", "email", "profile", "offline_access"],
        jwt_signing_key=require_env("MCP_JWT_SECRET"),
        client_storage=_build_client_storage(storage_path),
        verify_id_token=True,
        enable_cimd=False,
    )


def build_auth_provider() -> MultiAuth:
    """Compose Okta OIDC + rh-auth JWT verification via FastMCP MultiAuth.

    Required environment variables:
        RH_AUTH_SECRET: Shared HS256 secret for rh-auth tokens.
        (Plus everything required by ``build_okta_provider``.)
    """
    return MultiAuth(
        server=build_okta_provider(),
        verifiers=[
            JWTVerifier(
                public_key=require_env("RH_AUTH_SECRET"),
                algorithm="HS256",
                issuer=RH_AUTH_ISSUER,
            ),
        ],
        # CRITICAL (same reasoning as reclaw-comms-mcp/auth.py): do NOT
        # inherit the Okta provider's required_scopes here -- MultiAuth's
        # bearer middleware enforces required_scopes against EVERY verified
        # token, including rh-auth M2M JWTs, which never carry `openid`.
        required_scopes=[],
    )
