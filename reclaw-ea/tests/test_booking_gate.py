from datetime import UTC, datetime, timedelta

import pytest
from scheduler_mcp.autonomy.schema import ConfidenceInputs, Decision
from scheduler_mcp.negotiation.rounds import NegotiationOutcome
from scheduler_mcp.negotiation.schema import CandidateSlot
from scheduler_mcp.rules import Situation

from reclaw_ea.booking_gate import (
    MIN_APPROVALS_FOR_BOOKING_AUTONOMY,
    evaluate_booking_gate,
)
from reclaw_ea.fake_board import FakeBoard
from reclaw_ea.ledger import Ledger
from reclaw_ea.orchestrator import BOOKING_ACTION_KEY, Negotiator
from reclaw_ea.outcomes import InMemoryOutcomeStore, record_outcome
from reclaw_ea.scorer import SlotContext
from reclaw_ea.wire import AvailabilityRequest, Modality

T0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=30)


def slot_ctx(start, end):
    return SlotContext(start=start, end=end, situation=Situation())


def negotiate_to_completion(board, alice, bob):
    request = AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=3,
    )
    conversation_id = alice.open_negotiation(
        board, to_agent_identity="bob@example.com", request=request
    )
    bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])
    alice.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])
    bob.react(board, conversation_id, my_candidates=[slot_ctx(T0, T1)], rules=[])
    return conversation_id


class TestEvaluateBookingGate:
    def test_external_is_always_ask_first_and_permanently_gated(self):
        result = evaluate_booking_gate(
            is_external=True,
            confidence_inputs=ConfidenceInputs(
                approval_count=10_000, rejection_count=0
            ),
        )
        assert result.decision is Decision.ASK_FIRST
        assert result.permanently_gated is True

    def test_internal_fresh_neighborhood_starts_ask_first(self):
        result = evaluate_booking_gate(
            is_external=False, confidence_inputs=ConfidenceInputs()
        )
        assert result.decision is Decision.ASK_FIRST
        assert result.permanently_gated is False

    def test_internal_earns_act_after_threshold_approvals(self):
        result = evaluate_booking_gate(
            is_external=False,
            confidence_inputs=ConfidenceInputs(
                approval_count=MIN_APPROVALS_FOR_BOOKING_AUTONOMY, rejection_count=0
            ),
        )
        assert result.decision is Decision.ACT

    def test_any_rejection_forces_ask_first_regardless_of_approvals(self):
        result = evaluate_booking_gate(
            is_external=False,
            confidence_inputs=ConfidenceInputs(
                approval_count=10_000, rejection_count=1
            ),
        )
        assert result.decision is Decision.ASK_FIRST
        assert result.permanently_gated is False


class TestMaybeFinalizeGating:
    def test_fresh_owner_requires_approval_before_booking(self):
        board = FakeBoard(clock=lambda: T0)
        alice = Negotiator(
            "alice@example.com", Ledger(clock=lambda: T0), clock=lambda: T0
        )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)

        booked = []
        assert not alice.maybe_finalize(
            board, cid, on_book=lambda c, s: booked.append((c, s))
        )
        assert booked == []
        assert cid in alice._pending_booking_approvals

    def test_calling_maybe_finalize_again_while_pending_does_not_open_a_second_approval(
        self,
    ):
        board = FakeBoard(clock=lambda: T0)
        alice = Negotiator(
            "alice@example.com", Ledger(clock=lambda: T0), clock=lambda: T0
        )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)

        alice.maybe_finalize(board, cid, on_book=lambda c, s: None)
        first_approval_id = alice._pending_booking_approvals[cid]
        alice.maybe_finalize(board, cid, on_book=lambda c, s: None)
        assert alice._pending_booking_approvals[cid] == first_approval_id

    def test_approving_books_and_records_an_approval_outcome(self):
        board = FakeBoard(clock=lambda: T0)
        alice = Negotiator(
            "alice@example.com", Ledger(clock=lambda: T0), clock=lambda: T0
        )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)
        alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

        booked = []
        alice.respond_to_booking_approval(
            board, cid, approved=True, on_book=lambda c, s: booked.append((c, s))
        )
        assert len(booked) == 1
        assert alice.state_for(cid).outcome is NegotiationOutcome.BOOKED
        outcomes = alice.outcome_store.query(
            owner_identity="alice@example.com",
            action_type=BOOKING_ACTION_KEY,
            counterparty_class="internal",
        )
        assert len(outcomes) == 1
        assert outcomes[0].approved is True

    def test_rejecting_does_not_book_and_records_a_rejection(self):
        board = FakeBoard(clock=lambda: T0)
        alice = Negotiator(
            "alice@example.com", Ledger(clock=lambda: T0), clock=lambda: T0
        )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)
        alice.maybe_finalize(board, cid, on_book=lambda c, s: None)

        # _try_confirm already promoted alice's ledger hold to BOOKED
        # during the negotiation, before this later booking-gate approval
        # ever ran (docs/DESIGN.md §2.2 point 2: promotion happens at
        # confirm time). Confirm that precondition before asserting the fix.
        assert (
            alice.ledger.get(owner="alice@example.com", slot_start_utc=T0).state.value
            == "booked"
        )

        booked = []
        alice.respond_to_booking_approval(
            board, cid, approved=False, on_book=lambda c, s: booked.append((c, s))
        )
        assert booked == []
        assert alice.state_for(cid).outcome is NegotiationOutcome.IN_PROGRESS
        assert cid not in alice._pending_booking_approvals
        outcomes = alice.outcome_store.query(
            owner_identity="alice@example.com",
            action_type=BOOKING_ACTION_KEY,
            counterparty_class="internal",
        )
        assert len(outcomes) == 1
        assert outcomes[0].approved is False
        # Argus round 1 finding: rejection previously only popped the
        # pending marker, leaving the promoted BOOKED ledger hold as a
        # permanent, unreleasable block on the slot.
        assert alice.ledger.get(owner="alice@example.com", slot_start_utc=T0) is None

    def test_five_approvals_in_a_neighborhood_earns_immediate_booking(self):
        board = FakeBoard(clock=lambda: T0)
        outcome_store = InMemoryOutcomeStore()
        alice = Negotiator(
            "alice@example.com",
            Ledger(clock=lambda: T0),
            clock=lambda: T0,
            outcome_store=outcome_store,
        )
        for _ in range(MIN_APPROVALS_FOR_BOOKING_AUTONOMY):
            record_outcome(
                outcome_store,
                owner_identity="alice@example.com",
                action_type=BOOKING_ACTION_KEY,
                counterparty_class="internal",
                approved=True,
                now=T0,
            )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)

        booked = []
        result = alice.maybe_finalize(
            board, cid, on_book=lambda c, s: booked.append((c, s))
        )
        assert result is True
        assert len(booked) == 1
        assert cid not in alice._pending_booking_approvals

    def test_external_counterparty_always_requires_approval_even_with_track_record(
        self,
    ):
        board = FakeBoard(clock=lambda: T0)
        outcome_store = InMemoryOutcomeStore()
        for _ in range(50):
            record_outcome(
                outcome_store,
                owner_identity="alice@example.com",
                action_type=BOOKING_ACTION_KEY,
                counterparty_class="external",
                approved=True,
                now=T0,
            )
        alice = Negotiator(
            "alice@example.com",
            Ledger(clock=lambda: T0),
            clock=lambda: T0,
            outcome_store=outcome_store,
            is_external=lambda counterpart: True,
        )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)

        booked = []
        result = alice.maybe_finalize(
            board, cid, on_book=lambda c, s: booked.append((c, s))
        )
        assert result is False
        assert booked == []
        assert cid in alice._pending_booking_approvals

    def test_responding_without_a_pending_approval_raises(self):
        board = FakeBoard(clock=lambda: T0)
        alice = Negotiator(
            "alice@example.com", Ledger(clock=lambda: T0), clock=lambda: T0
        )
        bob = Negotiator("bob@example.com", Ledger(clock=lambda: T0), clock=lambda: T0)
        cid = negotiate_to_completion(board, alice, bob)
        with pytest.raises(ValueError):
            alice.respond_to_booking_approval(
                board, cid, approved=True, on_book=lambda c, s: None
            )
