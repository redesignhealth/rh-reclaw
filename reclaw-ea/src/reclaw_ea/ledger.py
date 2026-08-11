"""Slot reservation ledger — the primitive that prevents double-booking.

Design: `docs/DESIGN.md` §4. Ports maiea's `tools/memory/slots.py`
`(user_id, slot_start_utc)` claim/release/promote shape, with the three
gaps that doc calls out fixed:

  1. **Claims fail closed.** maiea's `claim_slot` swallowed its own
     exceptions and returned `True` ("never let a ledger hiccup block
     proposing times") — i.e. on an infra error, maiea *assumed the claim
     succeeded* and treated the slot as safely held, relying on the
     freebusy-at-book check as the only backstop. Here the ledger is the
     authoritative arbiter (`docs/DESIGN.md` §4: "the ledger is
     authoritative and freebusy-at-book is defense-in-depth") — an error
     talking to the store must resolve to "not ours," never "ours."
  2. **Active expiry reaper.** maiea only swept expired `offered` rows
     lazily, inside `claim_slot` itself — a slot nobody re-claims after it
     expires sits in the table forever. `reap_expired()` is a standalone
     entry point a scheduler can call on its own cadence.
  3. **Booked-hold reconciliation.** maiea's `booked` holds had no TTL and
     no reaper at all — if the calendar event they represent is later
     cancelled or moved outside the EA's own booking path, the ledger row
     never notices. `reconcile_booked()` takes a caller-supplied predicate
     over "is this booking still real" and releases the ones that aren't.

Hold TTLs are tier-based (`tiers.py`), not maiea's flat 72h. Release covers
every terminal negotiation outcome plus decline (docs/DESIGN.md's
generalization of maiea's TECH-4098 Gap-4, which only handled explicit
abandonment).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SlotState(StrEnum):
    OFFERED = "offered"
    BOOKED = "booked"


@dataclass(frozen=True)
class SlotKey:
    """A slot is identified by its owner and start time. Two negotiations
    proposing an overlapping-but-not-identical window are a scorer/rules
    concern (`docs/DESIGN.md` §3), not a ledger one — the ledger only
    arbitrates exact-start collisions, mirroring maiea's own PK shape."""

    owner: str
    slot_start_utc: datetime

    def __post_init__(self) -> None:
        if self.slot_start_utc.tzinfo is None:
            raise ValueError("slot_start_utc must be timezone-aware")


@dataclass(frozen=True)
class Reservation:
    owner: str
    slot_start_utc: datetime
    slot_end_utc: datetime
    negotiation_id: str
    state: SlotState
    reserved_at: datetime
    expires_at: datetime | None  # None: never auto-expires (booked, or Tier 4 no-hold)


class ClaimVerdict(StrEnum):
    CLAIMED = "claimed"  # fresh claim, or a re-claim by the same negotiation
    BLOCKED = "blocked"  # another negotiation holds this slot (offered or booked)
    ERROR = "error"  # store failure — resolved as not-ours, never as claimed


@dataclass(frozen=True)
class ClaimResult:
    verdict: ClaimVerdict
    reservation: Reservation | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is ClaimVerdict.CLAIMED


class LedgerStore(ABC):
    """Storage seam. `InMemoryLedgerStore` below is the default; a
    Postgres-backed implementation is the deploy-time swap (same "TODO:
    back with Postgres" seam as every other `rh-scheduler-mcp` store —
    kept here as an ABC rather than a module-level dict so that swap never
    touches `Ledger`'s business logic).

    **Known gap, flagged for the Postgres implementor (Argus round 1
    finding, tracked as TECH-5068):** `Ledger.claim()`'s atomicity claim
    (`docs/DESIGN.md` §4: "Atomic claim (INSERT … ON CONFLICT)") is only
    true for `InMemoryLedgerStore` because Python's GIL serializes the
    synchronous `get`-then-`put` sequence within one process. This ABC's
    `get`/`put` primitives cannot express a true atomic claim across
    concurrent processes/connections -- a Postgres-backed `LedgerStore`
    needs a `claim_if_absent(key, reservation) -> Reservation | None`
    (or equivalent single-statement `INSERT ... ON CONFLICT DO NOTHING
    RETURNING`) method added to this ABC, with `Ledger.claim()` updated to
    use it, before this is safe under real concurrency."""

    @abstractmethod
    def get(self, key: SlotKey) -> Reservation | None: ...

    @abstractmethod
    def put(self, reservation: Reservation) -> None: ...

    @abstractmethod
    def delete(self, key: SlotKey) -> None: ...

    @abstractmethod
    def all_for_negotiation(self, negotiation_id: str) -> list[Reservation]: ...

    @abstractmethod
    def all_offered_before(self, cutoff: datetime) -> list[Reservation]: ...

    @abstractmethod
    def all_booked(self) -> list[Reservation]: ...


class InMemoryLedgerStore(LedgerStore):
    """Process-local store. Fine for a single EA agent process; the
    multi-container split maiea needed (scanner + poller sharing SQLite)
    does not apply here since this design has exactly one owner of
    negotiation state per EA (`docs/DESIGN.md` §4, "no second path")."""

    def __init__(self) -> None:
        self._rows: dict[SlotKey, Reservation] = {}

    def get(self, key: SlotKey) -> Reservation | None:
        return self._rows.get(key)

    def put(self, reservation: Reservation) -> None:
        self._rows[SlotKey(reservation.owner, reservation.slot_start_utc)] = reservation

    def delete(self, key: SlotKey) -> None:
        self._rows.pop(key, None)

    def all_for_negotiation(self, negotiation_id: str) -> list[Reservation]:
        return [r for r in self._rows.values() if r.negotiation_id == negotiation_id]

    def all_offered_before(self, cutoff: datetime) -> list[Reservation]:
        return [
            r
            for r in self._rows.values()
            if r.state is SlotState.OFFERED
            and r.expires_at is not None
            and r.expires_at < cutoff
        ]

    def all_booked(self) -> list[Reservation]:
        return [r for r in self._rows.values() if r.state is SlotState.BOOKED]


class LedgerError(Exception):
    """Raised by a `LedgerStore` implementation to signal an infra failure.
    `Ledger` catches exactly this (not bare `Exception`) so a genuine
    programming bug elsewhere doesn't get silently absorbed into
    `ClaimVerdict.ERROR` — only the store's own declared failure mode does."""


class Ledger:
    def __init__(self, store: LedgerStore | None = None, clock=_utcnow) -> None:
        self._store = store or InMemoryLedgerStore()
        self._clock = clock

    def claim(
        self,
        *,
        owner: str,
        slot_start_utc: datetime,
        slot_end_utc: datetime,
        negotiation_id: str,
        state: SlotState = SlotState.OFFERED,
        ttl: timedelta | None = None,
    ) -> ClaimResult:
        """Reserve a slot. `ttl=None` with `state=OFFERED` is legal (Tier 4,
        per `tiers.hold_ttl_for`) — it means "offer this time without a
        hold," and such a claim is immediately superseded by any other
        negotiation's claim on the same slot rather than blocking it. A
        `None` ttl with `state=BOOKED` means "never auto-expires," matching
        maiea's `promote_slot` semantics.

        Fail-closed contract: any `LedgerError` from the store resolves to
        `ClaimVerdict.ERROR`, which callers MUST treat as "not claimed" —
        see module docstring, gap 1.
        """

        key = SlotKey(owner, slot_start_utc)
        now = self._clock()
        try:
            existing = self._store.get(key)
            if existing is not None and not self._is_expired(existing, now):
                if existing.negotiation_id == negotiation_id:
                    # Same negotiation re-claiming (e.g. re-offering, or
                    # promoting) — allowed, mirrors maiea's
                    # UPDATE...WHERE thread_id=? re-claim path.
                    pass
                else:
                    return ClaimResult(
                        verdict=ClaimVerdict.BLOCKED,
                        detail=f"held by negotiation {existing.negotiation_id!r} in state {existing.state}",
                    )

            no_hold = state is SlotState.OFFERED and ttl is None
            expires_at = None if (state is SlotState.BOOKED or no_hold) else now + ttl

            reservation = Reservation(
                owner=owner,
                slot_start_utc=slot_start_utc,
                slot_end_utc=slot_end_utc,
                negotiation_id=negotiation_id,
                state=state,
                reserved_at=now,
                expires_at=expires_at,
            )
            if no_hold:
                # Deliberately not persisted: an unheld offer has nothing
                # for a later claimant to check against, so recording it
                # would just be dead state that never blocks anything and
                # never expires. Return CLAIMED so the caller's offer flow
                # is uniform regardless of tier.
                return ClaimResult(
                    verdict=ClaimVerdict.CLAIMED, reservation=reservation
                )

            self._store.put(reservation)
            return ClaimResult(verdict=ClaimVerdict.CLAIMED, reservation=reservation)
        except LedgerError as exc:
            return ClaimResult(verdict=ClaimVerdict.ERROR, detail=str(exc))

    def promote(
        self, *, owner: str, slot_start_utc: datetime, negotiation_id: str
    ) -> ClaimResult:
        """Move an `offered` hold to `booked` (no expiry) after a
        successful confirm (`docs/DESIGN.md` §2.2 point 2: post `confirm`
        only after re-checking freebusy and promoting the hold). Only the
        negotiation that holds the slot may promote it."""

        try:
            # Argus round 1 finding: this used to call `self._store.get(key)`
            # directly, bypassing the expiry check `Ledger.get()` applies --
            # an `offered` hold whose TTL had already elapsed could still be
            # found and promoted, granting permanent exclusivity post-hoc
            # for a slot that had already lost it. Route through the
            # public, expiry-aware `get()` instead.
            existing = self.get(owner=owner, slot_start_utc=slot_start_utc)
            if existing is None or existing.negotiation_id != negotiation_id:
                return ClaimResult(
                    verdict=ClaimVerdict.BLOCKED,
                    detail="no matching offered hold for this negotiation to promote",
                )
            promoted = replace(existing, state=SlotState.BOOKED, expires_at=None)
            self._store.put(promoted)
            return ClaimResult(verdict=ClaimVerdict.CLAIMED, reservation=promoted)
        except LedgerError as exc:
            return ClaimResult(verdict=ClaimVerdict.ERROR, detail=str(exc))

    def release(
        self, *, owner: str, slot_start_utc: datetime, negotiation_id: str | None = None
    ) -> None:
        """Free a slot. `negotiation_id`, when given, scopes the release so
        one negotiation can never release another's hold (maiea's own
        defensive comment on `release_slot`, kept).

        `LedgerError` propagation (Argus round 1 finding: this was
        undocumented and inconsistent across methods): a store failure here
        propagates RAW, unlike `claim()`/`promote()`, which catch it and
        return `ClaimVerdict.ERROR`. Deliberate, not an oversight: `release`
        returns `None`, so there is no error-carrying return value to
        collapse a failure into the way `ClaimResult.verdict` does for the
        claim path -- swallowing the exception here would silently leave a
        hold in place with no signal to the caller at all. Callers that
        call `release` from a cleanup path (e.g. `orchestrator.py`'s
        rollback after a suppressed `_post`) should expect this and decide
        for themselves whether to catch it."""

        key = SlotKey(owner, slot_start_utc)
        existing = self._store.get(key)
        if existing is None:
            return
        if negotiation_id is not None and existing.negotiation_id != negotiation_id:
            return
        self._store.delete(key)

    def release_for_negotiation(self, negotiation_id: str) -> int:
        """Release every hold a negotiation still owns. Call this on all
        four terminal negotiation outcomes and on decline — the design
        doc's generalization of maiea's TECH-4098 Gap-4, which only
        released on explicit thread abandonment and left the other
        terminal paths to leak until TTL. Returns the count released.

        `LedgerError` propagation: propagates raw, same rationale as
        `release()` above -- this method has no error-carrying return
        value either, and it is itself the rollback/cleanup path several
        callers (`orchestrator.py`) invoke from inside their own
        exception-adjacent branches; swallowing a failure here would leave
        those callers believing cleanup succeeded when it didn't."""

        rows = self._store.all_for_negotiation(negotiation_id)
        for row in rows:
            self._store.delete(SlotKey(row.owner, row.slot_start_utc))
        return len(rows)

    def reap_expired(self, *, now: datetime | None = None) -> int:
        """Actively sweep expired `offered` holds, independent of any
        claim happening to run. Gap 2 from the module docstring — maiea
        only swept lazily inside `claim_slot`, so a slot nobody re-claims
        after expiry sat in the table indefinitely. Intended to be called
        on a scheduler's own cadence (e.g. every few minutes), not only
        opportunistically. Returns the count reaped.

        `LedgerError` propagation: propagates raw. This is a periodic
        background sweep, not a request-scoped operation with a caller
        waiting on a specific outcome -- a scheduler invoking this on a
        cadence should let a store failure surface (and its own retry/
        alerting logic handle it) rather than have failures silently
        counted as "reaped 0" indefinitely."""

        cutoff = now or self._clock()
        expired = self._store.all_offered_before(cutoff)
        for row in expired:
            self._store.delete(SlotKey(row.owner, row.slot_start_utc))
        return len(expired)

    def reconcile_booked(self, *, still_real) -> int:
        """Release `booked` holds whose underlying calendar event is no
        longer real. `still_real(reservation) -> bool` is caller-supplied
        (a calendar lookup) so this module stays free of any calendar-API
        dependency — the reconciliation *policy* (what "still real" means,
        how often to check) lives with the caller; this module only owns
        "walk every booked row and release the ones that fail the check."
        Gap 3 from the module docstring: maiea's `booked` holds had no
        expiry and no reaper of any kind. Returns the count released.

        `LedgerError` propagation: propagates raw, same rationale as
        `reap_expired()` -- a periodic sweep, not a request-scoped call."""

        released = 0
        for row in self._store.all_booked():
            if not still_real(row):
                self._store.delete(SlotKey(row.owner, row.slot_start_utc))
                released += 1
        return released

    def get(self, *, owner: str, slot_start_utc: datetime) -> Reservation | None:
        """Expiry-aware read -- `promote()` routes through this rather than
        the raw store, specifically so an expired `offered` row can never
        be found and promoted (Argus round 1 finding, fixed above).
        `LedgerError` propagation: propagates raw, same rationale as
        `release()` -- no error-carrying return value to collapse a
        failure into."""

        existing = self._store.get(SlotKey(owner, slot_start_utc))
        if existing is None or self._is_expired(existing, self._clock()):
            return None
        return existing

    @staticmethod
    def _is_expired(reservation: Reservation, now: datetime) -> bool:
        return reservation.expires_at is not None and reservation.expires_at < now
