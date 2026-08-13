"""Structured JSON logging and observability events for reclaw-comms-mcp.

Uses ``structlog`` (the RH observability standard, topics/08-observability.md)
configured for JSON output to stdout, which the ECS log driver ships to
CloudWatch. The event schema matches the MCP fleet's shared
``mcp_observability`` contract (rh-data-platform ``services/shared``) so
CloudWatch Metric Filters and Logs Insights queries written for rh-mcp /
rh-google-mcp (``$.event = "tool_call"``, ``$.event = "scope_denial"``, ...)
work unchanged against this service.

Event schema
------------
tool_call:
    {"event": "tool_call", "service": "reclaw-comms-mcp", "tool": "<name>",
     "duration_ms": 42.1, "success": true}
    On failure, adds ``"error_type": "<ExcClassName>"``. When identity is
    available, adds ``"user_id": "<local-part|service-slug>"``.

auth_flow:
    {"event": "auth_flow", "service": "...", "auth_type": "new_auth|token_refresh"}

auth_rejected:
    {"event": "auth_rejected", "service": "...",
     "reason": "sub_missing|sub_shape", "issuer": "rh-auth"}
    Logged at ``warning`` — an access-control failure, not routine traffic.

scope_denial:
    {"event": "scope_denial", "service": "...", "tool": "<name>",
     "reason": "missing_token|tool_not_enrolled|missing_scope",
     "client_id": "<bot subject>", "required_scope": "<scope>"}
    ``required_scope`` present on the missing_scope branch only — kept
    server-side; the client-facing error never reveals it. Logged at
    ``warning``, same reasoning as ``auth_rejected``.

user_active:
    {"event": "user_active", "service": "...", "user_id": "<local-part|slug>"}

Arbitrary security-relevant events (``log_security_event``) also flow
through this module at ``warning``, with an optional ``severity="critical"``
field for the subset (e.g. an explicit Okta ``alg: none`` signature-bypass
attempt) that a CloudWatch Metric Filter/alarm should distinguish from a
routine denial of the same event name.

Notes
-----
* Never log message content, tokens, or attacker-controlled claim values.
* ``user_id`` is the email local-part for humans, or the rh-auth service
  slug for M2M callers (see ``email_local_part``).
* Every ``log_*`` helper swallows its own failures — observability must
  never break the caller (same contract as the shared module).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import structlog

SERVICE_NAME = "reclaw-comms-mcp"

# Fallback logger for observability helpers' own failure paths, and for
# ``_resolve_log_level`` (structlog isn't configured yet at that point).
_fallback_logger = logging.getLogger(__name__)


def _resolve_log_level() -> int:
    """Resolve the numeric logging level from ``LOG_LEVEL`` (default ``INFO``).

    Extracted into its own function so both ``logging.basicConfig`` and
    structlog's filtering wrapper use the SAME computed level (previously
    only ``logging.basicConfig`` read ``LOG_LEVEL``; structlog's
    ``make_filtering_bound_logger`` was hardcoded to ``logging.INFO``, so
    ``LOG_LEVEL=DEBUG`` silently had no effect on structlog output). Falls
    back to ``INFO`` for an unset or invalid value rather than raising, and
    logs a warning (via the module-scoped ``_fallback_logger`` — structlog
    isn't configured yet at this point, and the bare ``logging.warning()``
    convenience function must NOT be used here: it implicitly calls
    ``logging.basicConfig()`` the first time it's invoked if the root
    logger has no handlers yet, which would make the real,
    intentional ``logging.basicConfig(level=level)`` call in
    ``configure_logging`` a no-op) so a typo'd env var doesn't silently
    produce the wrong level with no diagnostic trail.

    ``NOTSET`` (``logging.NOTSET == 0``) is treated as invalid too: it's an
    ``isinstance(level, int)``-passing integer, but it's a sentinel meaning
    "no filtering" rather than a real threshold, and
    ``structlog.make_filtering_bound_logger(0)`` would let every log level
    through unconditionally. That's not a sensible interpretation of a
    ``LOG_LEVEL`` env var, so it falls back to ``INFO`` like any other
    invalid value.
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

    Idempotent — safe to call from both ``main`` and tests.
    """
    level = _resolve_log_level()
    logging.basicConfig(level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Without this, `exc_info=True` passed to `obs_log.warning(...)`
            # (or `log_security_event`) produced a useless `{"exc_info":
            # true}` field in the JSON output -- the exception class and
            # traceback were silently dropped rather than rendered. Must run
            # BEFORE JSONRenderer so its output (a plain-text traceback
            # under the `exception` key) is itself serialized, not left as a
            # live exception object.
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

    Named for what it does — the previous name, ``hash_user``, implied a
    one-way irreversible transformation this function does not perform:
    this is plain-text truncation, not a hash, and provides no
    anonymization guarantee. Internal users only — no privacy concern in
    the local part. rh-auth service slugs (no ``@``) pass through whole, so
    service tokens surface unchanged in CloudWatch under ``user_id``.
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

    Deliberately does NOT log the rejected ``sub`` — the sub of a forged
    rh-auth token IS the attacker's payload; logging it would turn the
    metric stream into an attacker-writable side channel.

    Logged at ``warning``, not ``info``: this is an adversarial-signal
    event (an access-control failure), not routine traffic — at ``info``
    it's indistinguishable from normal tool calls for any log-level-based
    alarm.
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

    structlog's JSONRenderer escapes the user-controlled ``tool`` value, so
    crafted tool names cannot inject log lines. ``required_scope`` is
    included only on the ``missing_scope`` branch (server-side debugging);
    the client-facing denial message never reveals it (anti-enumeration).

    Logged at ``warning``, not ``info``, same reasoning as
    ``log_auth_rejected``: an access-control failure, not routine traffic.
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

    For security-sensitive paths that would otherwise use
    ``logging.getLogger(__name__)`` directly (e.g. Okta id_token
    decode/validation failures in ``auth.py``): those calls emit
    unstructured plain-text output, since ``configure_logging`` wires two
    INDEPENDENT pipelines (``logging.basicConfig``'s plain-text
    StreamHandler and structlog's JSONRenderer) with no bridge between
    them — a CloudWatch Metric Filter keyed on ``$.event`` silently misses
    every stdlib ``logger.*`` call. This is exactly the class of event
    most likely to matter during an incident, so it must flow through the
    same JSON pipeline as every other observability event in this module.

    ``exc_info=True`` only renders a traceback if this is called from
    inside an active ``except`` block — called elsewhere, structlog has no
    exception to capture and the field is silently omitted, not an error.

    ``severity`` is an optional field some callers pass (e.g.
    ``severity="critical"`` for an explicit signature-bypass attempt in
    auth.py) to let a downstream CloudWatch Metric Filter distinguish an
    especially adversarial event from a routine one of the same ``event``
    name — structlog itself has no level between ``warning`` and
    ``error`` for that distinction. Inert until a corresponding
    filter/alarm is actually configured; not yet wired up anywhere.
    Typed as a closed ``Literal`` rather than a free-form ``**fields``
    entry so a typo like ``"cricital"`` gets a type-check signal instead
    of silently emitting a non-matching value — add new values here as
    they gain a real meaning, not ad hoc at a call site.
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
