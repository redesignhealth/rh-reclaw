"""Argus round 1 finding: wire.py's Pydantic constraints (max_length=10,
ge=1/le=4, gt=0/le=1440, ge=1) were never tested against out-of-bounds
input -- ValidationError was never asserted anywhere in the suite."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from scheduler_mcp.negotiation.schema import CandidateSlot

from reclaw_ea.wire import (
    MAX_SLOTS_PER_MESSAGE,
    AvailabilityRequest,
    AvailabilityResponse,
    Confirm,
    CounterProposal,
    Decline,
    DeclineReason,
    Modality,
    NeedsClarification,
    ScoredSlot,
)

T0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=30)


def make_scored_slot(start=T0, end=T1, preference=0.5) -> ScoredSlot:
    return ScoredSlot(slot=CandidateSlot(start=start, end=end), preference=preference)


def test_scored_slot_preference_below_zero_rejected():
    with pytest.raises(ValidationError):
        ScoredSlot(slot=CandidateSlot(start=T0, end=T1), preference=-0.01)


def test_scored_slot_preference_above_one_rejected():
    with pytest.raises(ValidationError):
        ScoredSlot(slot=CandidateSlot(start=T0, end=T1), preference=1.01)


def test_scored_slot_preference_boundary_values_accepted():
    ScoredSlot(slot=CandidateSlot(start=T0, end=T1), preference=0.0)
    ScoredSlot(slot=CandidateSlot(start=T0, end=T1), preference=1.0)


def test_availability_request_duration_zero_rejected():
    with pytest.raises(ValidationError):
        AvailabilityRequest(
            window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
            duration_minutes=0,
            modality=Modality.VIDEO,
            priority=1,
        )


def test_availability_request_duration_over_24h_rejected():
    with pytest.raises(ValidationError):
        AvailabilityRequest(
            window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
            duration_minutes=24 * 60 + 1,
            modality=Modality.VIDEO,
            priority=1,
        )


def test_availability_request_duration_boundary_1440_accepted():
    AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
        duration_minutes=24 * 60,
        modality=Modality.VIDEO,
        priority=1,
    )


@pytest.mark.parametrize("priority", [0, 5, -1])
def test_availability_request_priority_out_of_range_rejected(priority):
    with pytest.raises(ValidationError):
        AvailabilityRequest(
            window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
            duration_minutes=30,
            modality=Modality.VIDEO,
            priority=priority,
        )


@pytest.mark.parametrize("priority", [1, 2, 3, 4])
def test_availability_request_priority_boundary_values_accepted(priority):
    AvailabilityRequest(
        window=CandidateSlot(start=T0, end=T0 + timedelta(hours=1)),
        duration_minutes=30,
        modality=Modality.VIDEO,
        priority=priority,
    )


def test_needs_clarification_seq_zero_rejected():
    with pytest.raises(ValidationError):
        NeedsClarification(about_seq=0)


def test_needs_clarification_seq_one_accepted():
    NeedsClarification(about_seq=1)


def test_decline_requires_a_reason():
    with pytest.raises(ValidationError):
        Decline()  # type: ignore[call-arg]
    Decline(reason=DeclineReason.OWNER_DECLINED)


@pytest.mark.parametrize("payload_cls", [AvailabilityResponse, CounterProposal])
def test_slots_over_max_length_rejected(payload_cls):
    too_many = tuple(
        make_scored_slot(
            start=T0 + timedelta(minutes=30 * i),
            end=T0 + timedelta(minutes=30 * (i + 1)),
        )
        for i in range(MAX_SLOTS_PER_MESSAGE + 1)
    )
    with pytest.raises(ValidationError):
        payload_cls(slots=too_many)


@pytest.mark.parametrize("payload_cls", [AvailabilityResponse, CounterProposal])
def test_slots_at_max_length_accepted(payload_cls):
    exactly_max = tuple(
        make_scored_slot(
            start=T0 + timedelta(minutes=30 * i),
            end=T0 + timedelta(minutes=30 * (i + 1)),
        )
        for i in range(MAX_SLOTS_PER_MESSAGE)
    )
    payload_cls(slots=exactly_max)


@pytest.mark.parametrize("payload_cls", [AvailabilityResponse, CounterProposal])
def test_empty_slots_without_none_available_rejected(payload_cls):
    """Argus round 1 finding: previously valid Pydantic but semantically
    ambiguous -- "no opinion yet" vs. "genuinely nothing available"."""
    with pytest.raises(ValidationError):
        payload_cls(slots=(), none_available=False)


@pytest.mark.parametrize("payload_cls", [AvailabilityResponse, CounterProposal])
def test_empty_slots_with_none_available_true_accepted(payload_cls):
    payload_cls(
        slots=(),
        none_available=True,
        reason=DeclineReason.NO_AVAILABILITY_WITHIN_CONSTRAINTS,
    )


@pytest.mark.parametrize("payload_cls", [AvailabilityResponse, CounterProposal])
def test_nonempty_slots_with_none_available_true_accepted(payload_cls):
    """The validator only requires ONE of the two to hold, not mutual
    exclusion -- a payload with both slots and none_available=True is
    unusual but not asserted to be invalid by this design."""
    payload_cls(slots=(make_scored_slot(),), none_available=True)


def test_confirm_carries_no_completion_semantics_of_its_own():
    Confirm(slot=CandidateSlot(start=T0, end=T1))
