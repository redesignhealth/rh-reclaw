"""Versioned Pydantic payload schemas for typed board messages.

Every message posted to the board must validate against the schema
registered for ``(conversation_type, message_type, schema_version)`` —
there is no free text anywhere in v1 (DESIGN.md §6). Validation rules:

- ``extra="forbid"`` on every model (strict — unknown fields rejected).
- All datetimes are timezone-aware ISO 8601 (``AwareDatetime``); naive
  datetimes are rejected.
- Enumerated string fields are closed ``Literal`` sets. No free-text
  fields anywhere — every field is a bounded numeric/datetime/enum value
  or a bounded list of them.

Discriminator field: every top-level message model also carries a
``type: Literal[...]`` field matching the DB ``messages.type`` column
value, defaulted to that same literal. It is optional on input (callers
that only pass the ``message_type`` service-layer argument need not repeat
it in the payload) but always present in the normalized/dumped form, and
if a caller *does* include it, it must match — a defense-in-depth check
against a payload accidentally validated against the wrong schema class,
independent of the ``(conversation.type, message.type)`` lookup that
selects the schema in the first place.

The registry (``MESSAGE_SCHEMAS``, accessed via ``get_schema``) is the
single source of truth for which message types exist per conversation
type and schema version. Adding a message type or a new
``schema_version`` is a code change here plus (nothing else) — old
versions stay registered so historical payloads remain validatable.

Design note — ``availability_response``'s either/or shape (DESIGN.md §6:
"slots[...] max 10, OR none_available+reason"): modeled as a *single*
Pydantic model (``AvailabilityResponseV1``) with both branches' fields
optional, plus a ``model_validator(mode="after")`` enforcing exactly one
branch is populated, rather than a ``Union``/discriminated-union of two
model classes. Rationale: ``get_schema()`` returns a single
``type[BaseModel]`` per registry key so the (not-yet-built) service layer
can call ``schema_cls.model_validate(payload)`` uniformly for every
message type without special-casing a ``Union`` vs. a bare model class at
the call site. A discriminated union would need its own discriminator
field threaded through a ``TypeAdapter`` instead, which buys nothing here
once mutual exclusivity is enforced by the validator.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

# Conversation types known to the board (v1: scheduling only). Used to
# validate ``agents.accepted_types`` at bind time and ``conversations.type``
# at start time.
CONVERSATION_TYPES: frozenset[str] = frozenset({"scheduling.availability"})

# Schema-registry namespace for the ``tasks`` table's payload (TECH-5094)
# — NOT a member of ``CONVERSATION_TYPES``: agents cannot ``start_conversation``
# of this "type", it exists only as the first coordinate of the
# ``MESSAGE_SCHEMAS`` lookup key (reused for tasks, see below) and as the
# domain label in docs/audit output.
TASK_NAMESPACE = "internal.coordination"

# Shared size/count limits (DESIGN.md §8 "message size caps" invariant).
# Defined once here so the tool boundary (providers/comms.py), the service
# layer (service.py), and the DB model (models.py) all reference the same
# values rather than three independently-drifting literals.
MAX_PARTICIPANTS_PER_CONVERSATION = 50
MAX_DISPLAY_NAME_LENGTH = 255
MAX_ACCEPTED_TYPES = 20
MAX_PAYLOAD_BYTES = 65536

# Message types known to the board (v1: all under scheduling.availability).
# Mirrors the DB CHECK-free, code-owned open vocabulary described in
# models.py's module docstring.
MessageType = Literal[
    "availability_request",
    "availability_response",
    "counter_proposal",
    "confirm",
    "decline",
    "needs_clarification",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeWindow(_StrictModel):
    """A timezone-aware [start, end) interval; end must be after start."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _end_after_start(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Slot(_StrictModel):
    """A proposed meeting slot with a sender preference weight (0..1).

    This is the "judgment crosses the boundary" primitive (DESIGN.md §6):
    ``preference`` is a scored opinion, never raw calendar data — there is
    no field here for a raw calendar entry to travel in.
    """

    start: AwareDatetime
    end: AwareDatetime
    preference: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _end_after_start(self) -> Slot:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


_CONSTRAINTS = Literal["mornings_only", "afternoons_only", "avoid_fridays", "buffer_15min"]
_DECLINE_REASONS = Literal["owner_declined", "no_availability", "expired", "other"]
_NONE_AVAILABLE_REASONS = Literal["no_overlap", "window_too_narrow", "owner_unavailable"]


class AvailabilityRequestV1(_StrictModel):
    """scheduling.availability / availability_request / v1. Opens the negotiation."""

    type: Literal["availability_request"] = "availability_request"
    window: TimeWindow
    duration_min: int = Field(ge=5, le=480)
    modality: Literal["video", "phone", "in_person"]
    priority: Literal["low", "normal", "high"]
    constraints: list[_CONSTRAINTS] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _no_duplicate_constraints(self) -> AvailabilityRequestV1:
        if len(set(self.constraints)) != len(self.constraints):
            raise ValueError("constraints must not contain duplicates")
        return self


class AvailabilityResponseV1(_StrictModel):
    """scheduling.availability / availability_response / v1.

    Exactly one of two mutually-exclusive branches must be populated:

    - ``slots``: up to 10 candidate slots with sender preference weights.
    - ``none_available`` + ``reason``: no slots can be offered.

    See the module docstring for why this is one model with an
    exclusivity validator rather than a Union of two model classes.
    """

    type: Literal["availability_response"] = "availability_response"
    slots: list[Slot] | None = Field(default=None, max_length=10)
    none_available: Literal[True] | None = None
    reason: _NONE_AVAILABLE_REASONS | None = None

    @model_validator(mode="after")
    def _exactly_one_branch(self) -> AvailabilityResponseV1:
        has_slots = self.slots is not None
        has_none_available = self.none_available is not None
        if has_slots and has_none_available:
            raise ValueError("provide either 'slots' or 'none_available'+'reason', not both")
        if not has_slots and not has_none_available:
            raise ValueError("must provide either 'slots' or 'none_available'+'reason'")
        if has_slots and self.reason is not None:
            raise ValueError("'reason' is only valid alongside 'none_available'")
        if has_none_available and self.reason is None:
            raise ValueError("'reason' is required when 'none_available' is set")
        if has_slots and not self.slots:
            raise ValueError("'slots' must contain at least one slot when provided")
        return self


class CounterProposalV1(_StrictModel):
    """scheduling.availability / counter_proposal / v1.

    Same slots shape as ``AvailabilityResponseV1``'s slots branch — a
    fresh set of candidate slots offered in reply to a prior proposal.
    """

    type: Literal["counter_proposal"] = "counter_proposal"
    slots: list[Slot] = Field(min_length=1, max_length=10)


class ConfirmV1(_StrictModel):
    """scheduling.availability / confirm / v1.

    Confirms one slot (not a list); posting it transitions the
    conversation to 'completed'. Booking itself is EA-side.
    """

    type: Literal["confirm"] = "confirm"
    slot: TimeWindow


class DeclineV1(_StrictModel):
    """scheduling.availability / decline / v1.

    Sets the SENDER's participant status to 'declined'. If all non-owner
    participants have declined, the conversation state becomes 'canceled'
    (see ``state_machine.resulting_conversation_state``).
    """

    type: Literal["decline"] = "decline"
    reason: _DECLINE_REASONS


class NeedsClarificationV1(_StrictModel):
    """scheduling.availability / needs_clarification / v1.

    Points at a prior message by seq — no free-text questions in v1.
    ``about_seq`` must reference an existing message in the same
    conversation; that referential check lives in the service layer (it
    needs DB state), not here — this model only enforces ``>= 1``.
    """

    type: Literal["needs_clarification"] = "needs_clarification"
    about_seq: int = Field(ge=1)


_TASK_ACTIONS_REQUIRING_WINDOW_AND_DURATION = frozenset(
    {"gather_availability", "schedule_meeting", "reschedule_meeting"}
)


def _check_no_duplicates(values: Sequence[Any], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")


class TaskSpecV1(_StrictModel):
    """internal.coordination / task_spec / v1 — the ``tasks.payload`` shape (TECH-5094).

    Machine-actionable coordinates only, never prose: an ``action`` enum
    plus structured scheduling parameters (reusing ``TimeWindow`` and the
    same constraint/modality/priority enums as the scheduling.availability
    messages). Prose stays in the EA's own context (DESIGN.md §8 invariant
    3) — there is no free-text field here for it to travel in.

    ``gather_availability``/``schedule_meeting``/``reschedule_meeting``
    require ``window`` and ``duration_min``; ``confirm_slot`` requires
    ``window``. ``report_status`` is the report-back direction (EA ->
    Chief-of-Staff) and needs neither.
    """

    type: Literal["task_spec"] = "task_spec"
    action: Literal[
        "gather_availability",
        "schedule_meeting",
        "reschedule_meeting",
        "cancel_meeting",
        "confirm_slot",
        "report_status",
    ]
    counterparty_agent_ids: list[UUID] = Field(default_factory=list, max_length=10)
    related_conversation_id: UUID | None = None
    window: TimeWindow | None = None
    duration_min: int | None = Field(default=None, ge=5, le=480)
    modality: Literal["video", "phone", "in_person"] | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    due_at: AwareDatetime | None = None
    constraints: list[_CONSTRAINTS] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _no_duplicates(self) -> TaskSpecV1:
        _check_no_duplicates(self.constraints, "constraints")
        _check_no_duplicates(self.counterparty_agent_ids, "counterparty_agent_ids")
        return self

    @model_validator(mode="after")
    def _required_fields_for_action(self) -> TaskSpecV1:
        if self.action in _TASK_ACTIONS_REQUIRING_WINDOW_AND_DURATION:
            if self.window is None or self.duration_min is None:
                raise ValueError(f"action '{self.action}' requires 'window' and 'duration_min'")
        elif self.action == "confirm_slot" and self.window is None:
            raise ValueError("action 'confirm_slot' requires 'window'")
        return self


# Registry: (namespace, payload_type, schema_version) -> model class. Reused
# verbatim from the message registry's ``(conversation_type, message_type,
# schema_version)`` key shape (TECH-5094 §4) so ``get_schema``/``validate_payload``
# have exactly one lookup path for every typed payload in this service,
# messages and tasks alike. Every value is a concrete BaseModel subclass
# (never a Union/TypeAdapter), so callers can uniformly do
# ``get_schema(...).model_validate(payload)``.
MESSAGE_SCHEMAS: dict[tuple[str, str, int], type[BaseModel]] = {
    ("scheduling.availability", "availability_request", 1): AvailabilityRequestV1,
    ("scheduling.availability", "availability_response", 1): AvailabilityResponseV1,
    ("scheduling.availability", "counter_proposal", 1): CounterProposalV1,
    ("scheduling.availability", "confirm", 1): ConfirmV1,
    ("scheduling.availability", "decline", 1): DeclineV1,
    ("scheduling.availability", "needs_clarification", 1): NeedsClarificationV1,
    (TASK_NAMESPACE, "task_spec", 1): TaskSpecV1,
}


class PayloadValidationError(ValueError):
    """Raised when a payload fails schema lookup or validation.

    Carries a client-safe summary (schema errors are not secret — the
    caller is already an authorized member when payloads are validated).
    """


def get_schema(conversation_type: str, message_type: str, schema_version: int) -> type[BaseModel]:
    """Look up the Pydantic model registered for this message coordinate.

    This is the seam the (not-yet-built) service layer calls to resolve
    which schema governs an incoming payload, given the three values it
    already has on hand: the conversation's ``type`` column, the
    caller-supplied ``message_type``, and the caller-supplied
    ``schema_version`` (defaulting to 1 upstream). Always returns a
    concrete model class — never a ``Union`` or ``TypeAdapter`` — so the
    caller can do ``get_schema(...).model_validate(payload)`` uniformly.

    Raises ``PayloadValidationError`` (not ``KeyError``) for unknown
    combinations so callers can catch one exception type across lookup
    and validation failures.
    """
    schema_cls = MESSAGE_SCHEMAS.get((conversation_type, message_type, schema_version))
    if schema_cls is None:
        raise PayloadValidationError(
            f"unknown message schema: type '{message_type}' schema_version "
            f"{schema_version} is not registered for conversation type "
            f"'{conversation_type}'"
        )
    return schema_cls


def validate_payload(
    conversation_type: str,
    message_type: str,
    schema_version: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate ``payload`` against the registered schema and normalize it.

    Returns the JSON-mode dump of the validated model (datetimes as ISO
    strings) — this normalized form is what gets stored in
    ``messages.payload``.

    Enforces ``MAX_PAYLOAD_BYTES`` (DESIGN.md §8 "message size caps"
    security invariant) on both the raw input payload and the normalized
    dump — checked here, once, so every message type is covered uniformly
    without each schema class needing its own size validator.
    """
    _check_payload_size(payload)
    schema_cls = get_schema(conversation_type, message_type, schema_version)
    try:
        model = schema_cls.model_validate(payload)
    except ValidationError as exc:
        summary = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()[:5]
        )
        raise PayloadValidationError(f"payload failed schema validation: {summary}") from exc
    dumped: dict[str, Any] = model.model_dump(mode="json")
    _check_payload_size(dumped)
    return dumped


def _check_payload_size(payload: dict[str, Any]) -> None:
    """Raise ``PayloadValidationError`` if ``payload``'s JSON encoding exceeds
    ``MAX_PAYLOAD_BYTES``."""
    size = len(json.dumps(payload, default=str).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadValidationError(
            f"payload failed schema validation: payload is {size} bytes, "
            f"exceeding the {MAX_PAYLOAD_BYTES}-byte cap"
        )


__all__ = [
    "CONVERSATION_TYPES",
    "MESSAGE_SCHEMAS",
    "TASK_NAMESPACE",
    "AvailabilityRequestV1",
    "AvailabilityResponseV1",
    "ConfirmV1",
    "CounterProposalV1",
    "DeclineV1",
    "MessageType",
    "NeedsClarificationV1",
    "PayloadValidationError",
    "Slot",
    "TaskSpecV1",
    "TimeWindow",
    "get_schema",
    "validate_payload",
]
