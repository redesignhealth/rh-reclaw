"""Structured JSON logging and observability events for reclaw-ea-mcp.

Uses ``structlog`` (the RH observability standard, topics/08-observability.md)
configured for JSON output to stdout, which the ECS log driver ships to
CloudWatch. The event schema matches the MCP fleet's shared
``mcp_observability`` contract (rh-data-platform ``services/shared``) so
CloudWatch Metric Filters and Logs Insights queries written for rh-mcp /
rh-google-mcp / reclaw-comms-mcp (``$.event = "tool_call"``,
``$.event = "scope_denial"``, ...) work unchanged against this service.
Identical to reclaw-comms-mcp's own copy one directory up except for
``SERVICE_NAME`` -- see that module's docstring for the full event schema.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import structlog

SERVICE_NAME = "reclaw-ea-mcp"

# Fallback logger for observability helpers' own failure paths, and for
# ``_resolve_log_level`` (structlog isn't configured yet at that point).
_fallback_logger = logging.getLogger(__name__)


def _resolve_log_level() -> int:
    """Resolve the numeric logging level from ``LOG_LEVEL`` (default ``INFO``).

    See reclaw-comms-mcp/observability.py's version of this function for
    the full rationale (shared verbatim here).
    """
    name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, name, None)
    if not isinstance(level, int) or level == logging.NOTSET:
        _fallback_logger.warning(
            "Invalid LOG_LEVEL=%r; falling back to INFO", os.environ.get("LOG_LEVEL")
        )
        return logging.INFO
    return level


def configure_logging() -> None:
    """Configure stdlib + structlog for JSON output to stdout.

    Idempotent -- safe to call from both ``main`` and tests.
    """
    level = _resolve_log_level()
    logging.basicConfig(level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


# Bound logger for structured observability events. structlog's JSONRenderer
# emits the positional message under the ``event`` key, matching the fleet's
# ``$.event = "<name>"`` Metric Filter contract.
obs_log = structlog.get_logger(service=SERVICE_NAME)


def hash_user(email: str) -> str:
    """Return the email local-part (before ``@``) for log attribution.

    Internal users only -- no privacy concern in the local part. rh-auth
    service slugs (no ``@``) pass through whole, so service tokens surface
    unchanged in CloudWatch under ``user_id``.
    """
    return email.strip().split("@")[0]


def log_tool_call(
    tool: str,
    duration_ms: float,
    success: bool,
    error_type: str | None = None,
    email: str | None = None,
) -> None:
    """Emit a ``tool_call`` observability event."""
    try:
        fields: dict[str, Any] = {
            "tool": tool,
            "duration_ms": round(duration_ms, 1),
            "success": success,
        }
        if error_type is not None:
            fields["error_type"] = error_type
        if email and email.strip():
            fields["user_id"] = hash_user(email)
        obs_log.info("tool_call", **fields)
    except Exception:
        _fallback_logger.warning("log_tool_call failed to emit event", exc_info=True)


def log_user_active(email: str) -> None:
    """Emit a ``user_active`` event for unique-user counting."""
    try:
        obs_log.info("user_active", user_id=hash_user(email))
    except Exception:
        _fallback_logger.warning("log_user_active failed to emit event", exc_info=True)


def log_auth_flow(
    auth_type: Literal["new_auth", "token_refresh"],
) -> None:
    """Emit an ``auth_flow`` event (browser auth completed / token refreshed)."""
    try:
        obs_log.info("auth_flow", auth_type=auth_type)
    except Exception:
        _fallback_logger.warning("log_auth_flow failed to emit event", exc_info=True)


def log_auth_rejected(
    reason: Literal["sub_missing", "sub_shape"],
    issuer: str | None = None,
) -> None:
    """Emit an ``auth_rejected`` event for a post-signature guard hit.

    Deliberately does NOT log the rejected ``sub`` -- the sub of a forged
    rh-auth token IS the attacker's payload; logging it would turn the
    metric stream into an attacker-writable side channel.
    """
    try:
        fields: dict[str, Any] = {"reason": reason}
        if issuer is not None:
            fields["issuer"] = issuer
        obs_log.info("auth_rejected", **fields)
    except Exception:
        _fallback_logger.warning("log_auth_rejected failed to emit event", exc_info=True)


def log_scope_denial(
    *,
    tool: str,
    reason: str,
    client_id: str,
    required_scope: str | None = None,
) -> None:
    """Emit a ``scope_denial`` event.

    structlog's JSONRenderer escapes the user-controlled ``tool`` value, so
    crafted tool names cannot inject log lines. ``required_scope`` is
    included only on the ``missing_scope`` branch (server-side debugging);
    the client-facing denial message never reveals it (anti-enumeration).
    """
    try:
        fields: dict[str, Any] = {
            "tool": tool,
            "reason": reason,
            "client_id": client_id,
        }
        if required_scope is not None:
            fields["required_scope"] = required_scope
        obs_log.info("scope_denial", **fields)
    except Exception:
        _fallback_logger.warning("log_scope_denial failed to emit event", exc_info=True)


__all__ = [
    "SERVICE_NAME",
    "configure_logging",
    "hash_user",
    "log_auth_flow",
    "log_auth_rejected",
    "log_scope_denial",
    "log_tool_call",
    "log_user_active",
    "obs_log",
]
