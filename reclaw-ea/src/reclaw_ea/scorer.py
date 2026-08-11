"""The preference scorer — the deterministic utility function behind the
comms board's `availability_response.slots[].preference` field.

Design: `docs/DESIGN.md` §3. This is the piece the provenance table in that
doc (§9) marks "new — this repo": neither maiea nor any shipped product
reviewed in the investigation this design followed from actually ranks
candidate slots. maiea's `propose_times` is chronological first-fit with no
scoring at all, and its judgment inputs (sender scores, VIP flags, inferred
meeting type) never reach the slot-choice step. This module is what fills
that gap.

Two layers, kept structurally separate per `docs/DESIGN.md` §3.1/§3.2:

  - **Constraint layer** (not this module): `scheduler_mcp.rules` decides
    what's *feasible* at all — `resolve_precedence` + `RuleEffect` tell the
    caller whether a slot is blocked outright by a hard rule. Nothing in
    this module may override that; the scorer only ever ranks slots the
    rules layer has already allowed through.
  - **Utility layer** (this module): `score_slot` computes a single
    deterministic `0..1` preference for one already-feasible slot, and
    `rank_slots` orders a candidate set. No LLM anywhere in this path —
    the LLM's job is upstream classification (meeting type, counterparty
    identity, tier), never scoring itself (`docs/DESIGN.md` §3.2: "the
    scorer is deterministic and unit-testable — no LLM in the scoring
    path").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

from scheduler_mcp.rules import FlexibilityRange, Rule, Situation, resolve_precedence

from .tiers import Tier

# --- Weights -----------------------------------------------------------
#
# Per-owner-tunable in principle (docs/DESIGN.md §11 open question 2 leaves
# this explicitly open); these are the shipped defaults, chosen so no
# single term can singlehandedly promote an infeasible-adjacent slot over
# a clearly better one. Kept as one dict, not scattered constants, so a
# future per-owner override is a single substitution at the `score_slot`
# call site rather than a signature change.

DEFAULT_WEIGHTS: dict[str, float] = {
    "incumbent": 0.40,
    "fragmentation": 0.25,
    "energy_fit": 0.20,
    "buffer": 0.10,
    "timezone_fairness": 0.05,
}


@dataclass(frozen=True)
class Incumbent:
    """What is already sitting in a candidate slot, if anything. `None`
    fields mean "unknown to the caller" and are scored conservatively
    (worst-case cost), matching `RuleCondition.matches`'s own fail-closed
    posture in `scheduler_mcp.rules` — an under-informed situation should
    never look artificially cheap to move."""

    exists: bool = False
    organizer_is_owner: bool | None = None
    attendee_count: int | None = None


@dataclass(frozen=True)
class SlotContext:
    """Everything the scorer needs about one candidate slot to produce a
    preference value. `situation` is the EA's own construction (per
    `Situation`'s docstring: "this is the EA's job... never the owner"),
    already carrying whatever the upstream classifier inferred (meeting
    type, direction, counterparty ref, holiday-ness)."""

    start: datetime
    end: datetime
    situation: Situation
    incumbent: Incumbent = field(default_factory=Incumbent)
    # Minutes of free time immediately adjacent to this slot on the busier
    # side, i.e. how much of the surrounding focus block this slot would
    # consume if taken. `None` = unknown, scored conservatively.
    adjacent_free_minutes: int | None = None
    # Preferred time-of-day window(s) for this kind of meeting, e.g. the
    # owner's deep-work exclusion already having filtered infeasible slots
    # out — this is a *preference* signal for what's left, not a second
    # constraint check.
    energy_peak: tuple[time, time] | None = None
    # Minutes of buffer directly before/after this slot and the nearest
    # busy interval on each side (mirrors maiea's 5-minute default).
    buffer_before_minutes: int | None = None
    buffer_after_minutes: int | None = None
    # True iff this slot falls inside the counterparty's inferred local
    # business-hours window (docs/DESIGN.md §3.2, ported from maiea
    # `_slots.py`'s generate-in-host-then-filter). None = no counterparty
    # timezone signal at all (domestic, or unknown) — scored neutrally,
    # never penalized, since "unknown" isn't "antisocial."
    within_counterparty_window: bool | None = None
    tier: Tier = Tier.TIER_3


@dataclass(frozen=True)
class SlotScore:
    slot: SlotContext
    preference: float  # 0..1, monotonic with the terms below
    terms: dict[str, float]  # per-component contribution, for explainability/audit


def _incumbent_cost(ctx: SlotContext, matched: list[Rule]) -> float:
    """0 = free slot. Otherwise scaled by the incumbent's flexibility
    range from the highest-precedence matched rule (already
    most-specific-first per `resolve_precedence`) — an `IMMOVABLE`
    incumbent is maximal cost (the caller should not have offered this
    slot at all; the constraint layer, not this function, is what actually
    blocks it), `UNRESTRICTED` costs almost nothing, `SAME_DAY`/`SAME_WEEK`
    sit between. Organizer role and attendee count nudge within that band:
    an owner-organized incumbent is cheaper to move than one organized by
    someone else, and more attendees raise the cost of disturbing it
    (docs/DESIGN.md §3.2: "a 1:1 the owner organized is cheap to move; an
    8-person meeting someone else organized is not" is the rule
    vocabulary's own framing, carried into the score)."""

    if not ctx.incumbent.exists:
        return 0.0

    flexibility = (
        matched[0].effect.flexibility if matched else FlexibilityRange.IMMOVABLE
    )
    base = {
        FlexibilityRange.UNRESTRICTED: 0.1,
        FlexibilityRange.SAME_WEEK: 0.4,
        FlexibilityRange.SAME_DAY: 0.6,
        FlexibilityRange.IMMOVABLE: 1.0,
    }[flexibility]

    # Unknown organizer/attendee-count: conservative (assume "someone
    # else organized it, many attendees") rather than assuming the cheap
    # case — see class docstring.
    organizer_adj = -0.15 if ctx.incumbent.organizer_is_owner is True else 0.05
    count = ctx.incumbent.attendee_count
    count_adj = 0.0 if count is None else min(0.2, 0.03 * max(0, count - 1))

    # Deliberately not upper-clamped here: an IMMOVABLE incumbent already
    # sits at base=1.0, and clamping the sum to 1.0 would erase the
    # organizer/attendee-count adjustments entirely whenever base is
    # already maxed — two IMMOVABLE incumbents with 2 vs. 20 attendees
    # would score identically, which is exactly the differentiation this
    # term exists to provide. `score_slot`'s final preference is clamped
    # to [0, 1] regardless, so an unbounded-above cost here is safe.
    return max(0.0, base + organizer_adj + count_adj)


def _fragmentation_cost(ctx: SlotContext) -> float:
    """Clockwise's objective, simplified to a single slot's contribution:
    taking a slot out of a large contiguous free block costs more than
    taking one out of an already-fragmented gap, because it destroys more
    potential focus time. `None` (unknown adjacency) is scored as the
    worst case — see `SlotContext` docstring."""

    minutes = ctx.adjacent_free_minutes
    if minutes is None:
        return 1.0
    # Diminishing cost as the surrounding block grows past ~2 hours: a
    # slot inside a 15-minute gap barely fragments anything further; one
    # inside a pristine 4-hour block is the expensive case.
    return max(0.0, min(1.0, minutes / 240))


def _energy_fit_cost(ctx: SlotContext) -> float:
    if ctx.energy_peak is None:
        return 0.5  # no signal — neutral, not a penalty
    peak_start, peak_end = ctx.energy_peak
    slot_time = ctx.start.time()
    return 0.0 if peak_start <= slot_time < peak_end else 1.0


def _buffer_cost(ctx: SlotContext, minimum_minutes: int = 5) -> float:
    """Cost of thin buffers on either side. Mirrors maiea's flat 5-minute
    minimum (`_slots.py`), generalized to a cost rather than a hard cutoff
    since the hard cutoff already lives in the constraint layer that
    produced this feasible candidate in the first place."""

    before = ctx.buffer_before_minutes
    after = ctx.buffer_after_minutes
    if before is None and after is None:
        return 0.5  # unknown — neutral
    values = [v for v in (before, after) if v is not None]
    tightest = min(values)
    if tightest >= minimum_minutes * 3:
        return 0.0
    if tightest <= minimum_minutes:
        return 1.0
    return 1.0 - (tightest - minimum_minutes) / (minimum_minutes * 2)


def _timezone_fairness_cost(ctx: SlotContext) -> float:
    if ctx.within_counterparty_window is None:
        return 0.0  # no counterparty tz signal — not antisocial by default
    return 0.0 if ctx.within_counterparty_window else 1.0


def score_slot(
    ctx: SlotContext,
    rules: list[Rule],
    *,
    weights: dict[str, float] | None = None,
) -> SlotScore:
    """Score one already-feasible slot. `rules` should be the owner's full
    rule set; this function calls `resolve_precedence` itself so callers
    never have to re-derive most-specific-first ordering by hand.

    Returns a `preference` in `[0, 1]` where **higher is better** — i.e.
    this is a preference, not a cost; internally every `_..._cost` term is
    inverted before weighting. `terms` in the result carries the raw
    per-component costs (not preferences) for audit/explainability, per
    the design doc's golden-scenario testing goal (§5): a fixed
    (calendar, rules, request) → expected ranked slots suite needs to
    inspect *why* a slot ranked where it did, not just the final number.
    """

    matched = resolve_precedence(rules, ctx.situation)

    costs = {
        "incumbent": _incumbent_cost(ctx, matched),
        "fragmentation": _fragmentation_cost(ctx),
        "energy_fit": _energy_fit_cost(ctx),
        "buffer": _buffer_cost(ctx),
        "timezone_fairness": _timezone_fairness_cost(ctx),
    }
    # Argus round 1 finding: `w[k]` on a caller-supplied partial `weights`
    # dict (fewer than all 5 keys) raised a bare KeyError with no
    # validation or documentation that partial overrides were unsupported.
    # Fall back to `DEFAULT_WEIGHTS[k]` per-key so a caller can override
    # just the terms they care about without having to restate every key.
    #
    # Argus round 2 finding: `weights or DEFAULT_WEIGHTS` treated an
    # explicitly-passed empty dict the same as an unset (`None`) value --
    # functionally identical here (the per-key `.get(k, DEFAULT_WEIGHTS[k])`
    # below already covers a missing key either way), but relying on
    # Python's falsy-empty-dict coincidence rather than an explicit `is not
    # None` check made that equivalence accidental rather than intended.
    w = weights if weights is not None else DEFAULT_WEIGHTS
    weighted_cost = sum(w.get(k, DEFAULT_WEIGHTS[k]) * costs[k] for k in costs)
    total_weight = sum(w.get(k, DEFAULT_WEIGHTS[k]) for k in costs)
    preference = 1.0 - (weighted_cost / total_weight if total_weight else 0.0)
    preference = max(0.0, min(1.0, preference))

    return SlotScore(slot=ctx, preference=preference, terms=costs)


def rank_slots(
    contexts: list[SlotContext],
    rules: list[Rule],
    *,
    weights: dict[str, float] | None = None,
    limit: int = 10,
) -> list[SlotScore]:
    """Score and sort a candidate set, highest preference first. `limit`
    matches the comms board's `availability_response` cap (`docs/DESIGN.md`
    companion, `reclaw-comms-mcp/docs/DESIGN.md` §6: "slots[...] max 10").
    Ties broken by earlier start time, for determinism (mirrors
    `resolve_precedence`'s own tie-break discipline)."""

    scored = [score_slot(ctx, rules, weights=weights) for ctx in contexts]
    scored.sort(key=lambda s: (-s.preference, s.slot.start))
    return scored[:limit]
