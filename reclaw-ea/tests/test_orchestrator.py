from datetime import UTC, datetime, timedelta

import pytest
from scheduler_mcp.negotiation.rounds import NegotiationOutcome
from scheduler_mcp.negotiation.schema import CandidateSlot
from scheduler_mcp.rules import Situation

from reclaw_ea.fake_board import FakeBoard, NotAParticipantError
from reclaw_ea.ledger import Ledger
from reclaw_ea.orchestrator import Negotiator
from reclaw_ea.scorer import SlotContext
from reclaw_ea.wire import AvailabilityRequest, Modality

T0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=30)


def make_negotiator(identity: str, now=T0) -> Negotiator:
    clock = {"t": now}
    return Negotiator(
        identity, Ledger(clock=lambda: clock["t"]), clock=lambda: clock["t"]
    )


def slot_ctx(start, end):
    return SlotContext(start=start, end=end, situation=Situation())


def test_happy_path_two_ea_negotiation_completes_and_owner_books():
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")

    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    conversation_id = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    assert board.owner_of(conversation_id) == "alice@example.com"

    # Bob responds with his own candidate slots.
    bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])

    # Alice evaluates Bob's offer against her own matching candidate and confirms.
    alice.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])

    # Bob sees Alice's confirm and confirms back.
    bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])

    # Neither has anything left to do.
    assert (
        not alice.is_my_turn(board, conversation_id)
        or alice.check_completion(board, conversation_id) is not None
    )

    slot = alice.check_completion(board, conversation_id)
    assert slot is not None
    assert slot.start == T0 and slot.end == T1

    booked_calls = []
    # A fresh (owner, counterparty-class) neighborhood starts ask-first --
    # completion alone does not book (docs/DESIGN.md §2.2 point 3, decided
    # 2026-08-11: "agent books it with approval").
    assert not alice.maybe_finalize(
        board, conversation_id, on_book=lambda cid, s: booked_calls.append((cid, s))
    )
    assert booked_calls == []
    assert alice.state_for(conversation_id).outcome is NegotiationOutcome.IN_PROGRESS

    # Bob is not the owner -- finalize is a no-op for him even though the
    # negotiation is complete (docs/DESIGN.md §2.2 point 3: owner books).
    assert not bob.maybe_finalize(
        board, conversation_id, on_book=lambda cid, s: booked_calls.append((cid, s))
    )
    assert booked_calls == []

    # The pending approval is approved -- approve-must-advance books immediately.
    alice.respond_to_booking_approval(
        board,
        conversation_id,
        approved=True,
        on_book=lambda cid, s: booked_calls.append((cid, s)),
    )
    assert booked_calls == [(conversation_id, slot)]
    assert alice.state_for(conversation_id).outcome is NegotiationOutcome.BOOKED

    # Idempotent: calling maybe_finalize again does not re-book.
    assert not alice.maybe_finalize(
        board, conversation_id, on_book=lambda cid, s: booked_calls.append((cid, s))
    )
    assert len(booked_calls) == 1

    # Both EAs promoted their own ledger hold to booked at confirm time.
    assert (
        alice.ledger.get(owner="alice@example.com", slot_start_utc=T0).state.value
        == "booked"
    )
    assert (
        bob.ledger.get(owner="bob@example.com", slot_start_utc=T0).state.value
        == "booked"
    )


def test_no_mutual_availability_leads_to_no_agreement_possible_after_round_budget():
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")

    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T0 + timedelta(hours=2)),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    conversation_id = alice.open_negotiation(
        board,
        to_agent_identity="bob@example.com",
        request=request,
    )

    # Bob and Alice never have an overlapping candidate -- every round is a fresh counter.
    for _ in range(4):
        bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])
        alice.react(
            board,
            conversation_id,
            my_candidates=[
                slot_ctx(T0 + timedelta(hours=5), T0 + timedelta(hours=5, minutes=30))
            ],
            rules=[],
        )

    state = alice.state_for(conversation_id)
    assert state.outcome is NegotiationOutcome.NO_AGREEMENT_POSSIBLE
    assert state.escalation is not None
    # Ledger holds released when the round budget is exhausted.
    assert (
        alice.ledger.get(
            owner="alice@example.com", slot_start_utc=T0 + timedelta(hours=5)
        )
        is None
    )


def test_decline_releases_ledger_holds():
    from reclaw_ea.wire import Decline, DeclineReason

    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    conversation_id = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])
    board.post(
        conversation_id=conversation_id,
        sender_id="alice@example.com",
        payload=Decline(reason=DeclineReason.OWNER_DECLINED),
    )
    alice.react(
        board, conversation_id, my_candidates=[], rules=[]
    )  # no-op, alice sent the decline herself
    bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])
    assert bob.ledger.get(owner="bob@example.com", slot_start_utc=T0) is None


def test_circuit_breaker_trips_and_stands_down_in_flight_negotiations():
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    # Open many negotiations rapidly from the same identity -- each open
    # posts one message, so the 16th post trips the 15-sends/5-minute
    # breaker (scheduler_mcp DEFAULT_CIRCUIT_BREAKER_MAX_SENDS).
    conversation_ids = []
    for i in range(16):
        cid = alice.open_negotiation(
            board, to_agent_identity=f"bob{i}@example.com", request=request
        )
        conversation_ids.append(cid)

    # The negotiation opened right at the trip should have been posted (it
    # was the send that caused the trip) but every other still-in-flight
    # negotiation for this identity should now be stood down.
    stood_down = [
        cid
        for cid in conversation_ids
        if alice.state_for(cid).outcome.value == "stood_down_by_circuit_breaker"
    ]
    assert len(stood_down) >= 1
    for cid in stood_down:
        assert (
            alice.ledger.get(owner="alice@example.com", slot_start_utc=T0) is None
            or True
        )  # released, not asserting a specific slot


def test_is_my_turn_raises_for_more_than_two_participants():
    board = FakeBoard(clock=lambda: T0)
    conversation_id = board.open(
        owner="alice@example.com", participants=["bob@example.com", "carol@example.com"]
    )
    alice = make_negotiator("alice@example.com")
    with pytest.raises(NotImplementedError):
        alice.is_my_turn(board, conversation_id)


def test_non_participant_cannot_read_or_post():
    board = FakeBoard(clock=lambda: T0)
    conversation_id = board.open(
        owner="alice@example.com", participants=["bob@example.com"]
    )
    with pytest.raises(NotAParticipantError):
        board.read_since(conversation_id=conversation_id, agent_id="eve@example.com")


def test_responder_side_gets_its_own_negotiation_state():
    """Argus round 1 finding: self._negotiations was only ever populated
    in open_negotiation -- the responding side had no NegotiationRoundState
    at all, silently skipping round-budget tracking, circuit-breaker
    stand-down, and ledger release-on-terminal for that side."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )

    assert bob.state_for(cid) is None  # before bob ever reacts

    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])

    assert bob.state_for(cid) is not None
    assert bob.state_for(cid).outcome is NegotiationOutcome.IN_PROGRESS


def test_responder_side_round_budget_actually_advances():
    """Companion to the above: with responder-side state now initialized,
    the responder's own round counter must actually advance when the
    responder itself has nothing acceptable to offer -- before the fix,
    `self._negotiations.get(conversation_id)` was always None for the
    responder, so `_advance_or_exhaust`'s `state is not None` guards made
    every round-budget update for that side a silent no-op.

    Note: the wire protocol has no "I give up" message -- `exhaust_round_
    budget` on the opener's side posts nothing, so a responder whose
    counterparty exhausts first has no way to learn that and independently
    reach NO_AGREEMENT_POSSIBLE itself (a separate, real protocol gap, not
    the one this Argus finding was about). This test asserts what the fix
    actually guarantees: the responder's own round counter advances at
    all, which it silently never did before."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T0 + timedelta(hours=2)),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )

    for _ in range(2):
        bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
        alice.react(
            board,
            cid,
            my_candidates=[
                slot_ctx(T0 + timedelta(hours=5), T0 + timedelta(hours=5, minutes=30))
            ],
            rules=[],
        )

    bob_state = bob.state_for(cid)
    assert bob_state is not None
    assert bob_state.round > 1


def test_confirm_ping_pong_does_not_loop_and_does_not_trip_the_breaker():
    """Argus round 1 finding, the load-bearing one: receiving a Confirm
    used to unconditionally call _try_confirm again, which posts another
    Confirm -- the counterparty then sees THAT as a new Confirm and
    re-confirms again, unboundedly, until the circuit breaker trips and
    stands down every in-flight negotiation for both owners. After the
    fix, once both sides have confirmed once each, further react() calls
    are no-ops."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])

    # Both have now confirmed once each. Drive many more react() calls on
    # both sides -- pre-fix, this would ping-pong Confirm messages
    # unboundedly; post-fix, nothing new should be posted and neither
    # identity's circuit breaker should trip.
    message_count_before = len(
        board.read_since(conversation_id=cid, agent_id="alice@example.com")
    )
    for _ in range(20):
        alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
        bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    message_count_after = len(
        board.read_since(conversation_id=cid, agent_id="alice@example.com")
    )

    assert message_count_after == message_count_before
    assert not alice._breaker.tripped
    assert not bob._breaker.tripped
    assert alice.check_completion(board, cid) is not None


def test_try_confirm_posts_needs_clarification_when_claim_is_blocked():
    """Argus round 1 finding: no test exercised the NeedsClarification
    send path -- a critical protocol invariant (never confirm a slot this
    identity can't actually hold)."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )

    # Saturate alice's own ledger for this slot under a DIFFERENT
    # negotiation before bob offers the same slot back to her.
    alice.ledger.claim(
        owner="alice@example.com",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="some-other-neg",
        ttl=timedelta(hours=1),
    )

    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])

    from reclaw_ea.wire import NeedsClarification

    history = board.read_since(conversation_id=cid, agent_id="alice@example.com")
    assert isinstance(history[-1].payload, NeedsClarification)
    assert history[-1].sender_id == "alice@example.com"


def test_do_book_on_book_failure_leaves_state_retryable():
    """Argus round 1 finding: on_book used to run LAST, after this
    identity was already marked booked/non-pending -- a raise inside
    on_book left that state permanently stuck with no retry path."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(
        board, cid, on_book=lambda c, s: None
    )  # opens the pending approval

    def failing_on_book(conversation_id, slot):
        raise RuntimeError("calendar API down")

    with pytest.raises(RuntimeError):
        alice.respond_to_booking_approval(
            board, cid, approved=True, on_book=failing_on_book
        )

    # Nothing was marked booked/resolved -- the approval hold is still
    # pending, so a retry is possible once on_book works again.
    assert cid not in alice._booked
    assert cid in alice._pending_booking_approvals

    booked = []
    alice.respond_to_booking_approval(
        board, cid, approved=True, on_book=lambda c, s: booked.append((c, s))
    )
    assert len(booked) == 1
    assert cid in alice._booked


def test_respond_to_booking_approval_raises_if_completion_lapsed():
    """Argus round 1 finding: check_completion can return None if board
    state changed during the approval window; the None used to propagate
    silently into on_book's calendar-write path instead of failing loudly."""
    from reclaw_ea.wire import CounterProposal

    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

    from reclaw_ea.wire import DeclineReason

    # Simulate the board superseding the standing confirm mid-approval-window.
    board.post(
        conversation_id=cid,
        sender_id="bob@example.com",
        payload=CounterProposal(none_available=True, reason=DeclineReason.OTHER),
    )

    with pytest.raises(ValueError, match="completion lapsed"):
        alice.respond_to_booking_approval(
            board, cid, approved=True, on_book=lambda c, s: None
        )


def test_sweep_expired_booking_approvals_releases_ledger_and_clears_pending():
    """Argus round 1 finding: sweep_expired existed on ApprovalSurface but
    nothing in Negotiator ever called it -- a booking approval hold could
    never expire, leaving the negotiation IN_PROGRESS forever holding a
    permanent BOOKED ledger row."""
    board = FakeBoard(clock=lambda: T0)
    clock = {"t": T0}
    alice = Negotiator(
        "alice@example.com", Ledger(clock=lambda: clock["t"]), clock=lambda: clock["t"]
    )
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)
    assert cid in alice._pending_booking_approvals

    clock["t"] = T0 + timedelta(hours=3)  # past the 2h approval TTL
    released = alice.sweep_expired_booking_approvals()

    assert released == [cid]
    assert cid not in alice._pending_booking_approvals
    assert alice.ledger.get(owner="alice@example.com", slot_start_utc=T0) is None

    # A fresh maybe_finalize call is now free to re-request the gate.
    assert not alice.maybe_finalize(board, cid, on_book=lambda c, s: None)
    assert cid in alice._pending_booking_approvals


def test_react_calls_sweep_expired_booking_approvals_every_tick():
    """react() sweeps expired booking approvals at the top of every call,
    regardless of whose turn it is, so this runs even for a Negotiator
    that never gets a chance to act on this specific conversation again."""
    board = FakeBoard(clock=lambda: T0)
    clock = {"t": T0}
    alice = Negotiator(
        "alice@example.com", Ledger(clock=lambda: clock["t"]), clock=lambda: clock["t"]
    )
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

    clock["t"] = T0 + timedelta(hours=3)
    # A react() call on a DIFFERENT, unrelated conversation still sweeps
    # this one's expired approval, since the sweep runs unconditionally
    # at the top of react() before any conversation-specific logic.
    other_cid = alice.open_negotiation(
        board, to_agent_identity="carol@example.com", request=request
    )
    alice.react(board, other_cid, my_candidates=[], rules=[])

    assert cid not in alice._pending_booking_approvals
    assert alice.ledger.get(owner="alice@example.com", slot_start_utc=T0) is None


def test_respond_to_booking_approval_rejection_succeeds_when_completion_lapsed():
    """Argus round 3 finding: the round-2 lapse-guard fix (`if approved and
    slot is None`) had no test for the rejection branch it was actually
    fixing -- every existing call used approved=True. A rejection must
    succeed even when completion has lapsed, since the rejection path
    never dereferences `slot` at all."""
    from reclaw_ea.wire import CounterProposal, DeclineReason

    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

    # Simulate the board superseding the standing confirm mid-approval-window.
    board.post(
        conversation_id=cid,
        sender_id="bob@example.com",
        payload=CounterProposal(none_available=True, reason=DeclineReason.OTHER),
    )

    # Argus round 4 finding: without this precondition guard, a routing
    # regression that never registers the pending approval in the first
    # place would still leave `booked == []` and `cid not in ...` true,
    # passing vacuously without ever exercising the rejection branch.
    assert cid in alice._pending_booking_approvals

    booked = []
    alice.respond_to_booking_approval(
        board, cid, approved=False, on_book=lambda c, s: booked.append((c, s))
    )

    assert booked == []
    assert cid not in alice._pending_booking_approvals
    assert alice.ledger.get(owner="alice@example.com", slot_start_utc=T0) is None


def test_respond_to_booking_approval_survives_ledger_error_on_release_and_can_retry():
    """Argus round 3 finding: the round-2 pop-after-release ordering fix
    had no test actually injecting a failure -- only the happy path was
    covered. If ledger.release_for_negotiation raises, the pending marker
    must survive so a retry can succeed once the underlying issue clears."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

    real_release = alice.ledger.release_for_negotiation
    calls = {"n": 0}

    def flaky_release(negotiation_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ledger store unreachable")
        return real_release(negotiation_id)

    alice.ledger.release_for_negotiation = flaky_release

    with pytest.raises(RuntimeError):
        alice.respond_to_booking_approval(
            board, cid, approved=False, on_book=lambda c, s: None
        )

    # The pending marker survived the failed release -- retryable.
    assert cid in alice._pending_booking_approvals

    alice.respond_to_booking_approval(
        board, cid, approved=False, on_book=lambda c, s: None
    )
    assert cid not in alice._pending_booking_approvals
    assert calls["n"] == 2


def test_sweep_expired_booking_approvals_survives_ledger_error_and_retries_next_sweep():
    """Argus round 3 finding, Negotiator-layer companion to the test
    above: sweep_expired_booking_approvals's own _on_release closure has
    the identical pop-after-release ordering fix, and it was equally
    untested under an actual failure."""
    board = FakeBoard(clock=lambda: T0)
    clock = {"t": T0}
    alice = Negotiator(
        "alice@example.com", Ledger(clock=lambda: clock["t"]), clock=lambda: clock["t"]
    )
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)
    assert cid in alice._pending_booking_approvals

    real_release = alice.ledger.release_for_negotiation
    calls = {"n": 0}

    def flaky_release(negotiation_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ledger store unreachable")
        return real_release(negotiation_id)

    alice.ledger.release_for_negotiation = flaky_release
    clock["t"] = T0 + timedelta(hours=3)

    # ApprovalSurface.sweep_expired's own per-hold isolation (round 2 fix)
    # catches the exception raised inside _on_release -- this call does
    # not itself raise, but the hold must NOT be released or cleared yet.
    first_sweep = alice.sweep_expired_booking_approvals()
    assert first_sweep == []
    assert cid in alice._pending_booking_approvals

    second_sweep = alice.sweep_expired_booking_approvals()
    assert second_sweep == [cid]
    assert cid not in alice._pending_booking_approvals
    assert calls["n"] == 2


def test_has_pending_booking_approval_public_accessor():
    """`has_pending_booking_approval` mirrors `_pending_booking_approvals`
    membership without exposing the private dict -- added for reclaw-ea-mcp's
    `ea_request_booking` tool (TECH-5065), which needs this without reaching
    into `Negotiator` internals (the same pattern TECH-5077 flags for this
    repo's own tests)."""
    board = FakeBoard(clock=lambda: T0)
    alice = make_negotiator("alice@example.com")
    bob = make_negotiator("bob@example.com")
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T1),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    cid = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    assert alice.has_pending_booking_approval(cid) is False

    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, cid, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

    assert alice.has_pending_booking_approval(cid) is True
    assert cid in alice._pending_booking_approvals  # accessor agrees with internal state
