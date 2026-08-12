"""Structured JSON logging and observability events for reclaw-ea-mcp.

Uses ``structlog`` (the RH observability standard, topics/08-observability.md)
configured for JSON output to stdout, which the ECS log driver ships to
CloudWatch. The event schema matches the MCP fleet's shared
``mcp_observability`` contract (rh-data-platform ``services/shared``) so
CloudWatch Metric Filters and Logs Insights queries written for rh-mcp /
rh-google-mcp / reclaw-comms-mcp (``$.event = "tool_call"``,
``$.event = "scope_denial"``, ...) work unchanged against this service.
Started as an identical copy of reclaw-comms-mcp's own observability.py one
directory up (same ``SERVICE_NAME``-only difference at first), but has
since diverged (Argus round 3 finding: the docstring claiming otherwise
went stale) -- this module additionally has ``structlog.processors.
ExceptionRenderer()`` in the processor chain (so ``exc_info=True`` actually
renders a traceback instead of a bare ``{"exc_info": true}``) and
``log_security_event``, a general-purpose helper the sibling doesn't have.
See reclaw-comms-mcp/observability.py's docstring for the base event
schema (``tool_call``, ``auth_flow``, ``auth_rejected``, ``scope_denial``,
``user_active``), which both files still share.
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
            # Argus round 2 finding: without this, `exc_info=True` passed
            # to `obs_log.warning(...)` (or `log_security_event`) produced
            # a useless `{"exc_info": true}` field in the JSON output --
            # the exception class and traceback were silently dropped
            # rather than rendered. Must run BEFORE JSONRenderer so its
            # output (a plain-text traceback under the `exception` key) is
            # itself serialized, not left as a live exception object.
            structlog.processors.ExceptionRenderer(),
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


def log_security_event(
    event: str, *, severity: Literal["critical"] | None = None, **fields: Any
) -> None:
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

    ``exc_info=True`` (Argus round 3 finding, documented) only renders a
    traceback if this is called from inside an active ``except`` block --
    called elsewhere, structlog has no exception to capture and the field
    is silently omitted, not an error.

    ``severity`` is an optional field some callers pass (e.g.
    ``severity="critical"`` for an explicit signature-bypass attempt in
    auth.py) to let a downstream CloudWatch Metric Filter distinguish an
    especially adversarial event from a routine one of the same ``event``
    name -- structlog itself has no level between ``warning`` and
    ``error`` for that distinction. Inert until a corresponding
    filter/alarm is actually configured; not yet wired up anywhere.
    Typed as a closed ``Literal`` (Argus round 4 finding: previously just
    another free-form ``**fields`` entry, so a typo like ``"cricital"``
    would silently emit a non-matching value with no type-check signal) --
    add new values here as they gain a real meaning, not ad hoc at a call
    site.
    """
    if severity is not None:
        fields["severity"] = severity
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
