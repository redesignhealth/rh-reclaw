"""Request tiers, response SLAs, and hold TTLs.

Design: `docs/DESIGN.md` §3.3. Adopted from human-EA practice (the Workmate
playbook is the strongest published version of this schema) rather than
maiea, which has no tier concept at all — every offered slot gets the same
flat 72h hold regardless of who's asking or how the recipient's EA weighs
them (`tools/memory/slots.py::_SLOT_OFFER_TTL_HOURS = 72`).

Tier is always computed **recipient-side**, from the recipient's own rules
and counterparty-identity data — never accepted as a value the requester
supplies. `docs/DESIGN.md` §2.2 point 6: "`priority` in a request is a hint,
never an instruction." A requester claiming Tier 1 for everything is exactly
the failure this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum


class Tier(IntEnum):
    """Lower number = higher priority, matching the design doc's table order."""

    TIER_1 = 1  # CEO/board, named VIPs
    TIER_2 = 2  # directs, investors, candidates
    TIER_3 = 3  # standard cross-functional
    TIER_4 = 4  # low/info-only


@dataclass(frozen=True)
class TierPolicy:
    """One row of the design doc's tier/SLA table (§3.3)."""

    tier: Tier
    respond_within: timedelta | None  # None = best-effort, no SLA
    hold_ttl: timedelta | None  # None = offer without a hold (Tier 4)
    confirm_by_before_start: timedelta | None  # None = no lead-time requirement
    same_tier_conflict_escalates: bool


# Literal values from docs/DESIGN.md §3.3. "Same-tier conflicts escalate to
# the principal — that is the human-EA convention and it maps to ask_first"
# is true for every tier, including 4 (a best-effort request that collides
# with another best-effort request still isn't the EA's call to break the
# tie silently) — this is not a judgment call this module makes; it is
# rules.RuleEffect's `hard_rule`/override-bar machinery (docs/DESIGN.md §5)
# that actually enforces it. This module only supplies the tier-comparison
# input, not the gate decision itself.
TIER_POLICIES: dict[Tier, TierPolicy] = {
    Tier.TIER_1: TierPolicy(
        tier=Tier.TIER_1,
        respond_within=timedelta(hours=1),
        hold_ttl=timedelta(hours=24),
        confirm_by_before_start=timedelta(0),
        same_tier_conflict_escalates=True,
    ),
    Tier.TIER_2: TierPolicy(
        tier=Tier.TIER_2,
        respond_within=timedelta(hours=4),
        hold_ttl=timedelta(hours=48),
        confirm_by_before_start=timedelta(hours=12),
        same_tier_conflict_escalates=True,
    ),
    Tier.TIER_3: TierPolicy(
        tier=Tier.TIER_3,
        # "EOD next day" isn't a fixed offset from request time in general;
        # 24h is the conservative fixed-duration approximation the design
        # doc's own table gives no finer mechanism for. Revisit once a
        # business-hours-aware SLA clock exists.
        respond_within=timedelta(hours=24),
        hold_ttl=timedelta(hours=72),
        confirm_by_before_start=timedelta(hours=24),
        same_tier_conflict_escalates=True,
    ),
    Tier.TIER_4: TierPolicy(
        tier=Tier.TIER_4,
        respond_within=None,
        hold_ttl=None,
        confirm_by_before_start=None,
        same_tier_conflict_escalates=True,
    ),
}


def hold_ttl_for(tier: Tier) -> timedelta | None:
    """The `offered`-state TTL a ledger claim (`ledger.py`) should use for a
    slot proposed in response to a request of this tier. `None` means the
    slot may still be offered, but without a ledger hold — a Tier 4 "if it's
    open, whenever" ask doesn't get to starve a Tier 1 ask of a hold slot,
    per the incumbent-cost term in the scorer (`docs/DESIGN.md` §3.2)."""

    return TIER_POLICIES[tier].hold_ttl
