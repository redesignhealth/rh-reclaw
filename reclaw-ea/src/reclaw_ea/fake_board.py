"""An in-memory stand-in for `reclaw-comms-mcp`, conforming to the same
`scheduling.availability` v1 contract (`wire.py`).

Design: `docs/DESIGN.md` §9/§12 delivery plan step 1-2, and TECH-5055's
"start-now note": the orchestrator, state machine wiring, and confirm
discipline can all be built and golden-tested against this fake transport
while the real board is in flight. Swapping this for an MCP client hitting
the deployed board is meant to be the *only* change needed later — this
class exposes exactly the four operations `Negotiator` (`orchestrator.py`)
needs (`open`, `post`, `read_since`, `participants`), matching the shape of
`reclaw-comms-mcp`'s own tool surface (`comms_start_conversation`,
`comms_post_message`, `comms_get_conversation`), not a richer interface
this fake happens to make easy.

What this fake deliberately does NOT reimplement: membership enforcement,
scope checks, rate limits, persistence, or `last_read_seq` (Argus round 1
finding: an earlier version of this docstring claimed `last_read_seq` was
modelled -- it never was; `_Conversation` has no such field, and every
`Negotiator` call to `read_since` uses `since_seq=0`, always reading full
history). Those are the board's own job (`reclaw-comms-mcp/docs/DESIGN.md`
§4, §8) and out of scope for a same-process test double. What it DOES
reproduce: server-assigned `seq`, monotonic per conversation, since
`Negotiator`'s confirm-completion check (`docs/DESIGN.md` §2.2 point 1:
"every active participant's *latest* substantive message") depends on
message ordering being authoritative, not client-side. That ordering is
only single-thread-safe here (`seq = len(convo.messages) + 1` is not an
atomic operation) -- adequate for this fake's synchronous, single-process
use, but not a claim about the real board's own concurrency guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .wire import BoardPayload


class BoardTransport(Protocol):
    """The exact surface `Negotiator` needs from a board transport --
    `FakeBoard` below is one implementation; an MCP client hitting the
    deployed `reclaw-comms-mcp` is meant to be a drop-in second one (Argus
    round 1 finding: `Negotiator`'s methods previously annotated `board:
    FakeBoard` directly, so a caller supplying any other conforming
    implementation would fail type-checking without an explicit cast, even
    though this module's own docstring claims swapping transports is "the
    only change needed"). `owner_of` and `participants_of` are included
    here because `Negotiator` calls both, even though the module docstring
    above historically only advertised four operations."""

    def open(self, *, owner: str, participants: list[str]) -> str: ...

    def post(
        self, *, conversation_id: str, sender_id: str, payload: BoardPayload
    ) -> BoardMessage: ...

    def read_since(
        self, *, conversation_id: str, agent_id: str, since_seq: int = 0
    ) -> list[BoardMessage]: ...

    def owner_of(self, conversation_id: str) -> str: ...

    def participants_of(self, conversation_id: str) -> list[str]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BoardMessage:
    conversation_id: str
    seq: int
    sender_id: str
    payload: BoardPayload
    created_at: datetime


@dataclass
class _Conversation:
    conversation_id: str
    participants: list[str]
    owner: str
    messages: list[BoardMessage] = field(default_factory=list)


class UnknownConversationError(Exception):
    pass


class NotAParticipantError(Exception):
    """Uniform-denial spirit of the real board (`reclaw-comms-mcp/docs/
    DESIGN.md` §4: "identical whether the conversation exists or not") is
    the board's own job to enforce over the wire; this fake still raises a
    distinct, non-uniform exception, since a same-process test double has
    no enumeration surface to protect and a specific error is more useful
    for catching orchestrator bugs during development."""


class FakeBoard:
    def __init__(self, clock=_utcnow) -> None:
        self._conversations: dict[str, _Conversation] = {}
        self._clock = clock
        self._next_id = 1

    def open(self, *, owner: str, participants: list[str]) -> str:
        """`docs/DESIGN.md` companion §4: "the creator gets role=owner." All
        named participants become members immediately (no invite handshake
        in v1 — internal trust domain)."""

        conversation_id = f"conv-{self._next_id}"
        self._next_id += 1
        all_participants = list(dict.fromkeys([owner, *participants]))
        self._conversations[conversation_id] = _Conversation(
            conversation_id=conversation_id, participants=all_participants, owner=owner
        )
        return conversation_id

    def post(
        self, *, conversation_id: str, sender_id: str, payload: BoardPayload
    ) -> BoardMessage:
        convo = self._require_participant(conversation_id, sender_id)
        seq = len(convo.messages) + 1  # server-assigned, monotonic per conversation
        message = BoardMessage(
            conversation_id=conversation_id,
            seq=seq,
            sender_id=sender_id,
            payload=payload,
            created_at=self._clock(),
        )
        convo.messages.append(message)
        return message

    def read_since(
        self, *, conversation_id: str, agent_id: str, since_seq: int = 0
    ) -> list[BoardMessage]:
        convo = self._require_participant(conversation_id, agent_id)
        return [m for m in convo.messages if m.seq > since_seq]

    def owner_of(self, conversation_id: str) -> str:
        return self._require(conversation_id).owner

    def participants_of(self, conversation_id: str) -> list[str]:
        return list(self._require(conversation_id).participants)

    def _require(self, conversation_id: str) -> _Conversation:
        convo = self._conversations.get(conversation_id)
        if convo is None:
            raise UnknownConversationError(conversation_id)
        return convo

    def _require_participant(
        self, conversation_id: str, agent_id: str
    ) -> _Conversation:
        convo = self._require(conversation_id)
        if agent_id not in convo.participants:
            raise NotAParticipantError(
                f"{agent_id!r} is not a participant in {conversation_id!r}"
            )
        return convo
