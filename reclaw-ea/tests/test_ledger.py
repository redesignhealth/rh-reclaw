from datetime import UTC, datetime, timedelta

import pytest

from reclaw_ea.ledger import (
    ClaimVerdict,
    Ledger,
    LedgerError,
    LedgerStore,
    SlotKey,
    SlotState,
)

T0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=30)


def make_ledger(now=T0):
    clock = {"t": now}
    ledger = Ledger(clock=lambda: clock["t"])
    return ledger, clock


def test_fresh_claim_succeeds():
    ledger, _ = make_ledger()
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    assert result.ok
    assert result.reservation.state is SlotState.OFFERED
    assert result.reservation.expires_at == T0 + timedelta(hours=24)


def test_second_negotiation_is_blocked():
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-2",
        ttl=timedelta(hours=24),
    )
    assert result.verdict is ClaimVerdict.BLOCKED
    assert "neg-1" in result.detail


def test_same_negotiation_may_reclaim():
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=48),
    )
    assert result.ok
    assert result.reservation.expires_at == T0 + timedelta(hours=48)


def test_expired_offer_may_be_reclaimed_by_another_negotiation():
    ledger, clock = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    clock["t"] = T0 + timedelta(hours=2)  # past expiry
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-2",
        ttl=timedelta(hours=1),
    )
    assert result.ok
    assert result.reservation.negotiation_id == "neg-2"


def test_promote_moves_to_booked_with_no_expiry():
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    result = ledger.promote(owner="alice", slot_start_utc=T0, negotiation_id="neg-1")
    assert result.ok
    assert result.reservation.state is SlotState.BOOKED
    assert result.reservation.expires_at is None


def test_promote_by_wrong_negotiation_is_blocked():
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    result = ledger.promote(owner="alice", slot_start_utc=T0, negotiation_id="neg-2")
    assert result.verdict is ClaimVerdict.BLOCKED


def test_release_frees_the_slot_for_anyone():
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    ledger.release(owner="alice", slot_start_utc=T0, negotiation_id="neg-1")
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-2",
        ttl=timedelta(hours=24),
    )
    assert result.ok


def test_release_scoped_to_wrong_negotiation_is_a_noop():
    """Defensive: one negotiation can never release another's hold."""
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    ledger.release(owner="alice", slot_start_utc=T0, negotiation_id="neg-2")
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-2",
        ttl=timedelta(hours=24),
    )
    assert result.verdict is ClaimVerdict.BLOCKED


def test_release_for_negotiation_frees_all_its_holds():
    """§10 failure-mode row: release on every terminal outcome + decline,
    not only explicit abandonment (maiea TECH-4098 Gap-4)."""
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    ledger.claim(
        owner="alice",
        slot_start_utc=T1,
        slot_end_utc=T1 + timedelta(minutes=30),
        negotiation_id="neg-1",
        ttl=timedelta(hours=24),
    )
    count = ledger.release_for_negotiation("neg-1")
    assert count == 2
    assert ledger.get(owner="alice", slot_start_utc=T0) is None
    assert ledger.get(owner="alice", slot_start_utc=T1) is None


def test_reap_expired_actively_sweeps_without_a_claim_happening():
    """Gap 2: maiea only swept lazily inside claim_slot; an abandoned
    negotiation's holds sat forever if nobody else claimed that exact slot."""
    ledger, clock = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    clock["t"] = T0 + timedelta(hours=2)
    reaped = ledger.reap_expired()
    assert reaped == 1
    assert ledger.get(owner="alice", slot_start_utc=T0) is None


def test_reap_expired_leaves_booked_holds_alone():
    ledger, clock = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    ledger.promote(owner="alice", slot_start_utc=T0, negotiation_id="neg-1")
    clock["t"] = T0 + timedelta(days=365)
    reaped = ledger.reap_expired()
    assert reaped == 0
    assert ledger.get(owner="alice", slot_start_utc=T0) is not None


def test_reconcile_booked_releases_holds_whose_event_is_gone():
    """Gap 3: booked holds had no reaper of any kind in maiea."""
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    ledger.promote(owner="alice", slot_start_utc=T0, negotiation_id="neg-1")
    released = ledger.reconcile_booked(still_real=lambda reservation: False)
    assert released == 1
    assert ledger.get(owner="alice", slot_start_utc=T0) is None


def test_reconcile_booked_keeps_holds_whose_event_is_confirmed_real():
    ledger, _ = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    ledger.promote(owner="alice", slot_start_utc=T0, negotiation_id="neg-1")
    released = ledger.reconcile_booked(still_real=lambda reservation: True)
    assert released == 0
    assert ledger.get(owner="alice", slot_start_utc=T0) is not None


def test_claim_fails_closed_on_store_error():
    """Gap 1, the load-bearing fix: maiea's claim_slot failed OPEN
    ("never let a ledger hiccup block proposing times") and treated an
    infra error as a successful claim. Here it must resolve to
    ClaimVerdict.ERROR, which callers must treat as not-claimed."""

    class BrokenStore(LedgerStore):
        def get(self, key):
            raise LedgerError("connection refused")

        def put(self, reservation):
            raise LedgerError("connection refused")

        def delete(self, key):
            raise LedgerError("connection refused")

        def all_for_negotiation(self, negotiation_id):
            return []

        def all_offered_before(self, cutoff):
            return []

        def all_booked(self):
            return []

    ledger = Ledger(store=BrokenStore(), clock=lambda: T0)
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    assert result.verdict is ClaimVerdict.ERROR
    assert not result.ok


def test_tier_4_offer_without_hold_does_not_block_another_negotiation():
    """A no-hold offer (ttl=None, Tier 4) must not starve a later,
    ledger-backed claim on the same slot — it was never persisted."""
    ledger, _ = make_ledger()
    result = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=None,
    )
    assert result.ok
    assert ledger.get(owner="alice", slot_start_utc=T0) is None  # nothing persisted
    second = ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-2",
        ttl=timedelta(hours=24),
    )
    assert second.ok


def test_slot_key_requires_timezone_aware_datetime():
    with pytest.raises(ValueError):
        SlotKey("alice", datetime(2026, 8, 12, 14, 0))  # naive  # noqa: DTZ001


def test_promote_on_an_expired_offer_is_blocked_not_claimed():
    """Argus round 1 finding: promote() used to call the store directly,
    bypassing the expiry check Ledger.get() applies -- an offered hold
    whose TTL had already elapsed could still be found and promoted,
    granting permanent exclusivity post-hoc for a slot that had already
    lost it. Expected: BLOCKED. (Previously: CLAIMED with a permanent
    booked hold.)"""
    ledger, clock = make_ledger()
    ledger.claim(
        owner="alice",
        slot_start_utc=T0,
        slot_end_utc=T1,
        negotiation_id="neg-1",
        ttl=timedelta(hours=1),
    )
    clock["t"] = T0 + timedelta(hours=2)  # past expiry
    result = ledger.promote(owner="alice", slot_start_utc=T0, negotiation_id="neg-1")
    assert result.verdict is ClaimVerdict.BLOCKED
    assert ledger.get(owner="alice", slot_start_utc=T0) is None
