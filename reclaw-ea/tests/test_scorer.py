"""Golden scorer tests: fixed (rules, situation) -> expected ranked slots.

docs/DESIGN.md §5: "The scorer gets the same treatment [as the autonomy
gate's golden scenarios]: fixed (calendar, rules, request) -> expected
ranked slots." These are that suite's first pass.
"""

from datetime import UTC, datetime, time

import pytest
from scheduler_mcp.rules import (
    FlexibilityRange,
    InMemoryRuleStore,
    MeetingDirection,
    Rule,
    RuleCondition,
    RuleEffect,
    Situation,
    apply_defaults,
)

from reclaw_ea.scorer import (
    DEFAULT_WEIGHTS,
    Incumbent,
    SlotContext,
    rank_slots,
    score_slot,
)

OWNER = "alice@example.com"


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 12, hour, minute, tzinfo=UTC)


def bare_situation(**kwargs) -> Situation:
    return Situation(**kwargs)


def test_free_slot_outranks_a_slot_over_an_immovable_incumbent():
    rules = [
        Rule(
            owner_identity=OWNER,
            description="board meetings are immovable",
            conditions=RuleCondition(meeting_type="board"),
            effect=RuleEffect(flexibility=FlexibilityRange.IMMOVABLE),
        )
    ]
    free = SlotContext(start=dt(14), end=dt(14, 30), situation=bare_situation())
    over_board = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=bare_situation(meeting_type="board"),
        incumbent=Incumbent(exists=True, organizer_is_owner=False, attendee_count=8),
    )
    ranked = rank_slots([over_board, free], rules)
    assert ranked[0].slot is free
    assert ranked[0].preference > ranked[1].preference


def test_unrestricted_incumbent_costs_far_less_than_immovable():
    hard = [
        Rule(
            owner_identity=OWNER,
            description="board immovable",
            conditions=RuleCondition(meeting_type="board"),
            effect=RuleEffect(flexibility=FlexibilityRange.IMMOVABLE),
        )
    ]
    soft = [
        Rule(
            owner_identity=OWNER,
            description="internal 1:1s freely movable",
            conditions=RuleCondition(
                meeting_type="1:1", meeting_direction=MeetingDirection.INTERNAL
            ),
            effect=RuleEffect(flexibility=FlexibilityRange.UNRESTRICTED),
        )
    ]
    over_board = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=bare_situation(meeting_type="board"),
        incumbent=Incumbent(exists=True),
    )
    over_1on1 = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=bare_situation(
            meeting_type="1:1", meeting_direction=MeetingDirection.INTERNAL
        ),
        incumbent=Incumbent(exists=True),
    )
    board_score = score_slot(over_board, hard)
    oneonone_score = score_slot(over_1on1, soft)
    assert oneonone_score.preference > board_score.preference


def test_owner_organized_incumbent_is_cheaper_than_someone_elses():
    rules: list[Rule] = []
    situation = bare_situation()
    owner_organized = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        incumbent=Incumbent(exists=True, organizer_is_owner=True, attendee_count=2),
    )
    other_organized = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        incumbent=Incumbent(exists=True, organizer_is_owner=False, attendee_count=2),
    )
    assert (
        score_slot(owner_organized, rules).preference
        > score_slot(other_organized, rules).preference
    )


def test_more_attendees_raises_incumbent_cost():
    rules: list[Rule] = []
    situation = bare_situation()
    small = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        incumbent=Incumbent(exists=True, organizer_is_owner=False, attendee_count=2),
    )
    large = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        incumbent=Incumbent(exists=True, organizer_is_owner=False, attendee_count=10),
    )
    assert score_slot(small, rules).preference > score_slot(large, rules).preference


def test_most_specific_rule_wins_the_ceo_carveout_case():
    """Mirrors the design doc's own example: internal meetings are
    movable, EXCEPT with the CEO. The counterparty-scoped rule must win
    over the direction-only default (scheduler_mcp.rules.resolve_precedence
    already guarantees this; this test proves the scorer actually reads
    the winning rule, not just the first/any matching rule)."""

    general = Rule(
        owner_identity=OWNER,
        description="internal meetings are movable",
        conditions=RuleCondition(meeting_direction=MeetingDirection.INTERNAL),
        effect=RuleEffect(flexibility=FlexibilityRange.UNRESTRICTED),
    )
    ceo_carveout = Rule(
        owner_identity=OWNER,
        description="never move meetings with the CEO",
        conditions=RuleCondition(
            meeting_direction=MeetingDirection.INTERNAL, counterparty_ref="ceo"
        ),
        effect=RuleEffect(flexibility=FlexibilityRange.IMMOVABLE),
    )
    with_ceo = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=bare_situation(
            meeting_direction=MeetingDirection.INTERNAL, counterparty_ref="ceo"
        ),
        incumbent=Incumbent(exists=True),
    )
    without_ceo = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=bare_situation(
            meeting_direction=MeetingDirection.INTERNAL, counterparty_ref="someone_else"
        ),
        incumbent=Incumbent(exists=True),
    )
    rules = [general, ceo_carveout]
    assert (
        score_slot(without_ceo, rules).preference
        > score_slot(with_ceo, rules).preference
    )


def test_fragmenting_a_large_focus_block_costs_more_than_a_small_gap():
    rules: list[Rule] = []
    situation = bare_situation()
    inside_small_gap = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=20
    )
    inside_big_block = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=240
    )
    assert (
        score_slot(inside_small_gap, rules).preference
        > score_slot(inside_big_block, rules).preference
    )


def test_unknown_adjacency_is_scored_conservatively_not_optimistically():
    rules: list[Rule] = []
    situation = bare_situation()
    known_small_gap = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=20
    )
    unknown = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=None
    )
    assert (
        score_slot(known_small_gap, rules).preference
        > score_slot(unknown, rules).preference
    )


def test_energy_peak_window_is_preferred_over_outside_it():
    rules: list[Rule] = []
    situation = bare_situation()
    peak = (time(9, 0), time(11, 0))
    inside = SlotContext(
        start=dt(9, 30), end=dt(10, 0), situation=situation, energy_peak=peak
    )
    outside = SlotContext(
        start=dt(16, 0), end=dt(16, 30), situation=situation, energy_peak=peak
    )
    assert score_slot(inside, rules).preference > score_slot(outside, rules).preference


def test_tight_buffers_cost_more_than_generous_ones():
    rules: list[Rule] = []
    situation = bare_situation()
    tight = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        buffer_before_minutes=2,
        buffer_after_minutes=2,
    )
    generous = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        buffer_before_minutes=30,
        buffer_after_minutes=30,
    )
    assert score_slot(generous, rules).preference > score_slot(tight, rules).preference


def test_outside_counterparty_window_costs_more_than_inside():
    rules: list[Rule] = []
    situation = bare_situation()
    inside = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        within_counterparty_window=True,
    )
    outside = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        within_counterparty_window=False,
    )
    assert score_slot(inside, rules).preference > score_slot(outside, rules).preference


def test_no_counterparty_timezone_signal_is_not_penalized():
    """docs/DESIGN.md §3.2: unknown tz is not the same as antisocial —
    only a *known* mismatch costs anything."""
    rules: list[Rule] = []
    situation = bare_situation()
    unknown = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        within_counterparty_window=None,
    )
    inside = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        within_counterparty_window=True,
    )
    assert score_slot(unknown, rules).preference == score_slot(inside, rules).preference


def test_rank_slots_is_deterministic_and_capped_at_limit():
    rules: list[Rule] = []
    situation = bare_situation()
    contexts = [
        SlotContext(
            start=dt(9 + i),
            end=dt(9 + i, 30),
            situation=situation,
            adjacent_free_minutes=240,
        )
        for i in range(15)
    ]
    ranked_once = rank_slots(contexts, rules, limit=10)
    ranked_again = rank_slots(contexts, rules, limit=10)
    assert len(ranked_once) == 10
    assert [s.slot.start for s in ranked_once] == [s.slot.start for s in ranked_again]


def test_ties_break_by_earlier_start_time():
    rules: list[Rule] = []
    situation = bare_situation()
    later = SlotContext(start=dt(16), end=dt(16, 30), situation=situation)
    earlier = SlotContext(start=dt(9), end=dt(9, 30), situation=situation)
    ranked = rank_slots([later, earlier], rules)
    assert ranked[0].slot is earlier


def test_score_is_bounded_zero_to_one_even_for_worst_case_slot():
    rules = [
        Rule(
            owner_identity=OWNER,
            description="board immovable",
            conditions=RuleCondition(meeting_type="board"),
            effect=RuleEffect(flexibility=FlexibilityRange.IMMOVABLE),
        )
    ]
    worst = SlotContext(
        start=dt(7),
        end=dt(7, 30),
        situation=bare_situation(meeting_type="board"),
        incumbent=Incumbent(exists=True, organizer_is_owner=False, attendee_count=20),
        adjacent_free_minutes=None,
        buffer_before_minutes=1,
        buffer_after_minutes=1,
        within_counterparty_window=False,
    )
    result = score_slot(worst, rules)
    assert 0.0 <= result.preference <= 1.0


def test_scorer_composes_with_shipped_rule_defaults():
    """Sanity check against apply_defaults (the real starting rule set a
    fresh owner gets), not just hand-built rules in isolation."""
    store = InMemoryRuleStore()
    apply_defaults(store, OWNER)
    store_rules = store.get_rules_for_owner(OWNER)
    situation = bare_situation(start_time=time(9, 30))
    ctx = SlotContext(start=dt(9, 30), end=dt(10, 0), situation=situation)
    result = score_slot(ctx, store_rules)
    assert 0.0 <= result.preference <= 1.0


# --- Argus round 2: partial-weights override coverage (round 1 fixed the
# KeyError bug; these tests verify the actual fallback/validation behavior) ---


def test_partial_weights_override_one_key_falls_back_to_defaults_for_the_rest():
    """A partial override changes the OVERALL preference (weights are a
    normalized average, so re-weighting one term shifts the denominator
    for all of them) -- what this test actually pins down is that the
    other 4 keys fell back to their real DEFAULT_WEIGHTS values, not that
    the result is unaffected. Verified by recomputing the expected
    preference by hand from the same formula score_slot uses."""
    rules: list[Rule] = []
    situation = bare_situation()
    ctx = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=20
    )
    result = score_slot(ctx, rules)
    overridden_weights = {**DEFAULT_WEIGHTS, "incumbent": 0.9}
    overridden_result = score_slot(ctx, rules, weights={"incumbent": 0.9})

    weighted_cost = sum(overridden_weights[k] * result.terms[k] for k in result.terms)
    total_weight = sum(overridden_weights.values())
    expected_preference = max(0.0, min(1.0, 1.0 - weighted_cost / total_weight))
    assert overridden_result.preference == pytest.approx(expected_preference)


def test_partial_weights_override_changes_ranking_when_it_matters():
    rules: list[Rule] = []
    situation = bare_situation()
    tiny_gap = SlotContext(
        start=dt(9),
        end=dt(9, 30),
        situation=situation,
        adjacent_free_minutes=20,
        buffer_before_minutes=1,
        buffer_after_minutes=1,
    )
    big_block = SlotContext(
        start=dt(15),
        end=dt(15, 30),
        situation=situation,
        adjacent_free_minutes=240,
        buffer_before_minutes=60,
        buffer_after_minutes=60,
    )
    # With default weights, tiny_gap (low fragmentation cost) outranks
    # big_block (low buffer cost, but that term is weighted lower).
    default_ranked = rank_slots([tiny_gap, big_block], rules)
    assert default_ranked[0].slot is tiny_gap
    # Overriding buffer's weight far above fragmentation's flips the ranking.
    overridden_ranked = rank_slots(
        [tiny_gap, big_block], rules, weights={"buffer": 5.0, "fragmentation": 0.0}
    )
    assert overridden_ranked[0].slot is big_block


def test_empty_weights_dict_behaves_identically_to_unset():
    """Argus round 2 finding: `weights={}` and `weights=None` must produce
    the same result -- both mean "no overrides, use defaults" -- proving
    the `is not None` check (not a truthiness check) is what's load-bearing,
    even though both branches numerically agree via the per-key fallback."""
    rules: list[Rule] = []
    situation = bare_situation()
    ctx = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=20
    )
    assert (
        score_slot(ctx, rules, weights={}).preference
        == score_slot(ctx, rules, weights=None).preference
    )


def test_unrecognized_weight_key_is_silently_ignored():
    """An extra key in `weights` that doesn't match any of the 5 known
    cost terms is never read by `score_slot` (it only ever looks up the 5
    keys in `costs` via `.get`) -- silently ignored rather than raising,
    which is the intended forward-compatible behavior for a caller passing
    a superset of recognized keys."""
    rules: list[Rule] = []
    situation = bare_situation()
    ctx = SlotContext(
        start=dt(15), end=dt(15, 30), situation=situation, adjacent_free_minutes=20
    )
    baseline = score_slot(ctx, rules)
    with_extra_key = score_slot(
        ctx, rules, weights={**DEFAULT_WEIGHTS, "made_up_term": 99.0}
    )
    assert with_extra_key.preference == baseline.preference
