"""JWT identity resolution for reclaw-comms-mcp.

Self-contained copy of the fleet-shared ``mcp_identity`` module
(rh-data-platform ``services/shared/mcp_identity.py``, TECH-3927/TECH-3928),
trimmed to what this service uses. This repo lives outside the
rh-data-platform monorepo, so it cannot take ``shared`` as a path
dependency; if the shared module is ever published to the internal Gitea
registry, replace this file with that dependency.

Threat model (unchanged from the shared module):

- Okta OIDC tokens carry identity in ``email`` / ``preferred_username``;
  their ``sub`` is an opaque Okta id.
- rh-auth Bearer JWTs carry identity in ``sub`` ONLY. The ``rh-auth issue``
  CLI accepts arbitrary ``--sub`` strings and arbitrary extra claims, so an
  rh-auth token's ``email`` claim is untrusted by design. Three
  impersonation variants are closed here:

  1. ``sub`` IS a victim's email (``--sub alice@redesignhealth.com``) —
     rejected by ``validate_sub_shape`` (no ``@`` allowed).
  2. Non-email ``sub`` + forged ``email`` claim — closed by gating the
     email-claim path on ``iss != "rh-auth"``.
  3. Whitespace/empty ``sub`` + forged ``email`` claim — rejected by
     ``validate_sub_shape``.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

__all__ = [
    "RH_AUTH_ISSUER",
    "try_resolve_email",
    "validate_sub_shape",
]

# Single source of truth for the rh-auth issuer string: used for
# cryptographic verification (``JWTVerifier(issuer=RH_AUTH_ISSUER)`` in
# auth.py) and for post-verification routing (scopes.py, this module).
RH_AUTH_ISSUER = "rh-auth"


def validate_sub_shape(claims: dict[str, Any]) -> None:
    """Reject the token if ``sub`` is email-shaped or empty/whitespace.

    Absent ``sub`` is permitted at this layer — Okta tokens resolve via the
    email-claim path and never need ``sub``. Error messages are deliberately
    generic: leaking the precise rejection criterion would hand the bypass
    condition to exactly the audience this gate targets.
    """
    sub = claims.get("sub")
    if sub is None:
        return
    sub_str = str(sub).strip()
    if not sub_str:
        raise ToolError("invalid token sub claim")
    if "@" in sub_str:
        raise ToolError("invalid token sub claim")


def _is_rh_auth_token(claims: dict[str, Any]) -> bool:
    """rh-auth tokens are identified by ``iss``, cryptographically verified
    upstream by the JWTVerifier before the token reaches this module.

    A missing/``None`` ``iss`` claim must NOT fall through to the "not
    rh-auth" branch — that branch trusts the token's ``email`` /
    ``preferred_username`` claims (the Okta/interactive path), and an absent
    issuer must not be treated as safely-interactive by default. This
    mirrors the fail-closed ``iss is None`` guard in
    ``scopes.is_interactive_token``, but here the stakes are higher: this
    value feeds ``try_resolve_email``, which becomes ``actor_sub`` — the
    identity key for every tool call — not just a scope-bypass decision.
    """
    issuer = claims.get("iss")
    if issuer is None:
        return True
    return bool(issuer == RH_AUTH_ISSUER)


def try_resolve_email(token: Any) -> str | None:
    """Best-effort identity extraction for observability and whoami.

    - rh-auth tokens (``iss == "rh-auth"``) resolve via ``sub`` only —
      their ``email``/``preferred_username`` claims are untrusted.
    - Other issuers resolve via ``email`` → ``preferred_username`` → ``sub``.

    Fails OPEN with ``None`` (no attribution) on rejection rather than
    raising, so an impersonation-shaped token cannot poison the
    unique-users metric but also cannot break the tool dispatch.
    """
    claims = token.claims
    try:
        validate_sub_shape(claims)
    except ToolError:
        return None
    if _is_rh_auth_token(claims):
        sub = claims.get("sub")
        if sub is None:
            return None
        return str(sub).strip()
    email = claims.get("email") or claims.get("preferred_username")
    if email:
        return str(email)
    sub = claims.get("sub")
    if sub is None:
        return None
    return str(sub).strip()
