"""Okta OIDC + rh-auth JWT authentication for reclaw-comms-mcp.

Adapted from rh-data-platform ``services/rh-mcp/auth.py`` (the reference
implementation named in the RH tech guide, topics/04-auth-and-identity.md
§MCP Server Auth).

Auth paths
----------
Interactive users (Claude Code, Claude Desktop, browser):
    Okta OIDC via FastMCP ``OIDCProxy``. Uses ``verify_id_token=True`` so
    FastMCP validates the Okta id_token (which carries identity claims like
    ``email`` and ``preferred_username``) and makes those claims available
    to tools via ``get_access_token().claims``.

Programmatic callers (agents, services, CI jobs):
    rh-auth HS256 Bearer token issued by the Tech Team via ``rh-auth issue``.
    FastMCP ``MultiAuth`` composes the Okta OIDC provider with a
    ``JWTVerifier`` keyed to ``RH_AUTH_SECRET`` (injected from SSM
    ``/general/{env}/rh-auth-secret`` at ECS task start). Both humans and
    machines POST to the same ``/mcp`` endpoint.

Health check (``/health``):
    Unauthenticated — handled by FastMCP before auth middleware runs.

Deliberately omitted vs rh-mcp: the refresh-token rotation-grace machinery
(``_ROTATION_*``), which mitigates an rh-mcp-specific Claude Desktop
multi-connection issue at real production scale. Add it back (copy from
rh-mcp) if ``refresh_token_miss``-style forced re-auths show up here.
"""

from __future__ import annotations

import base64
import json
import logging
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
from observability import log_auth_flow

logger = logging.getLogger(__name__)

# Claims to extract from the Okta ID token into the FastMCP JWT.
_UPSTREAM_CLAIM_KEYS = ["sub", "email", "preferred_username", "name"]

_DEFAULT_TOKEN_STORAGE_PATH = "/data/fastmcp-tokens"  # EFS-backed on ECS


def require_env(name: str) -> str:
    """Return a required environment variable, failing fast if missing/empty.

    No silent defaults for secrets (RH security standard + the
    secret-loading-fallback-consistency failure mode): a missing required
    value must crash at startup, not surface later as a broken auth path.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            "See .env.example for the full list of required configuration."
        )
    return value


class OktaOIDCProxy(OIDCProxy):
    """OIDC proxy configured for Okta SSO (copied idiom from rh-mcp).

    ``_extract_upstream_claims`` embeds the Okta id_token identity claims in
    the FastMCP JWT as a fallback for code paths that read from there; with
    ``verify_id_token=True`` the claims are also available directly. Emits
    structured ``auth_flow`` events for CloudWatch Metric Filters.
    """

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        """Decode the Okta ID token and extract identity claims.

        SECURITY (call-ordering dependency): this decodes the id_token
        payload WITHOUT verifying its signature. That is safe ONLY because
        the parent ``OIDCProxy`` has already verified the token earlier in
        the OAuth exchange (this hook runs downstream of that verification,
        not before it) — if that call ordering ever changes, this becomes a
        forgeable trust boundary (an attacker-controlled payload would be
        trusted as identity claims).

        Defense-in-depth only (does not replace the ordering dependency
        above): reject ``alg: none`` tokens outright rather than extracting
        claims from them. This protects a misconfigured dev/test
        environment where the parent-class verification hasn't actually
        run yet — it is not a substitute for that verification.
        """
        id_token = idp_tokens.get("id_token")
        if not id_token:
            return None
        try:
            header_b64 = id_token.split(".")[0]
            header_b64 += "=" * (-len(header_b64) % 4)  # base64 padding
            header = json.loads(base64.urlsafe_b64decode(header_b64))
            if not isinstance(header, dict):
                logger.error("Rejecting Okta id_token with non-object header")
                return None
            if str(header.get("alg", "")).lower() == "none":
                logger.error("Rejecting Okta id_token with alg=none")
                return None
            # Decode the JWT payload without verification — the upstream
            # provider already validated the token during the auth flow.
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)  # base64 padding
            payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload_b64))
            claims = {k: payload[k] for k in _UPSTREAM_CLAIM_KEYS if k in payload}
            return claims or None
        except (IndexError, json.JSONDecodeError, ValueError):
            logger.error("Failed to decode Okta id_token for upstream claims")
            return None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Exchange authorization code and emit a ``new_auth`` event."""
        result = await super().exchange_authorization_code(client, authorization_code)
        log_auth_flow("new_auth")
        return result

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange refresh token and emit a ``token_refresh`` event."""
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        log_auth_flow("token_refresh")
        return result


def _build_client_storage(storage_path: str) -> FileTreeStore:
    """Build a FileTreeStore with standard V1 sanitization for CIMD-safe keys.

    The stock ``FileTreeV1`` strategies hash any key or collection name
    containing filesystem-unsafe characters, so URL-style client IDs are
    stored safely on the EFS-backed volume. The directory is created if
    missing (the sanitization strategies require an existing path).
    """
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
        OKTA_ISSUER_URL: Okta authorization server issuer URL
        OKTA_CLIENT_ID: Okta application client ID
        OKTA_CLIENT_SECRET: Okta application client secret
        MCP_JWT_SECRET: Stable JWT signing secret (provisioned by Terraform,
            injected via SSM)

    Optional environment variables (non-secret, documented defaults):
        BASE_URL: Public URL of this MCP server
            (default: http://localhost:8080, for local dev)
        MCP_TOKEN_STORAGE_PATH: Directory for persistent OAuth token storage
            (default: /data/fastmcp-tokens, EFS-backed on ECS)
    """
    issuer_url = require_env("OKTA_ISSUER_URL")
    config_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    storage_path = os.environ.get("MCP_TOKEN_STORAGE_PATH", _DEFAULT_TOKEN_STORAGE_PATH)

    return OktaOIDCProxy(
        config_url=config_url,
        client_id=require_env("OKTA_CLIENT_ID"),
        client_secret=require_env("OKTA_CLIENT_SECRET"),
        base_url=os.environ.get("BASE_URL", "http://localhost:8080"),
        # offline_access is required for Okta to issue a refresh_token;
        # without it clients must do a full re-auth every ~1 hour.
        required_scopes=["openid", "email", "profile", "offline_access"],
        jwt_signing_key=require_env("MCP_JWT_SECRET"),
        client_storage=_build_client_storage(storage_path),
        verify_id_token=True,
        # Matches rh-mcp (TECH-1943): FastMCP's CIMD redirect_uri validation
        # rejects Claude Code's dynamic-port loopback callbacks.
        enable_cimd=False,
    )


def build_auth_provider() -> MultiAuth:
    """Compose Okta OIDC + rh-auth JWT verification via FastMCP MultiAuth.

    MultiAuth tries the Okta OIDCProxy first (interactive users), then the
    JWTVerifier (programmatic rh-auth tokens). The two paths are fully
    independent.

    Required environment variables:
        RH_AUTH_SECRET: Shared HS256 secret for rh-auth tokens, injected
            from SSM ``/general/{env}/rh-auth-secret`` at ECS task start.
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
        # CRITICAL (copied from rh-mcp): do NOT inherit the Okta provider's
        # required_scopes (["openid", "email", "profile", "offline_access"]).
        # MultiAuth's bearer middleware enforces required_scopes against
        # EVERY verified token — including rh-auth M2M JWTs, which never
        # carry `openid`. Inheriting them silently rejects all rh-auth
        # tokens with `insufficient_scope` even after a valid signature.
        # Authorization for rh-auth callers is handled per-tool by
        # ScopeEnforcementMiddleware, so the provider-level gate must be
        # empty.
        required_scopes=[],
    )
