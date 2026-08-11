"""The `scheduling.availability` v1 wire schema.

Design: `reclaw-comms-mcp/docs/DESIGN.md` §6, and `docs/DESIGN.md` §2.2's
EA-side protocol semantics agreed on top of it. This module is a
**client-side mirror** of the board's contract, not the board itself — the
board (`reclaw-comms-mcp`) owns the actual Postgres-backed service,
membership enforcement, and the append-only message log; this repo only
needs to speak the same schema so the orchestrator (`orchestrator.py`) can
be built and golden-tested today against `fake_board.py`, and pointed at
the real board later with no change to this module (`docs/DESIGN.md`
TECH-5055's "start-now note").

Deliberately reuses `scheduler_mcp.negotiation.schema.CandidateSlot` for
the `{start, end}` shape rather than redefining it — no reason for two
"self-contained time range" types in this dependency graph. Everything
else here is new: the board's message types are its own schema, not
`scheduler_mcp.negotiation.schema`'s older `Propose`/`Respond` handshake,
which predates the comms-board pivot and is superseded by this module for
the wire format (see `docs/DESIGN.md` §1: "negotiation runs EA-to-EA over
the board").
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from scheduler_mcp.negotiation.schema import CandidateSlot

MAX_SLOTS_PER_MESSAGE = 10  # reclaw-comms-mcp/docs/DESIGN.md §6: "max 10"


class Modality(StrEnum):
    VIDEO = "video"
    PHONE = "phone"
    IN_PERSON = "in_person"


class DeclineReason(StrEnum):
    """Closed vocabulary, no free text — `docs/DESIGN.md` companion §6:
    a finer vocabulary risks the categories themselves becoming a
    disclosure side-channel."""

    NO_AVAILABILITY_WITHIN_CONSTRAINTS = "no_availability_within_constraints"
    AUTONOMY_NOT_GRANTED = "autonomy_not_granted"
    OWNER_DECLINED = "owner_declined"
    OTHER = "other"


class ScoredSlot(BaseModel):
    """`docs/DESIGN.md` companion §6: "`preference` is the product":
    judgment crosses the boundary, never raw calendar data — there is no
    field here for a raw busy/free interval to travel in, only a slot and
    the offering EA's own preference for it (`scorer.SlotScore.preference`,
    unwrapped to a bare float for the wire)."""

    model_config = ConfigDict(frozen=True)

    slot: CandidateSlot
    preference: float = Field(ge=0.0, le=1.0)


class AvailabilityRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_type: Literal["availability_request"] = "availability_request"
    window: CandidateSlot
    duration_minutes: int = Field(gt=0, le=24 * 60)
    modality: Modality
    priority: int = Field(
        ge=1,
        le=4,
        description=(
            "docs/DESIGN.md §2.2 point 6: a hint, never an instruction. "
            "The receiving EA computes its own tier (tiers.Tier) from its "
            "own rules and counterparty identity; this field cannot buy "
            "autonomy or a better slot by itself."
        ),
    )
    constraints: tuple[str, ...] = Field(default=())


def _require_slots_or_none_available(slots: tuple, none_available: bool) -> None:
    """Shared by `AvailabilityResponse` and `CounterProposal` (Argus round
    1 finding): an empty `slots` tuple with `none_available=False` was
    previously valid Pydantic but semantically ambiguous -- "no opinion
    yet" vs. "genuinely nothing available" are different signals, and the
    orchestrator was silently treating the ambiguous case as a failed
    counter, consuming a round-budget entry for a message that never
    should have been sendable in the first place."""

    if not slots and not none_available:
        raise ValueError("slots must be non-empty, or none_available must be True")


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_type: Literal["availability_response"] = "availability_response"
    slots: tuple[ScoredSlot, ...] = Field(default=(), max_length=MAX_SLOTS_PER_MESSAGE)
    none_available: bool = False
    reason: DeclineReason | None = None

    @model_validator(mode="after")
    def _slots_or_none_available(self) -> AvailabilityResponse:
        _require_slots_or_none_available(self.slots, self.none_available)
        return self


class CounterProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_type: Literal["counter_proposal"] = "counter_proposal"
    slots: tuple[ScoredSlot, ...] = Field(default=(), max_length=MAX_SLOTS_PER_MESSAGE)
    none_available: bool = False
    reason: DeclineReason | None = None

    @model_validator(mode="after")
    def _slots_or_none_available(self) -> CounterProposal:
        _require_slots_or_none_available(self.slots, self.none_available)
        return self


class Confirm(BaseModel):
    """`docs/DESIGN.md` §2.2 point 1: a per-participant commitment, not a
    completion trigger. The conversation completes only when every active
    participant's *latest* substantive message is a `Confirm` naming the
    identical slot (`orchestrator.py`'s completion check) — this message
    type carries no completion semantics of its own."""

    model_config = ConfigDict(frozen=True)

    message_type: Literal["confirm"] = "confirm"
    slot: CandidateSlot


class Decline(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_type: Literal["decline"] = "decline"
    reason: DeclineReason


class NeedsClarification(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_type: Literal["needs_clarification"] = "needs_clarification"
    about_seq: int = Field(ge=1)


BoardPayload = Annotated[
    AvailabilityRequest
    | AvailabilityResponse
    | CounterProposal
    | Confirm
    | Decline
    | NeedsClarification,
    Field(discriminator="message_type"),
]
