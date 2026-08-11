"""Comms provider — placeholder tool surface.

The comms domain design (message schema, permission model, negotiation
state) is NOT final. This module carries a single ``whoami`` placeholder
tool that establishes the pattern domain tools will follow:

1. Register the tool on ``comms_server`` here.
2. Enroll its mounted name (``comms_<tool>``) in ``scopes.TOOL_SCOPES`` in
   the same PR (fail-closed default otherwise).
3. Read caller identity from ``get_access_token()`` — never from tool
   arguments.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from identity import try_resolve_email
from scopes import is_interactive_token, scopes_for_token

comms_server: FastMCP[Any] = FastMCP("comms")


@comms_server.tool
async def whoami() -> dict[str, Any]:
    """Return the authenticated caller's identity, issuer, caller type, and scopes.

    Diagnostic/placeholder tool: use it to verify that auth (Okta OIDC for
    humans, rh-auth Bearer JWT for agents) and scope enforcement are wired
    correctly. ``scopes`` is the rh-auth ``scopes`` claim for service
    callers; empty for interactive Okta callers (who bypass scope checks).
    """
    token = get_access_token()
    if token is None:
        # Defense in depth — FastMCP should never dispatch unauthenticated
        # calls, and ScopeEnforcementMiddleware already rejects them.
        raise ToolError("no access token provided")
    interactive = is_interactive_token(token)
    return {
        "identity": try_resolve_email(token),
        "issuer": token.claims.get("iss"),
        "caller_type": "interactive" if interactive else "service",
        "scopes": scopes_for_token(token),
    }
