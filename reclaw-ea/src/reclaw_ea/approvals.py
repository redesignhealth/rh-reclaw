"""The approval surface: what happens to a human after the autonomy gate
says `ask_first`.

Design: `docs/DESIGN.md` §5. `scheduler_mcp.autonomy.gate.evaluate_gate`
deliberately stops at returning a `Decision` — its own module docstring is
explicit that "the actual owner-response-outcome record ... does not exist
anywhere in this codebase yet" (TECH-4996) and that whoever builds it "MUST
treat TECH-4961's invariant as a hard requirement from day one": approving
a pending hold must advance whatever it was gating, and the hold expiring
unactioned must release it. Both directions were independently real maiea
bugs (`_rate_limiter.py`, "Architect Rulings" D17-D19) — this module is
where those two invariants live for the EA side, plus the three-tier
notification routing from `docs/DESIGN.md` §5 ("do silently / do and
tell / ask first").

The owner-response *feedback loop* into the confidence engine
(`docs/DESIGN.md` §5, §8; TECH-4996 upstream) is intentionally NOT built
here — that's TECH-5057, layered on top of `respond()`'s return value.
This module's job stops at "the hold resolved correctly and something got
notified," which is exactly the scope TECH-4961 requires and no more.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .autonomy import Decision, GateResult

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


DEFAULT_APPROVAL_TTL = timedelta(hours=2)


class NotificationTier(StrEnum):
    """`docs/DESIGN.md` §5: "do silently / do and tell / ask first" — the
    three-tier routing that keeps the middle tier from becoming noise
    (v3's stated risk: "notification fatigue turning the middle tier into
    noise"). Calibration from reaction feedback is future work (`docs/
    DESIGN.md` §8's weekly digest is the legibility mechanism for that);
    `route_notification` below implements the uncalibrated default only."""

    DO_SILENTLY = "do_silently"
    DO_AND_TELL = "do_and_tell"
    ASK_FIRST = "ask_first"


def route_notification(
    result: GateResult, *, tell_on_act: bool = True
) -> NotificationTier:
    """Uncalibrated default routing from a gate result to a notification
    tier. `ask_first`/`cannot_determine` always route to `ASK_FIRST` — the
    gate has already decided a human must weigh in, this function does not
    second-guess that. An `act` decision routes to `DO_AND_TELL` by
    default (`tell_on_act=True`) rather than `DO_SILENTLY`, since a brand
    new (owner, action-type, counterparty-class) neighborhood earning its
    first autonomous decisions is exactly when the owner most wants
    visibility — `tell_on_act=False` is the calibrated-down state a future
    reaction-feedback loop (TECH-5057-adjacent, not this module) would
    move a neighborhood toward over time, not something this function
    decides on its own."""

    if result.decision is Decision.ACT:
        return (
            NotificationTier.DO_AND_TELL
            if tell_on_act
            else NotificationTier.DO_SILENTLY
        )
    return NotificationTier.ASK_FIRST


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class InvalidApprovalStateError(Exception):
    """Raised when responding to or expiring a hold that isn't PENDING.
    Never silently no-ops — a caller racing a response against a sweep (or
    double-submitting a Slack button click) needs to know which one won,
    not have the second attempt silently vanish."""


@dataclass(frozen=True)
class ApprovalHold:
    """One pending human decision. `resume_token` is an opaque identifier
    the caller defines (a negotiation_id, a thread id, whatever the thing
    being gated actually is) — this module never inspects it, only passes
    it back to the `on_advance`/`on_release` callbacks so the caller can
    resume the right piece of state. `docs/DESIGN.md` §5: "Button payloads
    carry resume-state identifiers, never draft bodies" — `resume_token`
    is exactly that identifier, and this dataclass carries no draft
    content of its own for the same reason."""

    id: str
    owner: str
    resume_token: str
    reason: str
    tier: NotificationTier
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus


class ApprovalStore(ABC):
    """Storage seam, same pattern as `ledger.LedgerStore` — an in-memory
    default now, a Postgres-backed implementation at deploy time."""

    @abstractmethod
    def get(self, approval_id: str) -> ApprovalHold | None: ...

    @abstractmethod
    def put(self, hold: ApprovalHold) -> None: ...

    @abstractmethod
    def pending_expired_before(self, cutoff: datetime) -> list[ApprovalHold]: ...


class InMemoryApprovalStore(ApprovalStore):
    def __init__(self) -> None:
        self._rows: dict[str, ApprovalHold] = {}

    def get(self, approval_id: str) -> ApprovalHold | None:
        return self._rows.get(approval_id)

    def put(self, hold: ApprovalHold) -> None:
        self._rows[hold.id] = hold

    def pending_expired_before(self, cutoff: datetime) -> list[ApprovalHold]:
        return [
            h
            for h in self._rows.values()
            if h.status is ApprovalStatus.PENDING and h.expires_at < cutoff
        ]


class ApprovalSurface:
    """Requests, resolves, and expires approval holds. Owns exactly the
    two TECH-4961 invariants (approve-must-advance, expire-must-release);
    everything else about *why* a hold exists is the gate's job
    (`autonomy.py`) and everything about *what advancing means* is the
    caller's job (the orchestrator, TECH-5055/TECH-5058 — passed in as
    callbacks so this module has zero dependency on negotiation state)."""

    def __init__(
        self,
        store: ApprovalStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store or InMemoryApprovalStore()
        self._clock = clock
        self._id_factory = id_factory or self._default_id_factory()

    @staticmethod
    def _default_id_factory() -> Callable[[], str]:
        import itertools

        counter = itertools.count(1)
        return lambda: f"approval-{next(counter)}"

    def request(
        self,
        *,
        owner: str,
        resume_token: str,
        gate_result: GateResult,
        ttl: timedelta = DEFAULT_APPROVAL_TTL,
    ) -> ApprovalHold:
        """Open a hold for a gate decision that routed to `ASK_FIRST`
        (`docs/DESIGN.md` §5's approval surface is only ever reached for
        that tier — an `ACT` decision never creates a hold, it just gets
        notified per `route_notification`). Callers should not call this
        for an `ACT` decision; doing so is not blocked here (this module
        has no opinion on the gate's decision itself) but has no sensible
        `on_advance`/`on_release` meaning."""

        now = self._clock()
        hold = ApprovalHold(
            id=self._id_factory(),
            owner=owner,
            resume_token=resume_token,
            reason=gate_result.reason,
            tier=route_notification(gate_result),
            created_at=now,
            expires_at=now + ttl,
            status=ApprovalStatus.PENDING,
        )
        self._store.put(hold)
        return hold

    def respond(
        self,
        approval_id: str,
        *,
        approved: bool,
        on_advance: Callable[[ApprovalHold], None],
        on_release: Callable[[ApprovalHold], None] | None = None,
    ) -> ApprovalHold:
        """Resolve a pending hold. `approved=True` calls `on_advance`
        exactly once — the approve-must-advance half of TECH-4961: the
        thing this hold was gating must move forward, or it's orphaned,
        excluded from every retry query with nothing left to resume it
        (the exact maiea bug this invariant exists to prevent).
        `approved=False` calls `on_release` (defaulting to a no-op only if
        the caller genuinely has nothing to release, e.g. the gated action
        was abandoned entirely) — a reject is not silently equivalent to
        an approve-then-do-nothing; the caller's negotiation must be told
        explicitly that this path is closed.

        Raises `InvalidApprovalStateError` on an already-resolved or
        unknown hold — never silently no-ops, so a caller racing a UI
        click against an expiry sweep can tell which one won.

        Ordering (Argus round 1 finding, fixed): the callback runs BEFORE
        the resolved status is persisted, not after. Persisting first meant
        an exception from `on_advance`/`on_release` left the hold
        permanently APPROVED/REJECTED with the actual side effect never
        having completed and no way to retry (`respond()` on an
        already-resolved hold just raises `InvalidApprovalStateError`).
        With the callback first, a raise leaves the hold PENDING, so a
        caller can safely call `respond()` again — the caller's own
        callback is responsible for being safe to retry, but at least a
        retry path now exists at all.
        """

        hold = self._store.get(approval_id)
        if hold is None or hold.status is not ApprovalStatus.PENDING:
            raise InvalidApprovalStateError(
                f"approval {approval_id!r} is not a pending hold "
                f"(status={hold.status if hold else 'unknown'})"
            )

        if approved:
            on_advance(hold)
        elif on_release is not None:
            on_release(hold)

        resolved = replace(
            hold,
            status=ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
        )
        self._store.put(resolved)
        return resolved

    def sweep_expired(
        self,
        *,
        on_release: Callable[[ApprovalHold], None],
        now: datetime | None = None,
    ) -> list[ApprovalHold]:
        """The expire-must-release half of TECH-4961: a hold that times
        out unactioned must release whatever it was blocking, or the
        negotiation is stranded the same way an orphaned approve would
        strand it. Idempotent — only PENDING-and-expired holds are
        selected, and each is flipped to EXPIRED only AFTER `on_release`
        succeeds (Argus round 1 finding, fixed: this used to persist
        EXPIRED first, so an exception from `on_release` left the hold
        durably EXPIRED with no retry path -- `pending_expired_before`
        only selects PENDING rows, so a failed release was silently lost
        forever). With the write after, a hold whose `on_release` raises
        stays PENDING-and-expired and is picked up again by the NEXT
        sweep, retrying the release rather than losing it -- still
        idempotent, since a successfully-released hold is marked EXPIRED
        and no longer selected, and the caller's `on_release` is expected
        to be safe to call again on retry. In practice, `Negotiator.react()`
        calls this every tick rather than on a separate scheduler cadence
        (`docs/DESIGN.md` §4); a caller with no `react()`-driven loop of
        its own can still invoke this directly on any cadence it likes
        (`docs/DESIGN.md` §5, not §4 -- Argus round 4 finding, corrected;
        §4 doesn't mention `react()` or expiry sweeps at all).
        **Argus round 3 finding, corrected:** this docstring used to quote
        `docs/DESIGN.md` §5's "2h TTL, batched daily nudge" as this
        method's own cadence -- that phrase was itself retracted from
        DESIGN.md in the same round (a once-a-day *reminder* cadence
        doesn't cohere with a 2h expiry, and conflated two different
        mechanisms: the expiry sweep this method performs, and a
        principal-facing nudge that is a separate, unbuilt notification
        concern with its own still-open cadence). This method's own
        cadence has nothing to do with the nudge; see DESIGN.md §5 for
        the corrected distinction.

        Per-hold error isolation (Argus round 2 finding): one hold's
        `on_release` raising no longer aborts the rest of the batch. This
        matters more than it would in a purely-scheduled sweep because
        `Negotiator.react()` now calls this on every tick (`docs/DESIGN.md`
        §4 wiring) -- an unhandled exception here would have propagated up
        through `react()` and wedged EVERY conversation for that identity,
        not just the one negotiation whose hold failed to release. Each
        hold's failure is caught, logged (with `resume_token` for
        correlation, Argus round 3 finding), and left PENDING-and-expired
        for the next sweep to retry, exactly as a single-hold failure
        already behaved; the only change is that a *second* hold's
        failure no longer depends on the first one succeeding.

        **Known gap, not fixed here (Argus round 3 finding, tracked as
        TECH-5075):** retries are unbounded -- a hold with a permanent
        fault (not a transient one) will retry, fail, and log on every
        single call to this method forever, with no retry counter and no
        escalation to a human. The returned list also silently omits
        failed holds rather than surfacing them; a caller that needs to
        know about failures, not just successes, must inspect this
        method's own logging rather than its return value until TECH-5075
        lands. **Argus round 4 finding, added to the TECH-5075 note:** the
        amplification is per identity, not per conversation -- each
        `Negotiator` owns a single `ApprovalSurface` covering every
        conversation for that identity, and `react()` calls this once per
        tick regardless of which conversation is active. A single
        permanently-poisoned hold therefore emits one `logger.exception`
        per `react()` tick for the WHOLE identity (N_conversations x
        tick_rate), not once per tick for just the affected conversation.
        """

        cutoff = now or self._clock()
        expired = self._store.pending_expired_before(cutoff)
        released: list[ApprovalHold] = []
        failed = 0
        for hold in expired:
            try:
                on_release(hold)
            except Exception:
                failed += 1
                # Argus round 3 finding: the exception log omitted
                # resume_token, the identifier that actually correlates
                # this failure with a specific negotiation in the
                # orchestrator's own logs -- hold.id/owner alone don't.
                logger.exception(
                    "sweep_expired: on_release failed for approval %s (owner=%s, "
                    "resume_token=%s) -- left PENDING-and-expired for the next sweep to retry",
                    hold.id,
                    hold.owner,
                    hold.resume_token,
                )
                continue
            resolved = replace(hold, status=ApprovalStatus.EXPIRED)
            self._store.put(resolved)
            released.append(resolved)
        # Argus round 3 finding: a sweep that finds nothing expired and a
        # sweep that never ran produced identical log output. This summary
        # fires every call (even zero-expired), so "the sweep is running"
        # is always visible in logs, not just its failure path.
        #
        # Argus round 4 finding, fixed: the summary used to log at DEBUG
        # unconditionally, which is filtered out at production INFO level --
        # so `failed > 0` (a poisoned hold, the case a monitor most needs to
        # see) produced no visible summary line, only the per-hold
        # `logger.exception` above. Split by outcome: DEBUG for the
        # nothing-happened case, INFO for a normal release, WARNING when
        # `failed > 0` so a metric filter on this message can alert without
        # parsing stack traces.
        summary_args = (len(expired), len(released), failed)
        if failed > 0:
            logger.warning(
                "sweep_expired: found %d expired hold(s), released %d, failed %d",
                *summary_args,
            )
        elif expired:
            logger.info(
                "sweep_expired: found %d expired hold(s), released %d, failed %d",
                *summary_args,
            )
        else:
            logger.debug(
                "sweep_expired: found %d expired hold(s), released %d, failed %d",
                *summary_args,
            )
        return released
