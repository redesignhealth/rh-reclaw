"""Argus round 1 finding: tiers.py (a load-bearing module -- the
orchestrator calls hold_ttl_for for every ledger claim) had zero test
coverage, including the Tier.TIER_4 -> None branch."""

from datetime import timedelta

import pytest

from reclaw_ea.tiers import TIER_POLICIES, Tier, hold_ttl_for


@pytest.mark.parametrize(
    "tier,expected_ttl",
    [
        (Tier.TIER_1, timedelta(hours=24)),
        (Tier.TIER_2, timedelta(hours=48)),
        (Tier.TIER_3, timedelta(hours=72)),
        (Tier.TIER_4, None),
    ],
)
def test_hold_ttl_for_matches_design_doc_table(tier, expected_ttl):
    assert hold_ttl_for(tier) == expected_ttl


def test_tier_4_returns_none_explicitly():
    """The no-hold branch this design doc §3.3 calls out explicitly --
    pin it as its own assertion so a future refactor can't silently change
    Tier 4 back to a held offer without a test noticing."""
    assert hold_ttl_for(Tier.TIER_4) is None


@pytest.mark.parametrize("tier", list(Tier))
def test_every_tier_has_a_policy(tier):
    assert tier in TIER_POLICIES
    assert TIER_POLICIES[tier].tier is tier


def test_same_tier_conflicts_escalate_for_every_tier():
    assert all(policy.same_tier_conflict_escalates for policy in TIER_POLICIES.values())


def test_tier_1_has_no_lead_time_requirement():
    """§3.3: Tier 1 confirms "immediately" -- zero lead time before start."""
    assert TIER_POLICIES[Tier.TIER_1].confirm_by_before_start == timedelta(0)


def test_tier_4_has_no_sla_or_hold():
    policy = TIER_POLICIES[Tier.TIER_4]
    assert policy.respond_within is None
    assert policy.hold_ttl is None
    assert policy.confirm_by_before_start is None
