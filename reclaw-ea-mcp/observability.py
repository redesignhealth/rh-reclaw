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

    Safe to call multiple times with the same configuration (``main`` and
    tests both call this) -- ``structlog.configure`` overwrites the global
    config unconditionally on every call rather than no-op'ing after the
    first, so this is NOT idempotent in the strict sense (a prior call's
    config is discarded, not preserved); it is safe here only because every
    caller passes the same configuration.
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


def email_local_part(email: str) -> str:
    """Return the email local-part (before ``@``) for log attribution.

    Named for what it does (Argus round 1 finding: the previous name,
    ``hash_user``, implied a one-way irreversible transformation this
    function does not perform -- this is plain-text truncation, not a
    hash, and provides no anonymization guarantee). Internal users only --
    no privacy concern in the local part. rh-auth service slugs (no ``@``)
    pass through whole, so service tokens surface unchanged in CloudWatch
    under ``user_id``.
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
            fields["user_id"] = email_local_part(email)
        obs_log.info("tool_call", **fields)
    except Exception:
        _fallback_logger.warning("log_tool_call failed to emit event", exc_info=True)


def log_user_active(email: str) -> None:
    """Emit a ``user_active`` event for unique-user counting."""
    try:
        obs_log.info("user_active", user_id=email_local_part(email))
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

    ``warning``, not ``info`` (Argus round 1 finding): this is an
    adversarial-signal event (an access-control failure), not routine
    traffic -- at ``info`` it's indistinguishable from normal tool calls
    for any log-level-based alarm.

    Deliberately does NOT log the rejected ``sub`` -- the sub of a forged
    rh-auth token IS the attacker's payload; logging it would turn the
    metric stream into an attacker-writable side channel.
    """
    try:
        fields: dict[str, Any] = {"reason": reason}
        if issuer is not None:
            fields["issuer"] = issuer
        obs_log.warning("auth_rejected", **fields)
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

    ``warning``, not ``info`` (Argus round 1 finding, same reasoning as
    ``log_auth_rejected``): an access-control failure, not routine traffic.

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
        obs_log.warning("scope_denial", **fields)
    except Exception:
        _fallback_logger.warning("log_scope_denial failed to emit event", exc_info=True)


def log_security_event(event: str, **fields: Any) -> None:
    """Emit an arbitrary security-relevant event through the structured
    JSON pipeline, at ``warning``.

    Added (Argus round 1 finding) for security-sensitive paths that
    previously used ``logging.getLogger(__name__)`` directly (Okta
    id_token decode/validation failures in auth.py, identity-resolution
    failures in main.py's ``ObservabilityMiddleware``): those calls emit
    unstructured plain-text output, since ``configure_logging`` wires two
    INDEPENDENT pipelines (``logging.basicConfig``'s plain-text
    StreamHandler and structlog's JSONRenderer) with no bridge between
    them -- a CloudWatch Metric Filter keyed on ``$.event`` silently misses
    every stdlib ``logger.*`` call. This is exactly the class of event
    most likely to matter during an incident, so it must flow through the
    same JSON pipeline as every other observability event in this module.
    """
    try:
        obs_log.warning(event, **fields)
    except Exception:
        _fallback_logger.warning("log_security_event failed to emit event", exc_info=True)


__all__ = [
    "SERVICE_NAME",
    "configure_logging",
    "email_local_part",
    "log_auth_flow",
    "log_auth_rejected",
    "log_scope_denial",
    "log_security_event",
    "log_tool_call",
    "log_user_active",
    "obs_log",
]
