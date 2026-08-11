from datetime import UTC, datetime, timedelta

import pytest
from scheduler_mcp.autonomy.schema import Decision, GateResult

from reclaw_ea.approvals import (
    ApprovalStatus,
    ApprovalSurface,
    InvalidApprovalStateError,
    NotificationTier,
    route_notification,
)

T0 = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

ASK_FIRST_RESULT = GateResult(
    decision=Decision.ASK_FIRST, reason="needs a human", permanently_gated=False
)
CANNOT_DETERMINE_RESULT = GateResult(
    decision=Decision.CANNOT_DETERMINE, reason="lookup failed", permanently_gated=False
)
ACT_RESULT = GateResult(
    decision=Decision.ACT, reason="cleared threshold", permanently_gated=False
)


def make_surface(now=T0):
    clock = {"t": now}
    ids = iter(f"approval-{i}" for i in range(1, 1000))
    surface = ApprovalSurface(clock=lambda: clock["t"], id_factory=lambda: next(ids))
    return surface, clock


def test_route_notification_sends_ask_first_and_cannot_determine_to_ask_first():
    assert route_notification(ASK_FIRST_RESULT) is NotificationTier.ASK_FIRST
    assert route_notification(CANNOT_DETERMINE_RESULT) is NotificationTier.ASK_FIRST


def test_route_notification_act_defaults_to_do_and_tell():
    assert route_notification(ACT_RESULT) is NotificationTier.DO_AND_TELL


def test_route_notification_act_can_be_silent():
    assert (
        route_notification(ACT_RESULT, tell_on_act=False)
        is NotificationTier.DO_SILENTLY
    )


def test_request_opens_a_pending_hold_with_ttl():
    surface, _ = make_surface()
    hold = surface.request(
        owner="alice",
        resume_token="neg-1",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    assert hold.status is ApprovalStatus.PENDING
    assert hold.expires_at == T0 + timedelta(hours=2)
    assert hold.resume_token == "neg-1"
    assert hold.tier is NotificationTier.ASK_FIRST


def test_approve_calls_on_advance_exactly_once():
    """TECH-4961 approve-must-advance: approving a hold must move the
    thing it was gating forward, or it's orphaned."""
    surface, _ = make_surface()
    hold = surface.request(
        owner="alice", resume_token="neg-1", gate_result=ASK_FIRST_RESULT
    )
    calls = []
    resolved = surface.respond(
        hold.id, approved=True, on_advance=lambda h: calls.append(h.id)
    )
    assert resolved.status is ApprovalStatus.APPROVED
    assert calls == [hold.id]


def test_reject_calls_on_release_not_on_advance():
    surface, _ = make_surface()
    hold = surface.request(
        owner="alice", resume_token="neg-1", gate_result=ASK_FIRST_RESULT
    )
    advanced = []
    released = []
    resolved = surface.respond(
        hold.id,
        approved=False,
        on_advance=lambda h: advanced.append(h.id),
        on_release=lambda h: released.append(h.id),
    )
    assert resolved.status is ApprovalStatus.REJECTED
    assert advanced == []
    assert released == [hold.id]


def test_responding_twice_raises_rather_than_double_firing_callbacks():
    surface, _ = make_surface()
    hold = surface.request(
        owner="alice", resume_token="neg-1", gate_result=ASK_FIRST_RESULT
    )
    calls = []
    surface.respond(hold.id, approved=True, on_advance=lambda h: calls.append(h.id))
    with pytest.raises(InvalidApprovalStateError):
        surface.respond(hold.id, approved=True, on_advance=lambda h: calls.append(h.id))
    assert calls == [hold.id]  # not called twice


def test_responding_to_unknown_id_raises():
    surface, _ = make_surface()
    with pytest.raises(InvalidApprovalStateError):
        surface.respond("nope", approved=True, on_advance=lambda h: None)


def test_sweep_expired_releases_and_marks_expired():
    """TECH-4961 expire-must-release: a hold that times out unactioned
    must release whatever it was blocking, or it's stranded the same way
    an orphaned approve would strand it."""
    surface, clock = make_surface()
    hold = surface.request(
        owner="alice",
        resume_token="neg-1",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    clock["t"] = T0 + timedelta(hours=3)
    released = []
    result = surface.sweep_expired(on_release=lambda h: released.append(h.id))
    assert [h.id for h in result] == [hold.id]
    assert released == [hold.id]
    assert result[0].status is ApprovalStatus.EXPIRED


def test_sweep_expired_is_idempotent_never_double_releases():
    surface, clock = make_surface()
    hold = surface.request(
        owner="alice",
        resume_token="neg-1",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    clock["t"] = T0 + timedelta(hours=3)
    released = []
    surface.sweep_expired(on_release=lambda h: released.append(h.id))
    surface.sweep_expired(on_release=lambda h: released.append(h.id))
    assert released == [hold.id]  # only once, second sweep finds nothing pending


def test_sweep_expired_leaves_unexpired_holds_alone():
    surface, clock = make_surface()
    surface.request(
        owner="alice",
        resume_token="neg-1",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    clock["t"] = T0 + timedelta(minutes=30)
    released = []
    result = surface.sweep_expired(on_release=lambda h: released.append(h.id))
    assert result == []
    assert released == []


def test_expiry_race_a_late_response_after_sweep_is_rejected_not_double_resolved():
    """A hold can't be both expired by the sweep and later approved by a
    slow human click — whichever resolution happens first wins, and the
    loser gets a clear error rather than silently reprocessing."""
    surface, clock = make_surface()
    hold = surface.request(
        owner="alice",
        resume_token="neg-1",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    clock["t"] = T0 + timedelta(hours=3)
    surface.sweep_expired(on_release=lambda h: None)
    with pytest.raises(InvalidApprovalStateError):
        surface.respond(hold.id, approved=True, on_advance=lambda h: None)


def test_respond_callback_raising_leaves_hold_pending_and_retryable():
    """Argus round 2 finding: callback-before-persist ordering (fixed in
    round 1) was never actually tested end-to-end. If on_advance raises,
    the hold must stay PENDING -- not APPROVED with the advance never
    having happened -- so a caller can call respond() again once whatever
    failed is fixed."""
    surface, _ = make_surface()
    hold = surface.request(
        owner="alice", resume_token="neg-1", gate_result=ASK_FIRST_RESULT
    )

    def failing_on_advance(_hold):
        raise RuntimeError("downstream action failed")

    with pytest.raises(RuntimeError):
        surface.respond(hold.id, approved=True, on_advance=failing_on_advance)

    # Argus round 3 finding: proving the hold stayed PENDING through the
    # public API (a second respond() succeeding) rather than reaching into
    # `surface._store` directly -- the retry below already IS that proof.
    calls = []
    resolved = surface.respond(
        hold.id, approved=True, on_advance=lambda h: calls.append(h.id)
    )
    assert calls == [hold.id]
    assert resolved.status is ApprovalStatus.APPROVED


def test_sweep_expired_isolates_one_holds_failure_from_the_rest():
    """Argus round 2 finding: one hold's on_release raising must not
    abort the whole sweep batch -- especially now that Negotiator.react()
    calls this every tick, where an unhandled exception would wedge every
    conversation for that identity, not just the one bad hold."""
    surface, clock = make_surface()
    good_hold = surface.request(
        owner="alice",
        resume_token="neg-good",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    bad_hold = surface.request(
        owner="alice",
        resume_token="neg-bad",
        gate_result=ASK_FIRST_RESULT,
        ttl=timedelta(hours=2),
    )
    clock["t"] = T0 + timedelta(hours=3)

    def flaky_on_release(hold):
        if hold.resume_token == "neg-bad":
            raise RuntimeError("ledger store unreachable")

    released = surface.sweep_expired(on_release=flaky_on_release)

    # Argus round 3 finding: set-based comparison, not an ordered list --
    # the outcome here doesn't actually depend on which hold
    # pending_expired_before returns first (bad_hold always fails and is
    # excluded regardless of order), but asserting via a set rather than a
    # list makes that independence explicit rather than incidental.
    assert {h.id for h in released} == {good_hold.id}
    # good_hold is no longer PENDING now -- proven through the public API:
    # responding to it raises, since respond() only accepts a PENDING hold.
    # (Argus round 4 finding: this proves NOT-PENDING, not specifically
    # EXPIRED -- a sweep bug that set some other terminal status would also
    # make this assertion pass. Low regression risk in practice since
    # sweep_expired only ever writes EXPIRED, but the comment shouldn't
    # overclaim what's actually being demonstrated.)
    with pytest.raises(InvalidApprovalStateError):
        surface.respond(good_hold.id, approved=True, on_advance=lambda h: None)
    # bad_hold stays PENDING-and-expired, not lost -- proven by the retry
    # below actually picking it up (pending_expired_before only selects
    # PENDING rows, so it wouldn't be retried at all if it had been lost).

    def succeeding_on_release(hold):
        return None

    retried = surface.sweep_expired(on_release=succeeding_on_release)
    assert {h.id for h in retried} == {bad_hold.id}
    with pytest.raises(InvalidApprovalStateError):
        surface.respond(bad_hold.id, approved=True, on_advance=lambda h: None)
