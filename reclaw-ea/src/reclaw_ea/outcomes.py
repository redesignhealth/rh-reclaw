"""The owner-response feedback loop — closes the gap `scheduler_mcp.
autonomy.confidence` documents but does not implement: **fast demotion is
currently inert because `rejection_count` is always 0.**

Design: `docs/DESIGN.md` §5, §8. `confidence.py`'s own module docstring is
explicit about why: "there is currently no way to derive a real rejection
signal from `ask_first`/`cannot_determine` records... Closing it — adding
a record type for 'owner responded to ask_first #X with approve/reject'
and feeding it into `rejection_count`... is tracked as TECH-4996." This
module is that record type and that feed, implemented on the reclaw-ea
side of the boundary (the approval surface — `approvals.py` — is where an
owner's actual yes/no response is observed; `scheduler_mcp.autonomy` has
no visibility into that at all, by design, per its own gate/confidence
split).

`respond_and_record` is the integration point: it wraps `ApprovalSurface.
respond` so every resolution writes exactly one `OwnerResponseOutcome` in
the same call — "every approval-surface response (approve/reject/edit)
writes an outcome record" (`docs/DESIGN.md` §5), landing in v1 rather than
staying an inert data-availability gap the way it does upstream today.

Deliberately scoped to REJECTIONS only, not expiries: an expired hold means
the owner never responded, which is not evidence they would have said no —
recording it as a rejection would fabricate a signal nobody actually gave
(the same discipline `confidence_inputs_from_records` already applies to
distinguish "no rule matched" from "the lookup failed").
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from .approvals import ApprovalHold, ApprovalSurface
from .autonomy import (
    MAX_COUNT,
    AutonomyAuditRecord,
    ConfidenceInputs,
    confidence_inputs_from_records,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OwnerResponseOutcome:
    """One recorded owner response to an `ask_first` gate decision.
    Neighborhood-keyed the same way `scheduler_mcp.autonomy.confidence`
    scopes its queries (owner x action_type x counterparty_class) so this
    record composes directly with that module's existing scoping
    convention rather than inventing a second one."""

    id: str
    timestamp: datetime
    owner_identity: str
    action_type: str  # an ActionType value, or a reclaw-ea-local key (booking_gate.BOOKING_ACTION_KEY)
    counterparty_class: str | None
    approved: bool

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("OwnerResponseOutcome.timestamp must be timezone-aware")


class OutcomeStore(ABC):
    @abstractmethod
    def put(self, outcome: OwnerResponseOutcome) -> None: ...

    @abstractmethod
    def query(
        self, *, owner_identity: str, action_type: str, counterparty_class: str | None
    ) -> list[OwnerResponseOutcome]: ...


class InMemoryOutcomeStore(OutcomeStore):
    def __init__(self) -> None:
        self._rows: list[OwnerResponseOutcome] = []

    def put(self, outcome: OwnerResponseOutcome) -> None:
        self._rows.append(outcome)

    def query(
        self, *, owner_identity: str, action_type: str, counterparty_class: str | None
    ) -> list[OwnerResponseOutcome]:
        return [
            o
            for o in self._rows
            if o.owner_identity == owner_identity
            and o.action_type == action_type
            and o.counterparty_class == counterparty_class
        ]


def record_outcome(
    store: OutcomeStore,
    *,
    owner_identity: str,
    action_type: str,
    counterparty_class: str | None,
    approved: bool,
    now: datetime | None = None,
    id_factory: Callable[[], str] | None = None,
) -> OwnerResponseOutcome:
    outcome = OwnerResponseOutcome(
        id=(id_factory or (lambda: uuid.uuid4().hex))(),
        timestamp=now or _utcnow(),
        owner_identity=owner_identity,
        action_type=action_type,
        counterparty_class=counterparty_class,
        approved=approved,
    )
    store.put(outcome)
    return outcome


def respond_and_record(
    surface: ApprovalSurface,
    approval_id: str,
    *,
    approved: bool,
    owner_identity: str,
    action_type: str,
    counterparty_class: str | None,
    outcome_store: OutcomeStore,
    on_advance: Callable[[ApprovalHold], None],
    on_release: Callable[[ApprovalHold], None] | None = None,
    now: datetime | None = None,
) -> ApprovalHold:
    """Resolve a hold through `ApprovalSurface.respond` (preserving its
    approve-must-advance / fail-closed-on-double-response invariants
    unchanged) and, in the same call, write the `OwnerResponseOutcome` that
    upstream `confidence.py` has no way to produce on its own. Raises
    whatever `surface.respond` raises (e.g. `InvalidApprovalStateError`)
    BEFORE recording anything — an outcome is only ever written for a
    response that actually resolved the hold, never for a rejected retry
    or a race loser."""

    resolved = surface.respond(
        approval_id, approved=approved, on_advance=on_advance, on_release=on_release
    )
    record_outcome(
        outcome_store,
        owner_identity=owner_identity,
        action_type=action_type,
        counterparty_class=counterparty_class,
        approved=approved,
        now=now,
    )
    return resolved


def confidence_inputs_with_rejections(
    records: Iterable[AutonomyAuditRecord],
    outcomes: Iterable[OwnerResponseOutcome],
    *,
    as_of: datetime,
) -> ConfidenceInputs:
    """`confidence_inputs_from_records`, with `rejection_count` replaced by
    a real count instead of the hardcoded `0` upstream ships today. `records`
    and `outcomes` must already be scoped to the same (owner, action_type,
    counterparty_class) neighborhood — this function does no scoping of its
    own, matching `confidence_inputs_from_records`'s own contract.

    This is the fast-demotion path becoming real: `gate.py`'s
    `_confidence_decision` already checks `rejection_count > 0` and forces
    `ask_first` when it's true (that branch has existed since TECH-4946 —
    the design doc's own comment calls it "currently unreachable" pending
    exactly this feed). Nothing in `gate.py` needs to change; only the
    input it's given needs to stop being hardcoded to zero.
    """

    base = confidence_inputs_from_records(records, as_of=as_of)
    rejection_count = min(
        sum(1 for o in outcomes if not o.approved and o.timestamp <= as_of), MAX_COUNT
    )
    return ConfidenceInputs(
        approval_count=base.approval_count,
        rejection_count=rejection_count,
        most_recent_decision_at=base.most_recent_decision_at,
        explicit_rule_covered=base.explicit_rule_covered,
    )
