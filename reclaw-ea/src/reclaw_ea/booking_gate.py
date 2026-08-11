"""The gate on booking a completed negotiation.

Design: `docs/DESIGN.md` §2.2 point 3 and §5, decided 2026-08-11: "agent
books it with approval... over time we'll be able to give the agent
autonomy on booking." Completion (every participant's confirm standing) is
not itself authorization to write the calendar invite — it only makes the
owner's EA *eligible* to request booking, gated exactly like every other
consequential action, and able to earn `act` over time from a real
approval track record, the same asymmetric trust curve as everything else
in this design (ask-first by default, quick to earn, quick to lose).

**Why this is not a direct call to `scheduler_mcp.autonomy.gate.
evaluate_gate`**: that function's `ActionType` enum has no member for
"book a freshly negotiated meeting" — its six members are about disturbing
or not disturbing an owner's *existing* commitments (`grab_open_slot`,
`move_own_commitment`, ...) or the two universally-gated externally-visible
actions. `send_external_invite` is the right existing fit for the external
case (this module routes there in spirit — permanently gated, matching
`docs/DESIGN.md`'s own "the applicable internal action type... or
`send_external_invite`" framing) but nothing existing fits "confirm a
brand-new negotiated meeting with an internal counterparty" without
force-fitting a type whose documented semantics don't match (e.g.
`grab_open_slot`'s "no existing commitment is disturbed... starts
permissive on day one" is the WRONG default here — this design wants
ask-first, not permissive, precisely because a negotiated commitment
involves another party who is now expecting it, unlike claiming empty
space on your own calendar unprompted).

This module mirrors `gate.py`'s `_confidence_decision` policy shape (fast
demotion on any rejection, a small approval-count threshold to earn `act`)
rather than importing it, because that function's signature is tied to the
`ActionType` enum this action doesn't have a member in. **Follow-up,
tracked as TECH-5070** (a coordinated `rh-scheduler-mcp` + `reclaw-ea`
change, not a same-repo TODO): propose a `BOOK_NEGOTIATED_MEETING` (or
similar) `ActionType` upstream in `scheduler_mcp.autonomy.schema` so this
collapses into one gate instead of two parallel ones — this module is the
interim, not the intended end state.
"""

from __future__ import annotations

from .autonomy import MIN_APPROVALS_FOR_AUTONOMY, ConfidenceInputs, Decision, GateResult

# Imported, not copied (Argus round 1 finding: a hardcoded `= 5` with a
# comment claiming it mirrors gate.py's constant had no import and no test
# enforcing that -- an upstream retune would silently diverge the two
# policies). Routed through `.autonomy`, not `scheduler_mcp.autonomy.gate`
# directly, so this module's import surface stays consistent with every
# other `reclaw_ea` module (see this module's own docstring on why a
# one-file upstream `ActionType` migration is the goal).
MIN_APPROVALS_FOR_BOOKING_AUTONOMY = MIN_APPROVALS_FOR_AUTONOMY


def evaluate_booking_gate(
    *, is_external: bool, confidence_inputs: ConfidenceInputs
) -> GateResult:
    """Three-valued-in-spirit (reuses `GateResult`/`Decision` for interface
    parity with the rest of this codebase, though `cannot_determine` is
    unreachable here — there is no rule-lookup step this decision depends
    on, unlike `evaluate_gate`).

    `is_external` permanently gates the decision, mirroring
    `send_external_invite`'s treatment in `scheduler_mcp.autonomy.gate`:
    "the action is third-party-visible and mistakes there are the
    expensive, hard-to-recover kind." No confidence override — the design
    doc's "over time we'll be able to give the agent autonomy on booking"
    is about the internal case; nothing here relaxes the pre-existing
    external-invite invariant.
    """

    if is_external:
        return GateResult(
            decision=Decision.ASK_FIRST,
            reason="booking involves an external counterparty -- always ask first, mirroring send_external_invite",
            permanently_gated=True,
        )

    if confidence_inputs.rejection_count > 0:
        return GateResult(
            decision=Decision.ASK_FIRST,
            reason=(
                f"{confidence_inputs.rejection_count} past booking rejection(s) on file -- "
                "a single rejection forces ask_first regardless of approval history"
            ),
            permanently_gated=False,
        )

    if confidence_inputs.approval_count >= MIN_APPROVALS_FOR_BOOKING_AUTONOMY:
        return GateResult(
            decision=Decision.ACT,
            reason=(
                f"{confidence_inputs.approval_count} past booking approval(s) with zero "
                f"rejections clears the {MIN_APPROVALS_FOR_BOOKING_AUTONOMY}-approval threshold"
            ),
            permanently_gated=False,
        )

    return GateResult(
        decision=Decision.ASK_FIRST,
        reason=(
            f"only {confidence_inputs.approval_count} past booking approval(s) on file, below "
            f"the {MIN_APPROVALS_FOR_BOOKING_AUTONOMY}-approval threshold -- a new owner/"
            "counterparty-class neighborhood starts ask-first until enough track record accumulates"
        ),
        permanently_gated=False,
    )
