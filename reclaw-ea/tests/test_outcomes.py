from datetime import UTC, datetime, timedelta

import pytest
from scheduler_mcp.autonomy.gate import evaluate_gate
from scheduler_mcp.autonomy.schema import (
    ActionType,
    ConfidenceInputs,
    Decision,
    GateContext,
    MeetingDirection,
    MeetingMetadata,
    MoveOwnCommitmentAction,
)

from reclaw_ea.approvals import ApprovalSurface, InvalidApprovalStateError
from reclaw_ea.autonomy import InMemoryAutonomyAuditLog
from reclaw_ea.outcomes import (
    InMemoryOutcomeStore,
    confidence_inputs_with_rejections,
    record_outcome,
    respond_and_record,
)

T0 = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
OWNER = "alice@example.com"
ACTION_TYPE = ActionType.MOVE_OWN_COMMITMENT


def test_record_outcome_is_queryable_by_neighborhood():
    store = InMemoryOutcomeStore()
    record_outcome(
        store,
        owner_identity=OWNER,
        action_type=ACTION_TYPE,
        counterparty_class=None,
        approved=False,
        now=T0,
    )
    record_outcome(
        store,
        owner_identity=OWNER,
        action_type=ACTION_TYPE,
        counterparty_class="vip",
        approved=True,
        now=T0,
    )
    rows = store.query(
        owner_identity=OWNER, action_type=ACTION_TYPE, counterparty_class=None
    )
    assert len(rows) == 1
    assert rows[0].approved is False


def test_confidence_inputs_with_rejections_overrides_the_hardcoded_zero():
    """scheduler_mcp.autonomy.confidence.confidence_inputs_from_records
    always returns rejection_count=0 -- this is the fix."""
    from scheduler_mcp.autonomy.audit import AutonomyAuditRecord

    audit_records = [
        AutonomyAuditRecord(
            id="r1",
            timestamp=T0,
            owner_identity=OWNER,
            action_type=ACTION_TYPE,
            decision=Decision.ASK_FIRST,
            reason="ask",
            permanently_gated=False,
            confidence_inputs=ConfidenceInputs(),
            matched_rule_ids=(),
            counterparty_class=None,
        )
    ]
    outcomes = [
        record_outcome(
            InMemoryOutcomeStore(),
            owner_identity=OWNER,
            action_type=ACTION_TYPE,
            counterparty_class=None,
            approved=False,
            now=T0,
        )
    ]
    result = confidence_inputs_with_rejections(
        audit_records, outcomes, as_of=T0 + timedelta(days=1)
    )
    assert result.rejection_count == 1


def test_confidence_inputs_with_rejections_ignores_outcomes_after_as_of():
    outcomes = [
        record_outcome(
            InMemoryOutcomeStore(),
            owner_identity=OWNER,
            action_type=ACTION_TYPE,
            counterparty_class=None,
            approved=False,
            now=T0 + timedelta(days=10),
        )
    ]
    result = confidence_inputs_with_rejections([], outcomes, as_of=T0)
    assert result.rejection_count == 0


def test_fast_demotion_activates_end_to_end_once_a_rejection_is_recorded():
    """The load-bearing proof: gate.py's fast-demotion branch has existed
    since TECH-4946 but was unreachable because confidence_inputs_from_
    records always produced rejection_count=0. Feeding a real
    OwnerResponseOutcome through confidence_inputs_with_rejections makes
    a high-approval-count neighborhood demote to ask_first the moment a
    single rejection is on file -- with zero changes to gate.py itself."""

    from scheduler_mcp.autonomy.audit import AutonomyAuditRecord

    audit_log = InMemoryAutonomyAuditLog()
    # A neighborhood with a strong approval track record...
    high_confidence = ConfidenceInputs(
        approval_count=10, rejection_count=0, explicit_rule_covered=False
    )
    incumbent = MeetingMetadata(
        organizer=OWNER, attendee_count=2, direction=MeetingDirection.INTERNAL
    )
    context = GateContext(
        owner_identity=OWNER,
        action=MoveOwnCommitmentAction(
            meeting_id="evt-1",
            proposed_start=T0,
            proposed_end=T0 + timedelta(minutes=30),
        ),
        incumbent_meeting=incumbent,
        matched_rules=[],
        confidence_inputs=high_confidence,
    )
    baseline = evaluate_gate(context, audit_log=audit_log)
    assert (
        baseline.decision is Decision.ACT
    )  # confirms the "before" state: track record clears the threshold

    # ...but a single owner rejection, fed through the real feedback loop...
    outcomes = [
        record_outcome(
            InMemoryOutcomeStore(),
            owner_identity=OWNER,
            action_type=ACTION_TYPE,
            counterparty_class=None,
            approved=False,
            now=T0,
        )
    ]
    demoted_inputs = confidence_inputs_with_rejections(
        [
            AutonomyAuditRecord(
                id="r1",
                timestamp=T0,
                owner_identity=OWNER,
                action_type=ACTION_TYPE,
                decision=Decision.ACT,
                reason="prior approvals",
                permanently_gated=False,
                confidence_inputs=high_confidence,
                matched_rule_ids=(),
                counterparty_class=None,
            )
        ]
        * 10,  # matches the approval_count=10 track record
        outcomes,
        as_of=T0 + timedelta(days=1),
    )
    demoted_context = GateContext(
        owner_identity=OWNER,
        action=MoveOwnCommitmentAction(
            meeting_id="evt-1",
            proposed_start=T0,
            proposed_end=T0 + timedelta(minutes=30),
        ),
        incumbent_meeting=incumbent,
        matched_rules=[],
        confidence_inputs=demoted_inputs,
    )
    result = evaluate_gate(demoted_context, audit_log=audit_log)
    # ...forces ask_first regardless of the approval history.
    assert result.decision is Decision.ASK_FIRST
    assert "rejection" in result.reason.lower()


def test_respond_and_record_writes_outcome_only_after_successful_resolution():
    surface = ApprovalSurface(clock=lambda: T0)
    outcome_store = InMemoryOutcomeStore()
    from scheduler_mcp.autonomy.schema import GateResult

    hold = surface.request(
        owner=OWNER,
        resume_token="neg-1",
        gate_result=GateResult(
            decision=Decision.ASK_FIRST, reason="needs a human", permanently_gated=False
        ),
    )
    advanced = []
    respond_and_record(
        surface,
        hold.id,
        approved=True,
        owner_identity=OWNER,
        action_type=ACTION_TYPE,
        counterparty_class=None,
        outcome_store=outcome_store,
        on_advance=lambda h: advanced.append(h.id),
        now=T0,
    )
    assert advanced == [hold.id]
    rows = outcome_store.query(
        owner_identity=OWNER, action_type=ACTION_TYPE, counterparty_class=None
    )
    assert len(rows) == 1
    assert rows[0].approved is True


def test_respond_and_record_does_not_write_an_outcome_on_double_response():
    surface = ApprovalSurface(clock=lambda: T0)
    outcome_store = InMemoryOutcomeStore()
    from scheduler_mcp.autonomy.schema import GateResult

    hold = surface.request(
        owner=OWNER,
        resume_token="neg-1",
        gate_result=GateResult(
            decision=Decision.ASK_FIRST, reason="needs a human", permanently_gated=False
        ),
    )
    respond_and_record(
        surface,
        hold.id,
        approved=True,
        owner_identity=OWNER,
        action_type=ACTION_TYPE,
        counterparty_class=None,
        outcome_store=outcome_store,
        on_advance=lambda h: None,
        now=T0,
    )
    with pytest.raises(InvalidApprovalStateError):
        respond_and_record(
            surface,
            hold.id,
            approved=False,
            owner_identity=OWNER,
            action_type=ACTION_TYPE,
            counterparty_class=None,
            outcome_store=outcome_store,
            on_advance=lambda h: None,
            now=T0,
        )
    rows = outcome_store.query(
        owner_identity=OWNER, action_type=ACTION_TYPE, counterparty_class=None
    )
    assert len(rows) == 1  # not two -- the failed double-response never recorded


def test_expiry_is_never_recorded_as_a_rejection():
    """An expired hold means the owner never responded -- not evidence
    they would have said no. Recording it as a rejection would fabricate
    a signal nobody actually gave."""
    # sweep_expired takes no outcome_store parameter at all -- expiries
    # structurally cannot feed the rejection signal, by construction. This
    # is a pure signature check; no ApprovalSurface/hold setup is needed to
    # make the point (Argus round 2 finding: a prior version constructed a
    # hold via surface.request() that this test never actually used).
    import inspect

    assert (
        "outcome_store"
        not in inspect.signature(ApprovalSurface.sweep_expired).parameters
    )
