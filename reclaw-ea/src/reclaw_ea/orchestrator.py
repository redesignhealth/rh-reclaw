"""The negotiation orchestration loop: one `Negotiator` per EA identity,
speaking `scheduling.availability` v1 (`wire.py`) over a board transport
(`fake_board.FakeBoard` today; `reclaw-comms-mcp` later, same interface).

Design: `docs/DESIGN.md` §2. Wires the built-but-unwired `scheduler_mcp.
negotiation` modules (`rounds`, `circuit_breaker`) into a single-owner,
event-driven loop — the LLM's job (not implemented here; see module-level
note below) is upstream classification and candidate-slot generation, this
module's job is the deterministic protocol discipline around it:

  - `negotiation_id` = the board's `conversation_id`, stamped once at
    `open_negotiation` and never recomputed (maiea #298).
  - **Confirm discipline** (`docs/DESIGN.md` §2.2): before posting
    `Confirm`, claim (or re-claim) the slot in this identity's own
    `Ledger` and promote it to `booked` — a failed claim means this EA
    cannot honestly confirm, so it does not. Completion is detected by
    walking message history: every active participant's *latest*
    substantive message must be `Confirm` naming the identical slot.
    Only the conversation owner books the actual calendar invite;
    everyone else has already promoted their own ledger hold at confirm
    time.
  - **Circuit breaker**, identity-wide: every outbound post goes through
    `_post`, which checks `circuit_breaker.evaluate_send` before sending.
    The first call that trips it stands down every in-flight negotiation
    for this identity (`circuit_breaker.stand_down_all`) and alerts once;
    every call after that is silently a no-op post, matching maiea's
    "pause the entire EA identity" behavior.
  - **Round budget**: `advance_round` on an unresolved counter, moving to
    `NO_AGREEMENT_POSSIBLE` (with an assembled `EscalationPayload`) once
    the budget is exhausted rather than continuing indefinitely.

Scope note, deliberate: this module contains NO LLM calls and no slot
generation of its own. Callers supply already-computed `SlotContext`s
(from `scorer.py`, backed by whatever calendar/classification pipeline
exists upstream) and an owner `rules` list; `Negotiator` only decides
*when* to propose, accept, counter, or confirm those already-scored
candidates — matching `docs/DESIGN.md` §2's "the LLM never calls the
calendar directly; it requests a booking through the choke point," here
generalized to "the LLM never posts to the board directly either."
Real VIP/counterparty-identity-based tier resolution (`tiers.py`) is also
out of scope for this ticket — `default_tier_resolver` below is an
explicit placeholder, not a finished implementation, so that the
"priority is a hint, never an instruction" invariant (`docs/DESIGN.md`
§2.2 point 6) is trivially true in code today rather than silently
violated by a shortcut.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from datetime import UTC, datetime

from scheduler_mcp.negotiation.circuit_breaker import (
    DEFAULT_CIRCUIT_BREAKER_WINDOW,
    CircuitBreakerState,
    evaluate_send,
    stand_down_all,
    start_circuit_breaker,
)
from scheduler_mcp.negotiation.rounds import (
    NegotiationOutcome,
    NegotiationRoundState,
    advance_round,
    book,
    exhaust_round_budget,
    start_negotiation,
)
from scheduler_mcp.negotiation.schema import CandidateSlot
from scheduler_mcp.rules import Rule

from .approvals import DEFAULT_APPROVAL_TTL, ApprovalHold, ApprovalSurface
from .autonomy import ConfidenceInputs, Decision
from .booking_gate import evaluate_booking_gate
from .fake_board import BoardMessage, BoardTransport
from .ledger import ClaimVerdict, Ledger
from .outcomes import InMemoryOutcomeStore, OutcomeStore, respond_and_record
from .scorer import SlotContext, rank_slots
from .tiers import Tier, hold_ttl_for
from .wire import (
    MAX_SLOTS_PER_MESSAGE,
    AvailabilityRequest,
    AvailabilityResponse,
    Confirm,
    CounterProposal,
    Decline,
    DeclineReason,
    NeedsClarification,
    ScoredSlot,
)

logger = logging.getLogger(__name__)

BOOKING_ACTION_KEY = "book_negotiated_meeting"  # see booking_gate.py module docstring

ACCEPTANCE_THRESHOLD = 0.5  # a counterparty-offered slot scoring below this
# against my own candidates is treated as "not good enough to confirm,"
# not "infeasible" -- the scorer already filtered infeasible slots out
# upstream; this is purely a preference bar.


def _utcnow() -> datetime:
    return datetime.now(UTC)


def default_tier_resolver(request: AvailabilityRequest) -> Tier:
    """Placeholder tier resolution — see module docstring. Deliberately
    ignores `request.priority` (the requester's own hint) entirely, so
    a requester cannot buy a better tier just by asking for one; a real
    resolver reads the recipient's own VIP store and rules instead.
    Tracked as TECH-5069."""

    warnings.warn(
        "default_tier_resolver is a placeholder that always returns Tier.TIER_3 -- "
        "replace with a real VIP/counterparty-identity-based resolver before production "
        "(docs/DESIGN.md open question 2, TECH-5069)",
        stacklevel=2,
    )
    return Tier.TIER_3


def default_is_external(counterpart: str) -> bool:
    """Placeholder externality resolution — same spirit as
    `default_tier_resolver` above (Argus round 1 finding: both placeholder
    defaults were silent; if shipped unreplaced, every counterparty is
    treated as internal and `send_external_invite`'s permanent gate is
    never triggered for anyone). Real domain/org-membership resolution is
    tracked as TECH-5069, alongside `default_tier_resolver`."""

    warnings.warn(
        "default_is_external is a placeholder that always returns False (treats every "
        "counterparty as internal) -- replace with real domain/org-membership resolution "
        "before production (TECH-5069)",
        stacklevel=2,
    )
    return False


def _same_slot(a: CandidateSlot, b: CandidateSlot) -> bool:
    return a.start == b.start and a.end == b.end


class Negotiator:
    """One EA identity's negotiation state and board client."""

    def __init__(
        self,
        identity: str,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime] = _utcnow,
        round_budget: int | None = None,
        approval_surface: ApprovalSurface | None = None,
        outcome_store: OutcomeStore | None = None,
        is_external: Callable[[str], bool] = default_is_external,
    ) -> None:
        self.identity = identity
        self.ledger = ledger
        self._clock = clock
        self._round_budget_override = round_budget
        self._negotiations: dict[str, NegotiationRoundState] = {}
        self._send_log: list[datetime] = []
        self._breaker: CircuitBreakerState = start_circuit_breaker(
            owner_identity=identity
        )
        self._booked: set[str] = (
            set()
        )  # conversation_ids this identity has already booked
        self._pending_booking_approvals: dict[
            str, str
        ] = {}  # conversation_id -> approval_id
        self.approval_surface = approval_surface or ApprovalSurface(clock=clock)
        self.outcome_store = outcome_store or InMemoryOutcomeStore()
        # Placeholder, same spirit as `default_tier_resolver` -- see module
        # docstring. Real externality resolution (domain/org-membership
        # lookup) is out of scope for this ticket; everything is treated
        # as internal until a real predicate is supplied.
        self._is_external = is_external

    # -- Outbound, gated through the circuit breaker -----------------

    def _post(
        self, board: BoardTransport, conversation_id: str, payload
    ) -> BoardMessage | None:
        now = self._clock()
        new_breaker, alert = evaluate_send(self._breaker, self._send_log, now=now)
        self._breaker = new_breaker
        if alert is not None:
            in_flight = [
                s
                for s in self._negotiations.values()
                if s.outcome is NegotiationOutcome.IN_PROGRESS
            ]
            for stood in stand_down_all(
                in_flight, owner_identity=self.identity, now=now
            ):
                self._negotiations[stood.negotiation_id] = stood
                self.ledger.release_for_negotiation(stood.negotiation_id)
        if self._breaker.tripped:
            return None
        self._send_log.append(now)
        # Argus round 1 finding: entries older than the circuit-breaker
        # window are never consulted again (evaluate_send only counts
        # within `window` of `now`) but previously accumulated forever --
        # unbounded memory growth for any long-lived identity handling many
        # negotiations. Trim to the sliding window on every post.
        cutoff = now - DEFAULT_CIRCUIT_BREAKER_WINDOW
        self._send_log = [t for t in self._send_log if t >= cutoff]
        return board.post(
            conversation_id=conversation_id, sender_id=self.identity, payload=payload
        )

    # -- Opening -------------------------------------------------------

    def open_negotiation(
        self,
        board: BoardTransport,
        *,
        to_agent_identity: str,
        request: AvailabilityRequest,
    ) -> str:
        conversation_id = board.open(
            owner=self.identity, participants=[to_agent_identity]
        )
        now = self._clock()
        budget_kwargs = (
            {}
            if self._round_budget_override is None
            else {"round_budget": self._round_budget_override}
        )
        state = start_negotiation(
            from_agent_identity=self.identity,
            to_agent_identity=to_agent_identity,
            now=now,
            negotiation_id=conversation_id,
            **budget_kwargs,
        )
        self._negotiations[conversation_id] = state
        self._post(board, conversation_id, request)
        return conversation_id

    # -- Reacting --------------------------------------------------------

    def _latest_by_sender(
        self, messages: list[BoardMessage]
    ) -> dict[str, BoardMessage]:
        latest: dict[str, BoardMessage] = {}
        for message in messages:
            latest[message.sender_id] = (
                message  # messages are seq-ordered; last write wins
            )
        return latest

    def is_my_turn(self, board: BoardTransport, conversation_id: str) -> bool:
        participants = board.participants_of(conversation_id)
        if len(participants) != 2:
            raise NotImplementedError(
                "multi-party (>2) negotiations are explicitly deferred "
                "(docs/DESIGN.md open question 5) -- Negotiator only "
                "handles the two-party case"
            )
        counterpart = next(p for p in participants if p != self.identity)
        history = board.read_since(
            conversation_id=conversation_id, agent_id=self.identity
        )
        latest = self._latest_by_sender(history)
        counterpart_latest = latest.get(counterpart)
        my_latest = latest.get(self.identity)
        if counterpart_latest is None:
            return False  # nobody has replied to my opening request yet
        if my_latest is not None and my_latest.seq > counterpart_latest.seq:  # noqa: SIM103
            return False  # I already had the last word; waiting on them
        return True

    def _counterpart_of(self, board: BoardTransport, conversation_id: str) -> str:
        participants = board.participants_of(conversation_id)
        if len(participants) != 2:
            raise NotImplementedError(
                "multi-party (>2) negotiations are explicitly deferred "
                "(docs/DESIGN.md open question 5) -- Negotiator only "
                "handles the two-party case"
            )
        return next(p for p in participants if p != self.identity)

    def _ensure_negotiation_state(
        self, conversation_id: str, counterpart: str
    ) -> NegotiationRoundState:
        """Initialize round-budget/outcome tracking for a conversation this
        identity did not open itself. Before this fix, `self._negotiations`
        was only ever populated in `open_negotiation` -- the RESPONDING side
        of every negotiation had no `NegotiationRoundState` at all, silently
        skipping round-budget tracking, circuit-breaker stand-down, and
        ledger release-on-terminal for that side (Argus round 1 finding)."""

        state = self._negotiations.get(conversation_id)
        if state is not None:
            return state
        budget_kwargs = (
            {}
            if self._round_budget_override is None
            else {"round_budget": self._round_budget_override}
        )
        state = start_negotiation(
            from_agent_identity=counterpart,
            to_agent_identity=self.identity,
            now=self._clock(),
            negotiation_id=conversation_id,
            **budget_kwargs,
        )
        self._negotiations[conversation_id] = state
        return state

    def sweep_expired_booking_approvals(self) -> list[str]:
        """TECH-4961's expire-must-release half, wired at last for the
        booking gate (Argus round 1 finding: `sweep_expired` existed on
        `ApprovalSurface` but nothing in `Negotiator` ever called it, so a
        booking approval hold could never expire -- the negotiation stayed
        `IN_PROGRESS` forever holding a permanent `BOOKED` ledger row).
        Called at the top of every `react()` so it runs on every tick
        regardless of whose turn it is; also safe to call directly on a
        scheduler's own cadence. Returns the conversation_ids released.

        Argus round 2 findings, both fixed here: (1) this method took an
        unused `board` parameter -- nothing in its body ever referenced
        it; removed. (2) `_on_release` used to pop
        `_pending_booking_approvals` BEFORE calling `ledger.
        release_for_negotiation()` -- the same ordering bug fixed in
        `respond_to_booking_approval`'s `_release` for the identical
        reason: if the ledger call raises, a retry sweep would find the
        pending marker already gone even though `ApprovalSurface`'s own
        per-hold error isolation (`approvals.sweep_expired`) correctly
        left the hold PENDING-and-expired for retry. Reversed to match.
        `ApprovalSurface.sweep_expired`'s own per-hold isolation means a
        single conversation's release failure here no longer prevents
        this method from returning the others it did successfully release.

        **Argus round 3 finding, documented; Argus round 4 finding,
        corrected:** the returned list contains only conversation_ids that
        were actually released this call. A conversation whose
        `_on_release` raised (ledger error, etc.) is silently omitted from
        the return value (tracked as TECH-5075, along with the unbounded
        retry behavior described there; that note also documents this
        list's `list[str]` shape vs. `ApprovalSurface.sweep_expired`'s own
        `list[ApprovalHold]`) -- it remains in `_pending_booking_approvals`
        and PENDING-and-expired in the `ApprovalSurface`, to be retried by
        the next sweep. A caller that needs to know about failures, not
        just successes, should inspect the logs this method's own call to
        `ApprovalSurface.sweep_expired` emits during this invocation (each
        failure is logged with the conversation's resume_token, TECH-5075)
        -- do NOT call `self.approval_surface.sweep_expired` directly:
        doing so bypasses `_on_release` above, so the ledger hold is never
        released and `_pending_booking_approvals` is never popped, leaving
        the conversation permanently stranded (`maybe_finalize` will
        short-circuit on it forever, and `respond_to_booking_approval`
        will fail its PENDING check on every future call)."""

        released: list[str] = []

        def _on_release(hold: ApprovalHold) -> None:
            conversation_id = hold.resume_token
            self.ledger.release_for_negotiation(conversation_id)
            self._pending_booking_approvals.pop(conversation_id, None)
            released.append(conversation_id)

        self.approval_surface.sweep_expired(on_release=_on_release, now=self._clock())
        return released

    def react(
        self,
        board: BoardTransport,
        conversation_id: str,
        *,
        my_candidates: list[SlotContext],
        rules: list[Rule],
        tier_resolver: Callable[[AvailabilityRequest], Tier] = default_tier_resolver,
    ) -> None:
        """Process the counterparty's latest message and take exactly one
        action: propose my own ranked slots, confirm a match, or decline.
        No-ops if it isn't my turn (`is_my_turn`) or the negotiation is
        already terminal."""

        self.sweep_expired_booking_approvals()

        counterpart = self._counterpart_of(board, conversation_id)
        state = self._ensure_negotiation_state(conversation_id, counterpart)
        if state.is_terminal:
            return
        if not self.is_my_turn(board, conversation_id):
            return

        history = board.read_since(
            conversation_id=conversation_id, agent_id=self.identity
        )
        latest = self._latest_by_sender(history)
        counterpart_latest = latest[counterpart]
        my_latest = latest.get(self.identity)
        payload = counterpart_latest.payload

        if isinstance(payload, Decline):
            self.ledger.release_for_negotiation(conversation_id)
            return

        if isinstance(payload, NeedsClarification):
            return  # pause signal -- a human/EA judgment call, out of scope here

        ranked = (
            rank_slots(my_candidates, rules, limit=MAX_SLOTS_PER_MESSAGE)
            if my_candidates
            else []
        )
        by_start = {(s.slot.start, s.slot.end): s for s in ranked}

        if isinstance(payload, Confirm):
            # Ping-pong guard (Argus round 1 finding, correctness-critical):
            # if MY OWN latest message is already a Confirm for this exact
            # slot, I have nothing new to do -- posting another Confirm
            # here would make the counterparty's next react() see a newer
            # Confirm from me and re-confirm again, unboundedly, until the
            # per-identity circuit breaker trips and stands down every
            # in-flight negotiation for both owners. Completion detection
            # (`check_completion`) and booking are handled elsewhere
            # (`maybe_finalize`/`respond_to_booking_approval`), not by
            # re-sending a message that changes nothing on the wire.
            if (
                my_latest is not None
                and isinstance(my_latest.payload, Confirm)
                and _same_slot(my_latest.payload.slot, payload.slot)
            ):
                return
            # `mine is None` (no candidate at exactly this start/end in my
            # CURRENT candidate list) is common and expected when this is a
            # slot I originally offered myself -- it was already sent, not
            # necessarily still in "my_candidates" (my pool of what to
            # offer NEXT). Rather than trust that blindly, verify it
            # against my own ledger: a slot I actually offered or already
            # hold for this exact negotiation has a reservation row keyed
            # to (my identity, this slot start, this negotiation_id).
            # Trusting an unverified "mine is None" was itself an Argus
            # round 1 finding against the module's own "never trust the
            # counterparty's self-reported preference alone" invariant.
            mine = by_start.get((payload.slot.start, payload.slot.end))
            if mine is not None:
                confirm_echo_acceptable = mine.preference >= ACCEPTANCE_THRESHOLD
            else:
                held = self.ledger.get(
                    owner=self.identity, slot_start_utc=payload.slot.start
                )
                confirm_echo_acceptable = (
                    held is not None and held.negotiation_id == conversation_id
                )
            if confirm_echo_acceptable:
                self._try_confirm(
                    board, conversation_id, payload.slot, tier_resolver, request=None
                )
            return

        offered: tuple[ScoredSlot, ...] = ()
        if isinstance(payload, (AvailabilityResponse, CounterProposal)):
            offered = payload.slots

        # Deliberately never trust the counterparty's self-reported
        # preference alone: a slot is only acceptable if it also appears
        # in MY OWN candidate set (i.e. my own calendar/rules consider it
        # feasible for me) and clears my own acceptance bar. Blindly
        # accepting an offer that isn't in `by_start` would mean booking a
        # time nobody has verified against my calendar at all -- exactly
        # the "judgments cross the boundary, never raw data" boundary the
        # wire schema exists to enforce, applied on the receiving side too.
        acceptable = None
        for candidate in offered:
            key = (candidate.slot.start, candidate.slot.end)
            mine = by_start.get(key)
            if mine is not None and mine.preference >= ACCEPTANCE_THRESHOLD:
                acceptable = candidate.slot
                break

        if acceptable is not None:
            self._try_confirm(
                board, conversation_id, acceptable, tier_resolver, request=None
            )
            return

        if isinstance(payload, AvailabilityRequest):
            self._respond_with_ranked_slots(
                board,
                conversation_id,
                ranked,
                tier_resolver,
                request=payload,
                response_type="response",
            )
            return

        # CounterProposal/AvailabilityResponse with nothing acceptable --
        # advance the round and counter with my own top slots, or
        # exhaust the budget if this was the last one.
        self._advance_or_exhaust(board, conversation_id, ranked, tier_resolver)

    def _hold_ttl(self, tier_resolver, request: AvailabilityRequest | None):
        if request is None:
            return hold_ttl_for(Tier.TIER_3)
        return hold_ttl_for(tier_resolver(request))

    def _try_confirm(
        self, board, conversation_id, slot: CandidateSlot, tier_resolver, *, request
    ) -> None:
        ttl = self._hold_ttl(tier_resolver, request)
        result = self.ledger.claim(
            owner=self.identity,
            slot_start_utc=slot.start,
            slot_end_utc=slot.end,
            negotiation_id=conversation_id,
            ttl=ttl,
        )
        if result.verdict is not ClaimVerdict.CLAIMED:
            # Can't honestly confirm a slot this identity can't hold --
            # ask for clarification rather than confirm-then-fail.
            history = board.read_since(
                conversation_id=conversation_id, agent_id=self.identity
            )
            self._post(
                board, conversation_id, NeedsClarification(about_seq=history[-1].seq)
            )
            return

        promotion = self.ledger.promote(
            owner=self.identity,
            slot_start_utc=slot.start,
            negotiation_id=conversation_id,
        )
        if promotion.verdict is not ClaimVerdict.CLAIMED:
            # Argus round 1 finding: this return value was previously
            # discarded, silently ignoring a BLOCKED/ERROR promote() and
            # violating the ledger's own fail-closed contract. Same
            # fallback as a failed claim above -- ask for clarification
            # rather than proceed on state we couldn't actually establish.
            history = board.read_since(
                conversation_id=conversation_id, agent_id=self.identity
            )
            self._post(
                board, conversation_id, NeedsClarification(about_seq=history[-1].seq)
            )
            return

        posted = self._post(board, conversation_id, Confirm(slot=slot))
        if posted is None:
            # Argus round 1 finding: `_post` can silently return `None`
            # (e.g. the circuit breaker suppressed the send) after the
            # ledger was already promoted to a permanent BOOKED hold above
            # -- without this rollback, the slot would be blocked forever
            # with no Confirm ever having reached the counterparty. Release
            # rather than leave a phantom commitment nobody agreed to.
            self.ledger.release_for_negotiation(conversation_id)

    def _respond_with_ranked_slots(
        self,
        board,
        conversation_id,
        ranked,
        tier_resolver,
        *,
        request,
        response_type: str,
    ) -> None:
        if not ranked:
            payload_cls = (
                AvailabilityResponse if response_type == "response" else CounterProposal
            )
            self._post(
                board,
                conversation_id,
                payload_cls(
                    none_available=True,
                    reason=DeclineReason.NO_AVAILABILITY_WITHIN_CONSTRAINTS,
                ),
            )
            return
        ttl = self._hold_ttl(tier_resolver, request)
        scored_slots = []
        claim_failures = 0
        for candidate in ranked:
            claim = self.ledger.claim(
                owner=self.identity,
                slot_start_utc=candidate.slot.start,
                slot_end_utc=candidate.slot.end,
                negotiation_id=conversation_id,
                ttl=ttl,
            )
            if claim.verdict is ClaimVerdict.CLAIMED:
                scored_slots.append(
                    ScoredSlot(
                        slot=CandidateSlot(
                            start=candidate.slot.start, end=candidate.slot.end
                        ),
                        preference=candidate.preference,
                    )
                )
            else:
                # Argus round 1 finding: a claim BLOCKED/ERROR was
                # previously silently dropped -- a single store error
                # could degrade an entire offer set with no diagnostic at
                # all, indistinguishable from "genuinely nothing available."
                claim_failures += 1
                logger.warning(
                    "ledger claim %s for %s slot_start=%s negotiation_id=%s",
                    claim.verdict.value,
                    self.identity,
                    candidate.slot.start.isoformat(),
                    conversation_id,
                )
        payload_cls = (
            AvailabilityResponse if response_type == "response" else CounterProposal
        )
        if not scored_slots:
            # Distinguish "nothing was available" from "every candidate
            # failed to claim" (infra failure) -- OTHER is the closest fit
            # in the closed wire vocabulary (wire.DeclineReason) for the
            # latter; NO_AVAILABILITY_WITHIN_CONSTRAINTS stays reserved for
            # the genuine no-candidates case.
            reason = (
                DeclineReason.OTHER
                if claim_failures and claim_failures == len(ranked)
                else DeclineReason.NO_AVAILABILITY_WITHIN_CONSTRAINTS
            )
            self._post(
                board, conversation_id, payload_cls(none_available=True, reason=reason)
            )
            return
        self._post(board, conversation_id, payload_cls(slots=tuple(scored_slots)))

    def _advance_or_exhaust(
        self, board, conversation_id, ranked, tier_resolver
    ) -> None:
        state = self._negotiations.get(conversation_id)
        now = self._clock()
        suggested = [
            CandidateSlot(start=s.slot.start, end=s.slot.end) for s in ranked[:5]
        ]
        if state is not None and state.round >= state.round_budget:
            self._negotiations[conversation_id] = exhaust_round_budget(
                state, now=now, suggested_slots=suggested
            )
            self.ledger.release_for_negotiation(conversation_id)
            return
        if state is not None:
            self._negotiations[conversation_id] = advance_round(state)
        self._respond_with_ranked_slots(
            board,
            conversation_id,
            ranked,
            tier_resolver,
            request=None,
            response_type="counter",
        )

    # -- Completion --------------------------------------------------------

    def check_completion(
        self, board: BoardTransport, conversation_id: str
    ) -> CandidateSlot | None:
        """`docs/DESIGN.md` §2.2 point 1: complete iff every active
        participant's latest substantive message is `Confirm` naming the
        identical slot. Returns that slot, or `None` if not yet complete."""

        participants = board.participants_of(conversation_id)
        history = board.read_since(
            conversation_id=conversation_id, agent_id=self.identity
        )
        latest = self._latest_by_sender(history)
        confirmed_slots: list[CandidateSlot] = []
        for participant in participants:
            message = latest.get(participant)
            if message is None or not isinstance(message.payload, Confirm):
                return None
            confirmed_slots.append(message.payload.slot)
        first = confirmed_slots[0]
        if all(_same_slot(first, s) for s in confirmed_slots[1:]):
            return first
        return None

    def _counterparty_class(
        self, board: BoardTransport, conversation_id: str
    ) -> tuple[str, bool]:
        participants = board.participants_of(conversation_id)
        counterpart = next(p for p in participants if p != self.identity)
        is_ext = self._is_external(counterpart)
        return ("external" if is_ext else "internal"), is_ext

    def _do_book(
        self,
        board: BoardTransport,
        conversation_id: str,
        slot: CandidateSlot,
        on_book: Callable[[str, CandidateSlot], None],
    ) -> None:
        # Argus round 1 finding: `on_book` used to run LAST, after this
        # identity was already marked booked/non-pending -- if `on_book`
        # raised (e.g. a calendar API failure), that state was permanently
        # stuck: `conversation_id in self._booked` would short-circuit
        # every future `maybe_finalize` call with no path to retry. Calling
        # `on_book` first means a raise here leaves nothing mutated, so a
        # crash between the call and the bookkeeping below is recoverable
        # by simply calling `maybe_finalize` again.
        on_book(conversation_id, slot)
        state = self._negotiations.get(conversation_id)
        if state is not None and not state.is_terminal:
            self._negotiations[conversation_id] = book(state, now=self._clock())
        self._booked.add(conversation_id)
        self._pending_booking_approvals.pop(conversation_id, None)

    def maybe_finalize(
        self,
        board: BoardTransport,
        conversation_id: str,
        *,
        on_book: Callable[[str, CandidateSlot], None],
    ) -> bool:
        """If this identity owns the conversation and it has completed,
        request booking through the booking gate (`docs/DESIGN.md` §2.2
        point 3, decided 2026-08-11: "agent books it with approval... over
        time we'll be able to give the agent autonomy on booking").
        Completion alone is never sufficient to book — it only makes this
        identity *eligible* to request it. An external counterparty is
        always `ask_first` (mirrors `send_external_invite`'s permanent
        gate); an internal one earns `act` from a real approval track
        record via `booking_gate.evaluate_booking_gate`, starting
        ask-first for a fresh (owner, counterparty-class) neighborhood
        exactly like every other confidence-gated action in this design.

        Returns `True` only when booking actually happened in this call
        (an immediate `act`) — an `ask_first` outcome opens a pending
        approval (`respond_to_booking_approval` resolves it later) and
        returns `False`, the same as "not yet complete." Non-owners do
        nothing here — they already promoted their own ledger hold when
        they posted their own `Confirm` (`_try_confirm`); booking the
        calendar invite is the owner's job alone."""

        if (
            conversation_id in self._booked
            or conversation_id in self._pending_booking_approvals
        ):
            return False
        if board.owner_of(conversation_id) != self.identity:
            return False
        slot = self.check_completion(board, conversation_id)
        if slot is None:
            return False

        counterparty_class, is_ext = self._counterparty_class(board, conversation_id)
        outcomes = self.outcome_store.query(
            owner_identity=self.identity,
            action_type=BOOKING_ACTION_KEY,
            counterparty_class=counterparty_class,
        )
        # Argus round 1 finding, escalated rather than fixed this round:
        # this hand-rolls ConfidenceInputs instead of calling `outcomes.
        # confidence_inputs_with_rejections` (added in this same PR for
        # exactly this purpose). Not an oversight -- that function also
        # folds in `most_recent_decision_at` and `explicit_rule_covered`
        # from `AutonomyAuditRecord` history, and booking has no such
        # history: `evaluate_booking_gate` writes no audit record at all
        # (see booking_gate.py's own module docstring), because
        # `AutonomyAuditRecord.action_type` is typed to the upstream
        # `ActionType` enum, which has no member for "book a negotiated
        # meeting" -- the same gap that makes this module's gate a
        # parallel one rather than a call to `evaluate_gate`. Wiring this
        # properly needs that upstream ActionType addition first (TECH-5070,
        # a coordinated rh-scheduler-mcp + reclaw-ea change); until
        # then, this simpler approval/rejection count is the honest
        # subset of confidence_inputs_with_rejections this booking gate
        # can actually support, and `explicit_rule_covered`'s "3 if
        # rule-covered" fast path (docs/DESIGN.md §5) does not apply here.
        confidence_inputs = ConfidenceInputs(
            approval_count=sum(1 for o in outcomes if o.approved),
            rejection_count=sum(1 for o in outcomes if not o.approved),
        )
        gate_result = evaluate_booking_gate(
            is_external=is_ext, confidence_inputs=confidence_inputs
        )

        if gate_result.decision is Decision.ACT:
            # Argus round 1 finding, correctness-critical: this branch
            # previously called `record_outcome(..., approved=True)` for a
            # purely autonomous decision -- no owner was ever asked.
            # `OwnerResponseOutcome` is documented as "one recorded owner
            # response to an ask_first gate decision," and `approval_count`
            # (queried just above from this same store) is the sole input
            # `evaluate_booking_gate` uses to grant autonomy in the first
            # place. Self-recording every autonomous act as another
            # "approval" would let a neighborhood that crossed the
            # threshold once compound its own confidence indefinitely with
            # zero further owner involvement. Record nothing here: an ACT
            # decision leaves no owner-response evidence to log, by
            # definition.
            self._do_book(board, conversation_id, slot, on_book)
            return True

        hold = self.approval_surface.request(
            owner=self.identity,
            resume_token=conversation_id,
            gate_result=gate_result,
            ttl=DEFAULT_APPROVAL_TTL,
        )
        self._pending_booking_approvals[conversation_id] = hold.id
        return False

    def respond_to_booking_approval(
        self,
        board: BoardTransport,
        conversation_id: str,
        *,
        approved: bool,
        on_book: Callable[[str, CandidateSlot], None],
    ) -> None:
        """Resolve a pending booking approval opened by `maybe_finalize`.
        Approving books immediately (`approve-must-advance`); rejecting
        releases the pending hold without booking and records the
        rejection, so a future `maybe_finalize` call for the same
        conversation is free to re-request the gate (fast demotion, per
        `docs/DESIGN.md` §5, still applies to the NEXT booking decision in
        this (owner, counterparty-class) neighborhood — this call does not
        retry the current one)."""

        approval_id = self._pending_booking_approvals.get(conversation_id)
        if approval_id is None:
            raise ValueError(
                f"no pending booking approval for conversation {conversation_id!r}"
            )
        counterparty_class, _ = self._counterparty_class(board, conversation_id)
        slot = self.check_completion(board, conversation_id)
        # Argus round 2 finding: this used to raise on `slot is None`
        # unconditionally, before branching on `approved` -- a REJECTION
        # doesn't need `slot` at all (it only releases the ledger hold),
        # so a legitimate rejection could fail with ValueError purely
        # because completion had lapsed, when the rejection itself could
        # have resolved cleanly. Scope the lapse guard to the approval
        # branch only, where `slot` actually gets passed to `on_book`.
        if approved and slot is None:
            # Argus round 1 finding: the board state can change during the
            # up-to-several-hour approval window (a counterparty message
            # can supersede a standing confirm -- "completed conversations
            # never reopen" is a DESIGN.md invariant this orchestrator does
            # not yet enforce end-to-end). Silently passing None into
            # on_book's calendar-write path is worse than a loud failure;
            # `maybe_finalize` already guards this same case at its own
            # `check_completion` call.
            raise ValueError(
                f"completion lapsed for {conversation_id!r} before its booking approval resolved -- "
                "the negotiation is no longer fully confirmed"
            )

        def _advance(_hold) -> None:
            self._do_book(board, conversation_id, slot, on_book)

        def _release(_hold) -> None:
            # Argus round 2 finding: this used to pop the pending marker
            # BEFORE calling ledger.release_for_negotiation() -- if that
            # call raised LedgerError, the pop had already happened, so a
            # retry of respond_to_booking_approval would find approval_id
            # already gone from _pending_booking_approvals and raise "no
            # pending booking approval," even though the underlying
            # ApprovalSurface hold was still genuinely PENDING and
            # retryable at that layer. Reversed: release the ledger hold
            # first, and only clear the pending marker once that succeeds.
            #
            # Argus round 1 finding, still the reason this release exists
            # at all: `_try_confirm` already promoted this identity's
            # ledger hold to a permanent BOOKED row (§2.2 point 2 --
            # promotion happens at confirm time, before this later
            # booking-gate approval even runs). A rejection here must
            # release that hold explicitly, or it's a permanent block on
            # this slot with no calendar event and no reaper that would
            # ever find it (`reap_expired` only sweeps OFFERED rows;
            # `reconcile_booked` needs a live calendar check nothing wires
            # up for a booking that was rejected, not cancelled).
            #
            # Argus round 3 finding, noted rather than fixed; Argus round 4
            # finding, ticketed (TECH-5076) and comment corrected: this
            # function only reaches `respond_and_record` (and therefore
            # this closure) because `ApprovalSurface.respond` first
            # confirmed the hold is still PENDING -- i.e. the expiry sweep
            # has not yet run and flipped it to EXPIRED (NOT "its 2h TTL
            # has not yet lapsed," as a prior version of this comment
            # claimed: `respond()` checks status only, never the TTL
            # itself, so a hold can be past its TTL and still PENDING until
            # a sweep actually runs). If `release_for_negotiation` keeps
            # failing and the caller's retries don't succeed before a
            # sweep runs, `sweep_expired_booking_approvals` (not this
            # method) will eventually pick the hold up as EXPIRED instead,
            # and the owner's explicit rejection here is superseded by an
            # expiry rather than ever being recorded as the rejection
            # outcome it actually was. Not a data-loss bug in the ledger
            # sense (the ledger hold still gets released, just later, by
            # the sweep instead of this path), but the REJECTION as a
            # confidence signal is lost if this doesn't succeed before that
            # happens -- tracked as TECH-5076, see also docs/DESIGN.md §10.
            self.ledger.release_for_negotiation(conversation_id)
            self._pending_booking_approvals.pop(conversation_id, None)

        respond_and_record(
            self.approval_surface,
            approval_id,
            approved=approved,
            owner_identity=self.identity,
            action_type=BOOKING_ACTION_KEY,
            counterparty_class=counterparty_class,
            outcome_store=self.outcome_store,
            on_advance=_advance,
            on_release=_release,
            now=self._clock(),
        )

    def state_for(self, conversation_id: str) -> NegotiationRoundState | None:
        return self._negotiations.get(conversation_id)

    def has_pending_booking_approval(self, conversation_id: str) -> bool:
        """Whether `conversation_id` has an open booking-approval hold from
        `maybe_finalize`, awaiting `respond_to_booking_approval`. Public
        accessor so callers (e.g. reclaw-ea-mcp's `ea_request_booking`
        tool) don't need to reach into `_pending_booking_approvals`
        directly -- see TECH-5077, which flagged the equivalent pattern in
        this repo's own tests."""
        return conversation_id in self._pending_booking_approvals
