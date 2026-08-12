"""EA provider -- the MCP tool surface over ``reclaw_ea.orchestrator.Negotiator``
(TECH-5065). Mounted under the ``ea`` namespace in main.py.

Tool surface (exactly the 5 named in TECH-5065, plus ``ea_whoami`` for fleet
parity with reclaw-comms-mcp): ``ea_negotiate``, ``ea_react_to_conversation``,
``ea_check_completion``, ``ea_request_booking``, ``ea_respond_to_approval``.
The LLM-driven caller (TECH-5084's agent run-loop host) never touches
``Negotiator`` internals directly -- only these tools.

Every tool follows the same shape:

1. Resolve ``owner_identity`` via ``identity.require_owner_identity`` --
   NEVER from a tool argument (TECH-5065's auth invariant: a bug here would
   let one owner's agent read/spend another owner's ledger or approval
   history). This is stricter than reclaw-comms-mcp's identity resolution,
   which is best-effort/observability-only -- see ``require_owner_identity``'s
   docstring for why.
2. Look up (or lazily create) that owner's ``Negotiator`` via
   ``_negotiator_for`` -- one multi-tenant process, one ``Negotiator`` per
   owner, matching TECH-5065's "one multi-tenant service, not one process
   per person."
3. Call exactly one ``Negotiator`` method against the shared board.
4. Return a plain dict -- no ``Negotiator`` objects, wire-schema objects, or
   scheduler_mcp dataclasses cross the tool boundary directly.

Known interim gaps, each already tracked rather than silently shipped:

* **Board**: ``_board`` is a single process-wide ``FakeBoard``, not a real
  ``reclaw-comms-mcp`` client. This works for a same-process internal pilot
  pair (every owner's ``Negotiator`` in this service shares the same board
  instance) but does not span processes/services. TECH-5055 (in progress)
  owns building the real client; when it lands, swap ``_board`` for it --
  no tool signature here needs to change, since ``BoardTransport`` is
  already the abstraction ``Negotiator`` depends on.
* **Persistence**: ``_negotiator_for`` builds each ``Negotiator`` with the
  default in-memory ``Ledger``/``ApprovalSurface``/``OutcomeStore`` --
  state does not survive a process restart. TECH-5083 tracks real
  Postgres-backed stores.
* **Rules**: no owner-authored rule UI exists (by design -- see
  ``scheduler_mcp.rules``'s own module docstring: rule-writing is
  exclusively an EA tool-call concern, never a form). ``_rules_for`` seeds
  each owner with the shipped defaults (``rules.apply_defaults``) on first
  use and never lets a caller edit them. A real rule-authoring path is
  unticketed as of this writing -- the EA's own judgment (TECH-5084's
  run-loop host) is expected to write rules via ``scheduler_mcp.rules``
  directly once that host exists, not through this provider.
* **Booking**: ``ea_request_booking``'s ``on_book`` callback does not write
  a real calendar event -- there is no calendar integration in this
  service. It records the deterministic booking discipline (ledger
  promotion, autonomy gate, approval hold) and returns the confirmed slot
  to the caller, which is expected to create the actual calendar invite
  itself (e.g. via ``rh-google-mcp``) and is responsible for retrying
  ``ea_request_booking`` if that invite creation fails -- ``_do_book``'s
  own docstring is explicit that ``on_book`` failing leaves nothing
  mutated, so a retry is always safe.

Registration reminder (fail-closed ``TOOL_SCOPES``, see scopes.py): every
tool added here MUST be enrolled in ``scopes.TOOL_SCOPES`` under its
mounted name (``ea_<tool>``) in the same change.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, ConfigDict, Field, model_validator
from reclaw_ea.fake_board import FakeBoard, NotAParticipantError, UnknownConversationError
from reclaw_ea.ledger import Ledger
from reclaw_ea.orchestrator import Negotiator
from reclaw_ea.scorer import Incumbent, SlotContext
from reclaw_ea.tiers import Tier
from reclaw_ea.wire import AvailabilityRequest, Modality
from scheduler_mcp.negotiation.schema import CandidateSlot
from scheduler_mcp.rules import InMemoryRuleStore, Rule, Situation, apply_defaults

from identity import require_owner_identity, try_resolve_email
from observability import log_security_event
from scopes import is_interactive_token, scopes_for_token

ea_server: FastMCP[Any] = FastMCP("ea")

# Bounds conversation_id/to_agent_identity string inputs: both are stored as
# dict keys in process-lifetime state (Argus round 1 finding), so an
# unbounded string could bloat in-memory structures.
ConversationId = Annotated[str, Field(min_length=1, max_length=256)]

_CONVERSATION_NOT_FOUND = "conversation not found or not accessible"

# Exceptions that reveal, by their distinct shape, whether a conversation
# exists at all vs. exists-but-not-mine (Argus round 1 finding: without
# normalizing these, a caller could distinguish "unknown conversation_id"
# from "exists, but I'm not a participant" by exception type alone -- an
# oracle DESIGN.md §4 requires closed off, the same anti-enumeration
# posture reclaw-comms-mcp's own uniform denials already take).
#
# Deliberately NOT `ValueError` (Argus round 2 finding, correctness-
# critical: a prior version of this tuple included it). `reclaw_ea` raises
# bare `ValueError` for legitimate domain-state conditions unrelated to
# conversation existence/membership -- `SlotKey.__init__` for a
# timezone-naive datetime (ledger.py:60, a real programming bug, not a
# caller-facing denial) and `respond_to_booking_approval`'s "no pending
# approval" / "completion lapsed" cases (orchestrator.py:827,848, which
# `respond_to_approval` below handles with its OWN distinct, still-safe
# message -- see that tool). Catching `ValueError` here would silently
# report both as "conversation not found," masking real bugs and breaking
# `respond_to_approval`'s documented ability to distinguish its two cases.
_CONVERSATION_ERRORS: tuple[type[Exception], ...] = (
    UnknownConversationError,
    NotAParticipantError,
)


def _tool_error(operation: str, exc: Exception) -> ToolError:
    """Collapse a conversation-state exception into a uniform, detail-free
    `ToolError` -- the specific exception TYPE is logged server-side only
    (never in the client-facing message) so a caller cannot distinguish
    "conversation doesn't exist" from "exists but you're not in it" by
    probing error text. Uses `log_security_event` (Argus round 2 finding:
    a prior version used a bare stdlib `logger.warning`, invisible to the
    JSON/CloudWatch pipeline just like the auth.py/main.py paths this same
    round of fixes moved off stdlib logging -- and, as a stdlib `%s`-style
    call, vulnerable to log-line injection via a crafted `conversation_id`
    containing CRLF; `log_security_event` routes through structlog's
    `JSONRenderer`, which escapes the value).

    Argus round 3 finding, corrected: this docstring previously claimed
    the exception's MESSAGE is also logged server-side -- it isn't, only
    `type(exc).__name__` (`error_type` below). `UnknownConversationError`/
    `NotAParticipantError`'s messages both embed the raw `conversation_id`
    (fake_board.py), and logging that would be no worse for anti-
    enumeration than `operation` already is (both are caller-controlled,
    already-known-to-the-caller inputs, not secrets), but it isn't
    currently done -- if a future incident needs the specific
    conversation_id, add it explicitly rather than assuming it's already
    captured here."""
    log_security_event(
        "conversation_access_rejected",
        operation=operation,
        error_type=type(exc).__name__,
    )
    return ToolError(_CONVERSATION_NOT_FOUND)


# --- Per-owner state (see module docstring's "Known interim gaps") --------

# One process-wide board: EVERY owner's `Negotiator` in this service shares
# it (matches TECH-5065's "one multi-tenant service"). Two owners
# negotiating with each other therefore both resolve to conversation state
# on this SAME instance -- this is what makes a same-process pilot pair
# actually work end-to-end today, ahead of TECH-5055's real board client.
_board = FakeBoard()

# Argus round 1 finding: these three dicts/sets grow one entry per unique
# owner_identity with no eviction, TTL, or size cap. Since an rh-auth Bearer
# token can be minted with any `--sub` value by anyone holding
# RH_AUTH_SECRET, a compromised or misconfigured caller could cause
# unbounded heap growth by cycling through distinct identities. Acceptable
# for the same reason the rest of this module's persistence is (TECH-5083
# replaces all of this with real, presumably-bounded storage) but the
# growth dimension specifically is not yet mitigated -- no cap is applied
# here today.
_negotiators: dict[str, Negotiator] = {}
_rule_store = InMemoryRuleStore()
_rules_seeded: set[str] = set()


def _negotiator_for(owner_identity: str) -> Negotiator:
    negotiator = _negotiators.get(owner_identity)
    if negotiator is None:
        negotiator = Negotiator(identity=owner_identity, ledger=Ledger())
        _negotiators[owner_identity] = negotiator
    return negotiator


def _rules_for(owner_identity: str) -> list[Rule]:
    """Return `owner_identity`'s rules, seeding the shipped defaults on
    first use. See module docstring's "Rules" gap -- there is no edit path
    here by design."""
    if owner_identity not in _rules_seeded:
        apply_defaults(_rule_store, owner_identity)
        _rules_seeded.add(owner_identity)
    rules: list[Rule] = _rule_store.get_rules_for_owner(owner_identity)
    return rules


def _require_identity() -> str:
    token = get_access_token()
    if token is None:
        raise ToolError("no access token provided")
    return require_owner_identity(token)


# --- Pydantic mirrors for scorer.SlotContext / scorer.Incumbent -----------
#
# CandidateSlot, Situation, and Rule are already Pydantic models
# (scheduler_mcp) and cross the tool boundary directly. SlotContext and
# Incumbent are plain dataclasses (scorer.py) with no JSON-schema of their
# own, so this provider defines thin Pydantic mirrors and converts to the
# dataclasses internally -- the dataclasses themselves are not part of the
# wire contract and can keep evolving independently.


class IncumbentIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    exists: bool = False
    organizer_is_owner: bool | None = None
    attendee_count: int | None = None

    def to_incumbent(self) -> Incumbent:
        return Incumbent(
            exists=self.exists,
            organizer_is_owner=self.organizer_is_owner,
            attendee_count=self.attendee_count,
        )


class SlotContextIn(BaseModel):
    """Mirrors `reclaw_ea.scorer.SlotContext` -- see that class's docstring
    for what each field means and how it's scored."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    situation: Situation
    incumbent: IncumbentIn = Field(default_factory=IncumbentIn)
    adjacent_free_minutes: int | None = None
    energy_peak_start: time | None = None
    energy_peak_end: time | None = None
    buffer_before_minutes: int | None = None
    buffer_after_minutes: int | None = None
    within_counterparty_window: bool | None = None
    tier: Tier = Tier.TIER_3

    @model_validator(mode="after")
    def _energy_peak_both_or_neither(self) -> SlotContextIn:
        # Argus round 1 finding: silently dropping a partial energy_peak
        # specification (one bound set, the other None) previously
        # discarded both with no error -- a caller-supplied preference
        # signal vanishing without feedback.
        if (self.energy_peak_start is None) != (self.energy_peak_end is None):
            raise ValueError(
                "energy_peak_start and energy_peak_end must both be set or both omitted"
            )
        return self

    def to_slot_context(self) -> SlotContext:
        energy_peak = (
            (self.energy_peak_start, self.energy_peak_end)
            if self.energy_peak_start is not None and self.energy_peak_end is not None
            else None
        )
        return SlotContext(
            start=self.start,
            end=self.end,
            situation=self.situation,
            incumbent=self.incumbent.to_incumbent(),
            adjacent_free_minutes=self.adjacent_free_minutes,
            energy_peak=energy_peak,
            buffer_before_minutes=self.buffer_before_minutes,
            buffer_after_minutes=self.buffer_after_minutes,
            within_counterparty_window=self.within_counterparty_window,
            tier=self.tier,
        )


def _slot_to_dict(slot: CandidateSlot) -> dict[str, str]:
    return {"start": slot.start.isoformat(), "end": slot.end.isoformat()}


# --- Tools -----------------------------------------------------------------


@ea_server.tool
async def whoami() -> dict[str, Any]:
    """Return the authenticated caller's identity, issuer, caller type, and
    scopes -- diagnostic tool for verifying auth/scope wiring, matching
    reclaw-comms-mcp's `comms_whoami`. Never raises: a diagnostic tool that
    fails closed on the exact failure it exists to diagnose would be
    useless for debugging that failure.

    `owner_identity` is exactly the value every other tool attributes this
    caller's ledger, negotiator, and approval-hold state to -- `null` means
    `require_owner_identity` would reject this token (e.g. an interactive
    caller whose claims resolve to no usable identity), in which case
    every OTHER `ea_*` tool will also raise for this caller. `identity` is
    the older, best-effort field (Argus round 1 finding: it used to be the
    only one, and returned `None` for interactive callers even when their
    state IS keyed identically to service callers' -- kept for backwards
    compatibility, `owner_identity` is the one to trust)."""
    token = get_access_token()
    if token is None:
        raise ToolError("no access token provided")
    interactive = is_interactive_token(token)
    try:
        owner_identity: str | None = require_owner_identity(token)
    except ToolError:
        owner_identity = None
    return {
        "identity": try_resolve_email(token),
        "owner_identity": owner_identity,
        "issuer": token.claims.get("iss"),
        "caller_type": "interactive" if interactive else "service",
        "scopes": scopes_for_token(token),
    }


@ea_server.tool
async def negotiate(
    to_agent_identity: ConversationId,
    window: CandidateSlot,
    duration_minutes: Annotated[int, Field(gt=0, le=24 * 60)],
    modality: Modality,
    priority: Annotated[int, Field(ge=1, le=4)] = 3,
) -> dict[str, Any]:
    """Open a new negotiation with `to_agent_identity`. `to_agent_identity`
    must be the counterparty's own `owner_identity` as this service would
    resolve it (a service-token `sub`, or an interactive caller's email) --
    there is no separate directory/lookup, so it must be known out of band.
    Returns the `conversation_id` both sides use for every subsequent
    `ea_react_to_conversation`/`ea_check_completion` call.

    `priority` is a hint, never an instruction (docs/DESIGN.md §2.2 point
    6) -- the receiving EA computes its own tier from its own rules."""
    owner_identity = _require_identity()
    negotiator = _negotiator_for(owner_identity)
    request = AvailabilityRequest(
        window=window, duration_minutes=duration_minutes, modality=modality, priority=priority
    )
    conversation_id = negotiator.open_negotiation(
        _board, to_agent_identity=to_agent_identity, request=request
    )
    return {"conversation_id": conversation_id}


@ea_server.tool
async def react_to_conversation(
    conversation_id: ConversationId, my_candidates: list[SlotContextIn]
) -> dict[str, Any]:
    """Process the counterparty's latest message on `conversation_id` and
    take exactly one action (propose, confirm, or decline) -- a no-op if
    it isn't this owner's turn or the negotiation is already terminal.
    `my_candidates` are this owner's own scored candidate slots for this
    meeting (sourced from real calendar + judgment upstream of this
    service -- see module docstring's "Rules" and TECH-5084); rules are
    resolved internally, never accepted as input.

    Raises if `conversation_id` doesn't exist, or exists but this caller
    isn't a participant -- the two cases are deliberately indistinguishable
    to the caller, to prevent enumeration of which conversation IDs are
    real vs. which ones this caller merely isn't part of."""
    owner_identity = _require_identity()
    negotiator = _negotiator_for(owner_identity)
    contexts = [c.to_slot_context() for c in my_candidates]
    try:
        negotiator.react(
            _board,
            conversation_id,
            my_candidates=contexts,
            rules=_rules_for(owner_identity),
        )
    except _CONVERSATION_ERRORS as exc:
        raise _tool_error("ea_react_to_conversation", exc) from exc
    state = negotiator.state_for(conversation_id)
    return {
        "conversation_id": conversation_id,
        "round": state.round if state is not None else None,
        "is_terminal": state.is_terminal if state is not None else None,
        "outcome": state.outcome.value if state is not None else None,
    }


@ea_server.tool
async def check_completion(conversation_id: ConversationId) -> dict[str, Any]:
    """Return the agreed slot for `conversation_id` if every active
    participant's latest message is a matching `Confirm`, else `None`.
    Raises if `conversation_id` doesn't exist, or exists but this caller
    isn't a participant -- indistinguishably, to prevent enumeration."""
    owner_identity = _require_identity()
    negotiator = _negotiator_for(owner_identity)
    try:
        slot = negotiator.check_completion(_board, conversation_id)
    except _CONVERSATION_ERRORS as exc:
        raise _tool_error("ea_check_completion", exc) from exc
    return {"conversation_id": conversation_id, "slot": _slot_to_dict(slot) if slot else None}


@ea_server.tool
async def request_booking(conversation_id: ConversationId) -> dict[str, Any]:
    """Request booking for a completed negotiation, through the autonomy
    gate (docs/DESIGN.md §2.2 point 3). Completion alone never books
    directly -- an internal counterparty earns autonomous booking from a
    real approval track record. **Every counterparty is currently
    classified as internal** (`default_is_external`, TECH-5069 -- real
    domain/org-membership resolution is not wired into this service yet),
    so the "external counterparty is always ask_first" invariant this gate
    is designed to enforce is NOT YET ENFORCED for any actual external
    party. Do not rely on this tool to gate external-invite-adjacent
    autonomy until TECH-5069 lands.

    Returns `booked=True` with the confirmed slot only if booking happened
    immediately in this call (the caller is responsible for actually
    creating the calendar invite -- see module docstring's "Booking" gap).
    `booked=False, pending_approval=True` means a hold was opened;
    `ea_respond_to_approval` resolves it later. `booked=False,
    pending_approval=False` means the negotiation isn't complete yet, or
    is already booked/pending, OR this caller is a legitimate non-owner
    participant (only the conversation owner can book -- see
    `Negotiator.maybe_finalize`'s own docstring). Raises if
    `conversation_id` doesn't exist, or exists but this caller isn't a
    participant at all -- indistinguishably, to prevent enumeration."""
    owner_identity = _require_identity()
    negotiator = _negotiator_for(owner_identity)
    booked_slot: CandidateSlot | None = None

    def on_book(_conversation_id: str, slot: CandidateSlot) -> None:
        nonlocal booked_slot
        booked_slot = slot

    try:
        # Argus round 2 finding, correctness-critical: `maybe_finalize`
        # itself never raises for a caller who isn't a participant at all
        # -- it only ever checks `board.owner_of(...) != self.identity`
        # and returns `False`, the same as a legitimate non-owner
        # participant's normal no-op. That made this tool a conversation-
        # existence oracle: an unknown conversation_id raised (via
        # `board.owner_of`'s own lookup), but a REAL conversation_id this
        # caller merely isn't part of returned a plain `booked=False` dict
        # -- an enumerable difference none of the other tools have. This
        # explicit participants_of() check restores the same "unknown vs.
        # not-mine are indistinguishable" property the other three tools
        # already have, while still letting a genuine non-owner
        # participant reach `maybe_finalize`'s intentional no-op below.
        if owner_identity not in _board.participants_of(conversation_id):
            raise NotAParticipantError(
                f"{owner_identity!r} is not a participant in {conversation_id!r}"
            )
        booked = negotiator.maybe_finalize(_board, conversation_id, on_book=on_book)
    except _CONVERSATION_ERRORS as exc:
        raise _tool_error("ea_request_booking", exc) from exc
    return {
        "conversation_id": conversation_id,
        "booked": booked,
        "slot": _slot_to_dict(booked_slot) if booked_slot else None,
        "pending_approval": negotiator.has_pending_booking_approval(conversation_id),
    }


@ea_server.tool
async def respond_to_approval(conversation_id: ConversationId, approved: bool) -> dict[str, Any]:
    """Resolve a pending booking approval hold opened by `ea_request_booking`
    -- the human-in-the-loop response to an `ask_first` gate decision. This
    tool only records the booking decision and runs the deterministic
    ledger/autonomy-gate discipline -- it does NOT create a real calendar
    event either way (see module docstring's "Booking" gap); the caller is
    responsible for creating the invite itself when `booked=True` comes
    back. `approved=False` releases the ledger hold and records the
    rejection for the confidence system.

    Raises `ToolError("no pending booking approval for this conversation")`
    if no pending approval exists for this conversation -- this covers
    `conversation_id` being unknown, belonging to a different owner,
    already resolved, never requested, or swept as expired (TECH-5076),
    AND the rarer case where completion itself has lapsed (the negotiation
    is no longer fully confirmed) between this call starting and
    `respond_to_booking_approval` actually running. All of these reduce to
    the same message because all of them are keyed off THIS caller's own
    local state (`has_pending_booking_approval`, or a race against it),
    never board state -- none can be used to probe whether some other
    conversation_id exists."""
    owner_identity = _require_identity()
    negotiator = _negotiator_for(owner_identity)
    booked_slot: CandidateSlot | None = None

    def on_book(_conversation_id: str, slot: CandidateSlot) -> None:
        nonlocal booked_slot
        booked_slot = slot

    # Argus round 2 finding, corrected in round 3, corrected again in round
    # 4: a bare `except ValueError` here originally conflated
    # `respond_to_booking_approval`'s two distinct raise sites
    # (no-pending-approval vs. completion-lapsed, orchestrator.py:827,848)
    # under the wrong message. Round 3's fix replaced it with this
    # `has_pending_booking_approval` pre-check -- correct for the common
    # case, but it left a real race uncaught: `sweep_expired_booking_
    # approvals` (called at the top of every `Negotiator.react()`) can run
    # between this pre-check and the call below, on ANOTHER thread/request
    # sharing this same in-process `Negotiator`, flipping a hold from
    # pending to expired in between -- at which point
    # `respond_to_booking_approval` raises its OWN "no pending approval"
    # ValueError (or, less likely, "completion lapsed" if the negotiation
    # itself changed underneath the approval), and round 3's version let
    # that propagate uncaught: raw library internals reaching an MCP
    # client, the only tool in this provider that did so. Both of
    # `respond_to_booking_approval`'s ValueError messages are locally-safe
    # (see this function's docstring), so catching `ValueError` here
    # specifically -- narrower than `_CONVERSATION_ERRORS`, and only after
    # the pre-check has already ruled out the common no-pending-approval
    # case -- is safe: anything reaching this except clause is one of
    # those two known, non-leaking conditions from THIS exact call, not an
    # unrelated bug several layers down being silently reclassified.
    if not negotiator.has_pending_booking_approval(conversation_id):
        log_security_event(
            "booking_approval_rejected", operation="ea_respond_to_approval", reason="no_pending"
        )
        raise ToolError("no pending booking approval for this conversation")

    try:
        negotiator.respond_to_booking_approval(
            _board, conversation_id, approved=approved, on_book=on_book
        )
    except _CONVERSATION_ERRORS as exc:
        raise _tool_error("ea_respond_to_approval", exc) from exc
    except ValueError:
        log_security_event(
            "booking_approval_rejected",
            operation="ea_respond_to_approval",
            reason="lapsed_between_precheck_and_call",
        )
        raise ToolError("no pending booking approval for this conversation") from None
    return {
        "conversation_id": conversation_id,
        "booked": booked_slot is not None,
        "slot": _slot_to_dict(booked_slot) if booked_slot else None,
    }
