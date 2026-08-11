"""Confirms reclaw_ea.autonomy actually wires to a live evaluate_gate call
end-to-end, and that a permanently-gated action stays gated regardless of
confidence -- the one invariant docs/DESIGN.md treats as non-negotiable."""

from datetime import UTC, datetime, timedelta

from scheduler_mcp.autonomy.schema import GrabOpenSlotAction, SendExternalInviteAction

from reclaw_ea.autonomy import (
    ConfidenceInputs,
    Decision,
    GateContext,
    InMemoryAutonomyAuditLog,
    evaluate_gate,
)

T0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)


def test_send_external_invite_is_permanently_gated_even_at_max_confidence():
    audit = InMemoryAutonomyAuditLog()
    context = GateContext(
        owner_identity="alice@example.com",
        action=SendExternalInviteAction(
            recipient="bob@external.example.com",
            proposed_start=T0,
            proposed_end=T0 + timedelta(minutes=30),
        ),
        matched_rules=[],
        confidence_inputs=ConfidenceInputs(
            approval_count=10_000, rejection_count=0, explicit_rule_covered=True
        ),
    )
    result = evaluate_gate(context, audit_log=audit)
    assert result.decision is Decision.ASK_FIRST
    assert result.permanently_gated is True


def test_grab_open_slot_starts_permissive_with_zero_history():
    audit = InMemoryAutonomyAuditLog()
    context = GateContext(
        owner_identity="alice@example.com",
        action=GrabOpenSlotAction(slot_start=T0, slot_end=T0 + timedelta(minutes=30)),
        matched_rules=[],
        confidence_inputs=ConfidenceInputs(),
    )
    result = evaluate_gate(context, audit_log=audit)
    assert result.decision is Decision.ACT
