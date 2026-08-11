"""EA-side wiring for `scheduler_mcp.autonomy`.

Design: `docs/DESIGN.md` §5. `scheduler_mcp.autonomy.gate.evaluate_gate` is
already the complete, tested three-valued gate (`{act, ask_first,
cannot_determine}`, never-raise, permanently-gated action classes,
golden-scenario suite) — this module does not reimplement any of that. It
re-exports the pieces an EA needs so callers only import from one place,
and adds the one thing `evaluate_gate` explicitly does NOT do (per that
module's own docstring): decide what happens to a human after a gate
returns `ask_first`. That's `approvals.py`, which this module re-exports
alongside the gate for convenience.
"""

from __future__ import annotations

from scheduler_mcp.autonomy.audit import (
    AutonomyAuditLog,
    AutonomyAuditRecord,
    InMemoryAutonomyAuditLog,
    new_record_id,
)
from scheduler_mcp.autonomy.confidence import MAX_COUNT, confidence_inputs_from_records
from scheduler_mcp.autonomy.gate import MIN_APPROVALS_FOR_AUTONOMY, evaluate_gate
from scheduler_mcp.autonomy.schema import (
    PERMANENTLY_GATED_ACTION_TYPES,
    ActionType,
    ConfidenceInputs,
    Decision,
    GateContext,
    GateResult,
    MeetingMetadata,
    RequesterInfo,
)

__all__ = [
    "MAX_COUNT",
    "MIN_APPROVALS_FOR_AUTONOMY",
    "PERMANENTLY_GATED_ACTION_TYPES",
    "ActionType",
    "AutonomyAuditLog",
    "AutonomyAuditRecord",
    "ConfidenceInputs",
    "Decision",
    "GateContext",
    "GateResult",
    "InMemoryAutonomyAuditLog",
    "MeetingMetadata",
    "RequesterInfo",
    "confidence_inputs_from_records",
    "evaluate_gate",
    "new_record_id",
]
