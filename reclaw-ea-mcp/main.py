"""reclaw-ea-mcp entrypoint.

MCP service wrapping ``reclaw_ea.orchestrator.Negotiator`` -- per-owner
scheduling negotiation, holds/booking, and the autonomy gate (TECH-5065).
Mounts the ``ea`` provider behind Okta OIDC (humans) + rh-auth JWT (the
reclaw-ea agent run-loop host, TECH-5084) auth, with fail-closed per-tool
scope enforcement.

Structurally identical to reclaw-comms-mcp/main.py one directory up (same
fleet pattern, adapted from rh-data-platform ``services/rh-mcp/main.py``)
-- only the mounted provider, tool-scope registry, and instructions differ.
See that module's docstring for the full middleware-ordering rationale.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, NoReturn

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from auth import build_auth_provider
from identity import RH_AUTH_ISSUER, try_resolve_email
from observability import (
    configure_logging,
    log_scope_denial,
    log_tool_call,
    log_user_active,
)
from providers.ea import ea_server
from scopes import (
    is_interactive_token,
    required_scope_for,
    required_scope_for_resource,
    safe_client_id,
    scopes_for_token,
)

configure_logging()

logger = logging.getLogger(__name__)


_DENIAL_MESSAGE = "insufficient_scope: tool '{tool_name}' requires elevated permissions"
_RESOURCE_DENIAL_MESSAGE = "insufficient_scope: resource '{uri}' requires elevated permissions"


class ScopeEnforcementMiddleware(Middleware):
    """Enforce rh-auth scopes on every tool dispatch (TECH-2752 pattern).

    Identical shape to reclaw-comms-mcp/main.py's version -- see that
    class's docstring for the full behavior/ordering rationale. Runs
    *inside* ``ObservabilityMiddleware``: middleware are registered in
    outer->inner order, and observability is registered first. **Do not
    reorder middleware registration.**
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name: str = context.message.name
        token = get_access_token()

        if is_interactive_token(token):
            return await call_next(context)

        if token is None:
            self._deny(tool_name, reason="missing_token", client_id=None)

        required = required_scope_for(tool_name)
        if required is None:
            self._deny(tool_name, reason="tool_not_enrolled", client_id=safe_client_id(token))

        if required not in scopes_for_token(token):
            self._deny(
                tool_name,
                reason="missing_scope",
                client_id=safe_client_id(token),
                required_scope=required,
            )

        return await call_next(context)

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, Any],
    ) -> Any:
        """This service registers no resources today; fail-closed by default
        for any future one, mirroring reclaw-comms-mcp's identical hook."""
        uri = str(context.message.uri)
        token = get_access_token()

        if is_interactive_token(token):
            return await call_next(context)

        if token is None:
            self._deny_resource(uri, reason="missing_token", client_id=None)

        required = required_scope_for_resource(uri)
        if required is None:
            self._deny_resource(
                uri, reason="resource_not_enrolled", client_id=safe_client_id(token)
            )

        if required not in scopes_for_token(token):
            self._deny_resource(
                uri,
                reason="missing_scope",
                client_id=safe_client_id(token),
                required_scope=required,
            )

        return await call_next(context)

    @staticmethod
    def _deny(
        tool_name: str,
        *,
        reason: str,
        client_id: str | None,
        required_scope: str | None = None,
    ) -> NoReturn:
        log_scope_denial(
            tool=tool_name,
            reason=reason,
            client_id=client_id or "unknown",
            required_scope=required_scope,
        )
        raise ToolError(_DENIAL_MESSAGE.format(tool_name=tool_name))

    @staticmethod
    def _deny_resource(
        uri: str,
        *,
        reason: str,
        client_id: str | None,
        required_scope: str | None = None,
    ) -> NoReturn:
        log_scope_denial(
            tool=uri,
            reason=reason,
            client_id=client_id or "unknown",
            required_scope=required_scope,
        )
        raise ResourceError(_RESOURCE_DENIAL_MESSAGE.format(uri=uri))


class ObservabilityMiddleware(Middleware):
    """Emit a structured ``tool_call`` event for every tool dispatch.

    Identical shape to reclaw-comms-mcp/main.py's version -- see that
    class's docstring for the full identity-resolution rationale.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name: str = context.message.name
        t0 = time.monotonic()
        error_type: str | None = None
        try:
            result: ToolResult = await call_next(context)
            return result
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            email: str | None = None
            try:
                token = get_access_token()
                if token is not None:
                    if token.claims.get("iss") == RH_AUTH_ISSUER:
                        email = try_resolve_email(token)
                    else:
                        upstream: dict[str, Any] = token.claims.get("upstream_claims", {})
                        email = upstream.get("email") or try_resolve_email(token)
            except Exception:
                logger.warning("Failed to extract user identity for observability", exc_info=True)
            log_tool_call(
                tool=tool_name,
                duration_ms=duration_ms,
                success=error_type is None,
                error_type=error_type,
                email=email,
            )
            if email:
                log_user_active(email)


mcp: FastMCP[Any] = FastMCP(
    "reclaw-ea-mcp",
    instructions=(
        "Per-owner EA agent scheduling-negotiation service for Redesign Health. "
        "Wraps the reclaw-ea Negotiator: negotiation state, preference scoring, "
        "holds/booking discipline, and the autonomy gate. Use ea_negotiate to "
        "open a negotiation with another owner's EA, ea_react_to_conversation "
        "to process the next turn, ea_check_completion to check for a standing "
        "agreement, ea_request_booking to request booking through the autonomy "
        "gate once complete, and ea_respond_to_approval to resolve a pending "
        "booking-approval hold. ea_whoami returns the authenticated caller's "
        "identity and scopes. This service performs no LLM reasoning of its "
        "own -- callers supply already-scored candidate slots."
    ),
    auth=build_auth_provider(),
)

mcp.add_middleware(ObservabilityMiddleware())
mcp.add_middleware(ScopeEnforcementMiddleware())

mcp.mount(ea_server, namespace="ea")


if __name__ == "__main__":
    # Default host matches rh-mcp/reclaw-comms-mcp: on ECS the Tailscale
    # sidecar shares the task's network namespace, so the server binds
    # loopback only. Local docker-compose overrides MCP_HOST=0.0.0.0.
    # Port 8081, not reclaw-comms-mcp's 8080, so both can run side by side
    # locally without a collision (see auth.py's BASE_URL default).
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8081")),
    )
