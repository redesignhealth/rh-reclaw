"""Exceptions raised by the comms service layer (``service.py``).

Stage 3 (the not-yet-built MCP tools layer) catches these and maps them to
``fastmcp.exceptions.ToolError`` messages. Only three shapes ever cross the
service/tools boundary:

- ``AccessDeniedError``: the uniform "not authorized for this resource"
  denial (DESIGN.md §4/§8's anti-enumeration rule), covering both
  conversation-membership denials and task-admission denials
  (``denied.not_same_owner``/``denied.ownership_unverified``, TECH-5094).
  ``str()`` of every ``AccessDeniedError`` instance is the *same constant
  string*, regardless of cause — not a participant, invited-but-not-
  accepted, left/declined, an unknown/inactive target agent, a target
  that doesn't accept the conversation type, or an unadmitted task
  assignment. The specific cause is available to server-side code only
  via the ``reason`` attribute (mirrored 1:1 into the audit log's
  ``action`` column by ``service._deny``); it must never be interpolated
  into a client-visible message.

  Unknown-agent and type-not-accepted during ``start_conversation``/
  ``invite`` are deliberately folded into this SAME uniform shape rather
  than given their own specific message. DESIGN.md's uniform-denial rule is
  stated in terms of conversations, not agents, but this service leans
  toward not leaking agent existence either: whether a given sub is a
  board agent at all is exactly the kind of fact an internal-trust-domain
  service should not need to confirm to a caller who guesses at it, and
  unifying costs nothing (the caller already knows which agents it named).

- ``InvalidConversationStateError``: a message type is not legal given the
  conversation's current state (state-machine violation — posting after
  completion/cancellation/expiry). Kept distinct and specific: the caller
  is already an authorized member and has legitimate access to
  ``conversation.state`` via ``get_conversation``, so there is nothing to
  enumerate here.

- ``RateLimitExceededError``: a sender exceeded a per-hour cap. Specific by
  design — DESIGN.md does not treat rate limiting as an enumeration risk.

Payload/schema validation failures are NOT redefined here: they reuse
``schemas.PayloadValidationError`` directly, which is already a distinct,
specific exception type.
"""

from __future__ import annotations

_ACCESS_DENIED_MESSAGE = "access_denied: not authorized for this resource"


class AccessDeniedError(Exception):
    """Uniform denial for every conversation/agent authorization failure.

    ``str(exc)`` is always the fixed ``_ACCESS_DENIED_MESSAGE`` string. The
    ``reason`` attribute (matches the audit log's ``action`` for this
    denial) is for server-side logging only.
    """

    def __init__(self, *, reason: str) -> None:
        super().__init__(_ACCESS_DENIED_MESSAGE)
        self.reason = reason


class InvalidConversationStateError(Exception):
    """A message type is not legal in the conversation's current state."""


class RateLimitExceededError(Exception):
    """A sender exceeded a per-hour rate limit. Message is specific by design."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


__all__ = [
    "AccessDeniedError",
    "InvalidConversationStateError",
    "RateLimitExceededError",
]
