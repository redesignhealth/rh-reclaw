"""Comms domain service layer — all access rules live here, not in tools.

Every function in this module takes an ``AsyncSession`` plus primitive/typed
arguments (UUIDs, strings, dicts) — never a FastMCP ``AccessToken`` or other
request object. The (not-yet-built) MCP tools layer
is responsible for:

1. Verifying the caller's token and extracting identity claims.
2. Resolving the caller's ``sub`` to a board ``Agent.id`` (for every
   function below except ``register_agent``, which establishes that
   mapping in the first place).
3. Calling exactly one function here per tool invocation and mapping the
   three exception shapes in ``exceptions.py`` to ``ToolError`` messages.

This split keeps token verification out of the domain layer entirely (this
module never imports ``fastmcp``) and keeps every authorization/state-
machine/rate-limit/audit rule in one place so it cannot drift between
tools.

Identity threading (``actor_sub``)
-----------------------------------
Every function that can deny or mutate takes an explicit ``actor_sub``
keyword: the caller's VERIFIED raw token subject, sourced by the tools
layer from token claims — never re-derived here. It is threaded through
separately from any resolved ``*_agent_id`` because the two can fail
independently: a garbage/spoofed ``agent_id`` must still produce an
audit row attributable to the real ``actor_sub`` that presented it. Only
``register_agent`` accepts ``sub`` instead of a resolved ``agent_id``,
since establishing that mapping is exactly what it does.

Access model (v1, internal trust domain)
-----------------------------------------
- Board admission is the permission: an ``Agent`` row (created by
  ``register_agent``) is what lets a ``sub`` participate. No pairwise
  grants (DESIGN.md §4, §10 — the seam for one is ``may_open``).
- Membership = visibility. An ``invited`` participant sees only minimal
  conversation metadata (no messages) until they call ``accept_invite``;
  an ``active`` participant sees full history; a ``left``/``declined``
  participant sees nothing further — identical to a non-member.
- Every conversation-scoped authorization failure raises the single
  uniform ``AccessDeniedError`` (see ``exceptions.py``) — identical message
  whether the conversation does not exist, the caller was never invited,
  is still ``invited`` and trying to read content or post, or
  left/declined. The audit trail distinguishes causes via the ``action``
  column even though the client-visible message never does.
- Conversation-open authorization routes through ``may_open`` (per-target
  predicate) and invites through ``may_invite`` — the seams DESIGN.md §10
  names for a future grants/consent layer.

Judgment calls made in this module (documented once, here, rather than
scattered as inline comments):

- **Expiry vs. history access**: lazy expiry (``expires_at`` in the past)
  flips an ``active`` conversation to ``expired`` on next touch and then
  treats it exactly like ``completed``/``canceled`` for *write* legality
  (``is_message_legal`` already rejects all message types outside
  ``active``). For *reads*, membership is still visibility: a participant
  who was ``active`` before expiry keeps read access to full history
  afterward — expiry ends the negotiation, it does not retroactively
  revoke a member's own record of it. This mirrors how ``completed`` and
  ``canceled`` conversations already remain readable by their members.
- **Unknown agent / type-not-accepted uniformity**: see
  ``exceptions.AccessDeniedError``'s docstring — folded into the uniform denial
  rather than given a leakier, more specific message.
- **Board-level ``Agent.status`` gating**: checked (uniform denial) on the
  *initiating* side of a write — starting a conversation, inviting, and
  posting all require the actor's own agent to be board-``active``, and
  ``invite``/``start_conversation`` require the same of every target. It
  is deliberately NOT re-checked on ``accept_invite``/``decline_invite``/
  ``leave``: those are a participant exiting or resolving their own
  already-granted membership, and a participant should always be able to
  do that even if ops suspends their agent mid-negotiation.

Audit contract
--------------
Every mutation AND every authorization/validation/rate-limit denial writes
an ``audit_log`` row, committed together with (or in place of) the
operation it describes. Denial actions are namespaced ``denied.*``:
``denied.not_member`` (no participant row at all), ``denied.wrong_state.
<status>`` (participant row exists but is in the wrong status — keeps each
of "already active", "declined", "left" distinguishable in the trail even
though the client sees one uniform message), ``denied.unknown_agent``,
``denied.type_not_accepted``, ``denied.already_participant``,
``denied.bad_state`` (state-machine violation), ``denied.bad_schema``
(payload validation), and ``denied.rate_limited``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, Protocol

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from exceptions import AccessDeniedError, InvalidConversationStateError, RateLimitExceededError
from models import Agent, AuditLog, Conversation, Message, Participant, Task
from schemas import (
    CONVERSATION_TYPES,
    MAX_ACCEPTED_TYPES,
    MAX_DISPLAY_NAME_LENGTH,
    TASK_NAMESPACE,
    PayloadValidationError,
    validate_payload,
)
from state_machine import is_message_legal, resulting_conversation_state

# Plain stdlib logging, not structlog/observability.py's event-schema
# helpers (TECH-5094 Argus round 4): this module's own docstring commits to
# never importing fastmcp, and observability.py's log_* helpers exist for
# the tools-layer request lifecycle (tool_call/auth_flow/scope_denial),
# not an arbitrary service-layer diagnostic. This logger exists solely so
# an ownership-lookup failure's full exception (never persisted to the
# audit_log itself -- see the except block below) still lands somewhere
# (CloudWatch, via the ECS log driver) instead of being silently discarded.
logger = logging.getLogger(__name__)

# --- Policy constants --------------------------------------------------------

# Fixed v1 conversation TTL used when a caller doesn't supply an explicit
# ``expires_at`` to ``start_conversation``. Not yet client-configurable
# beyond that override (DESIGN.md §5): negotiations that outlive a week are
# stale. The explicit-override parameter exists mainly so tests (and any
# future admin tooling) can construct already-expired conversations without
# sleeping for a week.
CONVERSATION_TTL = timedelta(days=7)

# Per-sender rate limits, counted from the messages/conversations tables
# directly (no Redis — DESIGN.md §5: "No Redis until it matters").
MAX_MESSAGES_PER_CONVERSATION_PER_HOUR = 30
MAX_CONVERSATION_STARTS_PER_HOUR = 10
MAX_TASK_CREATES_PER_HOUR = 30


def _now() -> datetime:
    return datetime.now(UTC)


# --- Audit helpers ------------------------------------------------------------


def _audit(
    session: AsyncSession,
    *,
    actor_sub: str,
    action: str,
    agent_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Stage an append-only audit row (committed by the caller)."""
    session.add(
        AuditLog(
            actor_sub=actor_sub,
            action=action,
            agent_id=agent_id,
            conversation_id=conversation_id,
            message_id=message_id,
            task_id=task_id,
            detail=detail,
        )
    )


async def _deny(
    session: AsyncSession,
    *,
    actor_sub: str,
    action: str,
    agent_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> NoReturn:
    """Audit a denial, COMMIT it, and raise the uniform ``AccessDeniedError``.

    The commit persists the denial row (and any state already staged on
    the session, e.g. a lazy expiry flip) even though the caller's
    operation fails.
    """
    _audit(
        session,
        actor_sub=actor_sub,
        action=action,
        agent_id=agent_id,
        conversation_id=conversation_id,
        task_id=task_id,
        detail=detail,
    )
    await session.commit()
    raise AccessDeniedError(reason=action)


async def _deny_bad_state(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    current_state: str,
    message_type: str,
) -> NoReturn:
    """Audit + raise the specific (non-uniform) state-machine violation."""
    _audit(
        session,
        actor_sub=actor_sub,
        action="denied.bad_state",
        agent_id=agent_id,
        conversation_id=conversation_id,
        detail={"state": current_state, "message_type": message_type},
    )
    await session.commit()
    raise InvalidConversationStateError(
        f"message type '{message_type}' is not legal while the conversation is '{current_state}'"
    )


async def _deny_rate_limited(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    limit_name: str,
    message: str,
) -> NoReturn:
    _audit(
        session,
        actor_sub=actor_sub,
        action="denied.rate_limited",
        agent_id=agent_id,
        conversation_id=conversation_id,
        detail={"limit": limit_name},
    )
    await session.commit()
    raise RateLimitExceededError(message, reason=limit_name)


async def _deny_bad_schema(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    message_type: str,
    exc: PayloadValidationError,
) -> NoReturn:
    _audit(
        session,
        actor_sub=actor_sub,
        action="denied.bad_schema",
        agent_id=agent_id,
        conversation_id=conversation_id,
        detail={"message_type": message_type},
    )
    await session.commit()
    raise exc


# --- Lookups -------------------------------------------------------------------


async def _find_agent_by_id(session: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    return (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()


async def get_agent_by_sub(session: AsyncSession, sub: str) -> Agent | None:
    """Resolve a caller's verified ``sub`` to their board ``Agent`` row, or ``None``.

    The tools layer (stage 3) calls this on every tool except
    ``register_agent`` to turn the caller's raw token subject into the
    ``agent_id`` every other function in this module expects. Read-only,
    no denial/audit path, and no board-``status`` gating here — "this sub
    has never registered" is not a conversation-authorization decision (it
    is folded into the uniform ``AccessDeniedError`` nowhere else in this
    module), so the tools layer is expected to surface it as its own
    explicit "call comms_register first" error rather than the uniform
    denial, which is about conversation access, not board admission.
    """
    return (await session.execute(select(Agent).where(Agent.sub == sub))).scalar_one_or_none()


async def _fk_safe_agent_id(session: AsyncSession, agent_id: uuid.UUID) -> uuid.UUID | None:
    """Return ``agent_id`` iff it references a real ``Agent`` row, else ``None``.

    ``audit_log.agent_id`` is FK-constrained to ``agents.id``. Most denial
    paths only ever see an ``agent_id`` that already passed through
    ``_require_active_agent`` (so it is always FK-safe by construction),
    but the "not a participant at all" branches below can be reached with
    a caller-presented ``agent_id`` that was never resolved against the
    ``agents`` table at all (e.g. a spoofed/garbage id) — inserting THAT
    into ``audit_log.agent_id`` would raise a ``ForeignKeyViolationError``
    and turn a graceful denial into a crash. This check keeps the denial
    graceful; the raw attempted id is still captured in ``detail`` by the
    caller so the audit trail doesn't lose it.
    """
    exists = (
        await session.execute(select(Agent.id).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    return exists


async def _find_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, for_update: bool = False
) -> Conversation | None:
    stmt = select(Conversation).where(Conversation.id == conversation_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _find_participant(
    session: AsyncSession, conversation_id: uuid.UUID, agent_id: uuid.UUID
) -> Participant | None:
    return (
        await session.execute(
            select(Participant).where(
                Participant.conversation_id == conversation_id,
                Participant.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()


def _maybe_expire(session: AsyncSession, actor_sub: str, conversation: Conversation) -> None:
    """Lazily flip an over-deadline 'active' conversation to 'expired'.

    Persisted by whatever commit the caller performs next (a denial's
    commit, or the operation's own success commit).
    """
    if conversation.state == "active" and conversation.expires_at <= _now():
        conversation.state = "expired"
        _audit(
            session,
            actor_sub=actor_sub,
            action="conversation.expire",
            conversation_id=conversation.id,
        )


async def _require_active_agent(
    session: AsyncSession, *, actor_sub: str, agent_id: uuid.UUID
) -> Agent:
    """Resolve ``agent_id`` to its board-ACTIVE ``Agent``, or deny (uniform).

    Used on the *initiating* side of writes (starting a conversation,
    inviting, posting) — see the module docstring's judgment-call note on
    why this check is deliberately skipped for accept/decline/leave.
    """
    agent = await _find_agent_by_id(session, agent_id)
    if agent is None or agent.status != "active":
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.unknown_agent",
            agent_id=agent.id if agent else None,
        )
    return agent


async def _load_participant_for_transition(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    required_status: str,
    for_update: bool = False,
) -> tuple[Conversation, Participant]:
    """Load a conversation + the caller's participant row, requiring an
    EXACT current participant status; apply lazy expiry; deny (uniform)
    on every other outcome.

    Every failure — no such conversation, no participant row, or a
    participant row in any status other than ``required_status`` —
    raises the identical ``AccessDeniedError``. The audit ``action`` still
    distinguishes "not a participant at all" (``denied.not_member``) from
    "participant, but in the wrong status" (``denied.wrong_state.
    <current_status>``), per the module docstring's audit contract.
    """
    conversation = await _find_conversation(session, conversation_id, for_update=for_update)
    participant = (
        await _find_participant(session, conversation_id, agent_id) if conversation else None
    )
    if conversation is None or participant is None:
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.not_member",
            agent_id=await _fk_safe_agent_id(session, agent_id),
            conversation_id=conversation.id if conversation else None,
            detail={"attempted_agent_id": str(agent_id)},
        )
    _maybe_expire(session, actor_sub, conversation)
    if participant.status != required_status:
        await _deny(
            session,
            actor_sub=actor_sub,
            action=f"denied.wrong_state.{participant.status}",
            agent_id=agent_id,
            conversation_id=conversation.id,
            detail={"required_status": required_status, "current_status": participant.status},
        )
    return conversation, participant


async def _load_participant_for_read(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> tuple[Conversation, Participant]:
    """Load a conversation + participant for ``get_conversation``.

    Unlike ``_load_participant_for_transition``, an ``invited`` participant
    is NOT denied here — ``get_conversation`` itself decides what an
    ``invited`` caller may see (metadata only). Only "no participant row"
    and "left"/"declined" are denied, identically to non-membership.
    """
    conversation = await _find_conversation(session, conversation_id)
    participant = (
        await _find_participant(session, conversation_id, agent_id) if conversation else None
    )
    if conversation is None or participant is None:
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.not_member",
            agent_id=await _fk_safe_agent_id(session, agent_id),
            conversation_id=conversation.id if conversation else None,
            detail={"attempted_agent_id": str(agent_id)},
        )
    if participant.status in ("left", "declined"):
        await _deny(
            session,
            actor_sub=actor_sub,
            action=f"denied.wrong_state.{participant.status}",
            agent_id=agent_id,
            conversation_id=conversation.id,
            detail={"current_status": participant.status},
        )
    _maybe_expire(session, actor_sub, conversation)
    return conversation, participant


# --- Policy seams --------------------------------------------------------------


def may_open(target: Agent, conversation_type: str) -> bool:
    """v1 conversation-open policy for a single target agent.

    DESIGN.md §4/§10's seam for a future grants/consent layer (pairwise,
    directional, expiring, human-approved) once external counterparties
    exist. Today: any board-active agent that lists ``conversation_type``
    in its own ``accepted_types`` may be named as a target — no pairwise
    check between initiator and target in the internal trust domain.
    """
    return target.status == "active" and conversation_type in target.accepted_types


def may_invite(inviter_participant_status: str) -> bool:
    """v1 invite policy: any ACTIVE member may invite.

    Deliberately a plain predicate over just the inviter's participant
    status (not the whole ``Participant``/``Conversation`` objects) so it
    stays trivial to unit-test and to tighten later (e.g. owner-only
    invites) as a policy change, not a migration — DESIGN.md §4/§10.
    """
    return inviter_participant_status == "active"


# --- Serialization helpers ------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _agent_public(agent: Agent) -> dict[str, Any]:
    """Directory projection — compact AXI fields, no ``owner_sub`` leak."""
    return {
        "agent_id": str(agent.id),
        "sub": agent.sub,
        "display_name": agent.display_name,
        "owner_email": agent.owner_email,
        "accepted_types": list(agent.accepted_types),
        "status": agent.status,
    }


def _conversation_dict(conversation: Conversation) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation.id),
        "type": conversation.type,
        "state": conversation.state,
        "created_by": str(conversation.created_by),
        "expires_at": _iso(conversation.expires_at),
        "created_at": _iso(conversation.created_at),
    }


def _message_dict(message: Message, sender_sub: str) -> dict[str, Any]:
    return {
        "seq": message.seq,
        "sender_agent_id": str(message.sender_id),
        "sender_sub": sender_sub,
        "type": message.type,
        "schema_version": message.schema_version,
        "payload": message.payload,
        "created_at": _iso(message.created_at),
    }


# --- Board admission -----------------------------------------------------------


async def register_agent(
    session: AsyncSession,
    *,
    sub: str,
    owner_sub: str,
    owner_email: str,
    display_name: str,
    accepted_types: list[str],
) -> Agent:
    """Idempotently create or re-bind the board ``Agent`` row for ``sub``.

    SECURITY: ``owner_sub``/``owner_email`` MUST be sourced by the caller
    (the MCP tools layer) from verified OAuth token claims — DESIGN.md §4:
    "Owner identity ... is always derived from verified token claims: never
    accepted as a parameter." This function performs NO token
    verification of its own; it persists exactly what it is given. Never
    call it with owner_sub/owner_email taken from untrusted tool arguments.

    Idempotent: calling again with the same ``sub`` updates
    ``display_name``/``accepted_types``/``owner_email`` in place (unique on
    ``agents.sub``) rather than creating a duplicate row, and re-marks the
    agent ``active`` + refreshes ``bound_at``. ``owner_sub`` is the
    exception: it is frozen at first registration and never overwritten by
    a later call, even one presenting a different ``owner_sub`` — see the
    inline comment on the re-registration branch below (TECH-5094): once
    ``add_task``'s ``may_assign`` started reading ``owner_sub`` as an
    admission-decision input, allowing a re-register to change it became a
    forgeable privilege-escalation path, not just an unmodeled edge case.

    Raises ``ValueError`` (not ``AccessDeniedError``) for malformed input — this
    is a data-validation failure, not an authorization decision (the
    caller has not claimed a resource yet). This includes an empty or
    over-length (``schemas.MAX_DISPLAY_NAME_LENGTH``) ``display_name``, and
    an empty, unknown-typed, or over-count (``schemas.MAX_ACCEPTED_TYPES``)
    ``accepted_types``.
    """
    sub = sub.strip()
    if not sub:
        raise ValueError("sub must be non-empty")
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("display_name must be non-empty")
    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(f"display_name exceeds {MAX_DISPLAY_NAME_LENGTH} characters")
    unknown_types = sorted(set(accepted_types) - CONVERSATION_TYPES)
    if not accepted_types or unknown_types:
        raise ValueError(
            "accepted_types must be a non-empty subset of "
            f"{sorted(CONVERSATION_TYPES)} (got unknown: {unknown_types})"
        )
    if len(accepted_types) > MAX_ACCEPTED_TYPES:
        raise ValueError(f"accepted_types exceeds {MAX_ACCEPTED_TYPES} entries")
    normalized_types = sorted(set(accepted_types))

    existing = (await session.execute(select(Agent).where(Agent.sub == sub))).scalar_one_or_none()
    now = _now()
    created = existing is None
    if existing is None:
        agent = Agent(
            sub=sub,
            owner_sub=owner_sub,
            owner_email=owner_email,
            display_name=display_name,
            accepted_types=normalized_types,
            status="active",
            bound_at=now,
        )
        session.add(agent)
    else:
        agent = existing
        # owner_sub is deliberately NOT overwritten on re-registration
        # (TECH-5094 Argus round 1, security/B1): it is now read by
        # AgentTableOwnershipClient as the input to may_assign's admission
        # decision, and rh-auth extra claims (including owner_sub) are
        # caller-supplied and unverified (providers/comms.py). Allowing a
        # re-register to overwrite it would let a caller forge a victim's
        # owner_sub, re-register their own agent under it, and be admitted
        # into that victim's tasks. Freezing it at first registration closes
        # that path; owner_email carries no admission-decision weight today
        # so it is unaffected.
        agent.owner_email = owner_email
        agent.display_name = display_name
        agent.accepted_types = normalized_types
        agent.status = "active"
        agent.bound_at = now
    await session.flush()
    _audit(
        session,
        actor_sub=sub,
        action="agent.register",
        agent_id=agent.id,
        detail={"created": created},
    )
    await session.commit()
    return agent


async def list_agents(
    session: AsyncSession,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Paginated board directory, ordered by ``sub`` (keyset pagination).

    ``cursor`` is the ``sub`` of the last agent from a previous page;
    passing it back returns the next page. Not authorization-gated at this
    layer (internal trust domain, DESIGN.md §10 flags directory enumeration
    as acceptable today and as the seam that tightens once external
    counterparties exist) — no denial paths, so no audit rows.
    """
    limit = max(1, min(limit, 200))
    stmt = select(Agent).order_by(Agent.sub).limit(limit + 1)
    if cursor:
        stmt = stmt.where(Agent.sub > cursor)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    total_count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    return {
        "agents": [_agent_public(a) for a in rows],
        "total_count": total_count,
        "has_more": has_more,
        "next_cursor": rows[-1].sub if has_more and rows else None,
    }


# --- Conversation lifecycle -----------------------------------------------------


async def _enforce_start_rate_limit(
    session: AsyncSession, *, actor_sub: str, initiator: Agent
) -> None:
    one_hour_ago = _now() - timedelta(hours=1)
    count = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.created_by == initiator.id,
                Conversation.created_at > one_hour_ago,
            )
        )
    ).scalar_one()
    if count >= MAX_CONVERSATION_STARTS_PER_HOUR:
        await _deny_rate_limited(
            session,
            actor_sub=actor_sub,
            agent_id=initiator.id,
            conversation_id=None,
            limit_name="conversation_starts_per_hour",
            message=(
                f"rate_limited: at most {MAX_CONVERSATION_STARTS_PER_HOUR} "
                "conversation starts per hour"
            ),
        )


async def _resolve_targets(
    session: AsyncSession,
    *,
    actor_sub: str,
    initiator: Agent,
    target_ids: list[uuid.UUID],
    conversation_type: str,
) -> list[Agent]:
    """Resolve + authorize every named target via ``may_open``.

    Each failure (missing/inactive agent, or type not accepted) raises the
    uniform ``AccessDeniedError`` — see ``exceptions.AccessDeniedError``'s docstring
    for why unknown-agent is folded into the same shape as type-not-
    accepted rather than given a more specific message. The audit trail
    keeps the two causes distinguishable via ``action``.
    """
    rows = (await session.execute(select(Agent).where(Agent.id.in_(target_ids)))).scalars().all()
    by_id = {a.id: a for a in rows}
    for target_id in target_ids:
        target = by_id.get(target_id)
        if target is None or target.status != "active":
            await _deny(
                session,
                actor_sub=actor_sub,
                action="denied.unknown_agent",
                agent_id=initiator.id,
                detail={"target_agent_id": str(target_id)},
            )
        elif not may_open(target, conversation_type):
            await _deny(
                session,
                actor_sub=actor_sub,
                action="denied.type_not_accepted",
                agent_id=initiator.id,
                detail={"target_agent_id": str(target_id), "conversation_type": conversation_type},
            )
    return [by_id[target_id] for target_id in target_ids]


async def start_conversation(
    session: AsyncSession,
    *,
    actor_sub: str,
    initiator_agent_id: uuid.UUID,
    conversation_type: str,
    target_agent_ids: list[uuid.UUID],
    initial_message: dict[str, Any],
    message_type: str = "availability_request",
    schema_version: int = 1,
    expires_at: datetime | None = None,
) -> Conversation:
    """Open a conversation with N other agents; post the seq-1 message.

    The initiator becomes an ``active`` participant with ``role='owner'``;
    every named target becomes an ``invited`` participant (DESIGN.md §4's
    invite/accept revision — "Named targets are added as invited, never
    active on creation"). ``initial_message``/``message_type`` are
    validated via ``schemas.validate_payload`` against
    ``(conversation_type, message_type, schema_version)`` before anything
    is persisted.

    Raises ``ValueError`` for a malformed ``conversation_type`` or an empty
    target list (input-validation, not authorization); ``AccessDeniedError``
    (uniform) if any target is unknown/inactive/doesn't accept the type;
    ``RateLimitExceededError`` past the per-initiator hourly cap;
    ``schemas.PayloadValidationError`` if ``initial_message`` fails schema
    validation.
    """
    initiator = await _require_active_agent(
        session, actor_sub=actor_sub, agent_id=initiator_agent_id
    )
    if conversation_type not in CONVERSATION_TYPES:
        raise ValueError(
            f"unknown conversation_type {conversation_type!r} — supported: "
            f"{sorted(CONVERSATION_TYPES)}"
        )
    target_ids = sorted({t for t in target_agent_ids if t != initiator.id}, key=str)
    if not target_ids:
        raise ValueError("target_agent_ids must name at least one other agent")

    await _enforce_start_rate_limit(session, actor_sub=actor_sub, initiator=initiator)
    targets = await _resolve_targets(
        session,
        actor_sub=actor_sub,
        initiator=initiator,
        target_ids=target_ids,
        conversation_type=conversation_type,
    )

    try:
        payload = validate_payload(conversation_type, message_type, schema_version, initial_message)
    except PayloadValidationError as exc:
        await _deny_bad_schema(
            session,
            actor_sub=actor_sub,
            agent_id=initiator.id,
            conversation_id=None,
            message_type=message_type,
            exc=exc,
        )

    now = _now()
    conversation = Conversation(
        type=conversation_type,
        state="active",
        created_by=initiator.id,
        expires_at=expires_at or (now + CONVERSATION_TTL),
    )
    session.add(conversation)
    await session.flush()

    session.add(
        Participant(
            conversation_id=conversation.id,
            agent_id=initiator.id,
            role="owner",
            status="active",
            invited_by=None,
            joined_at=now,
        )
    )
    for target in targets:
        session.add(
            Participant(
                conversation_id=conversation.id,
                agent_id=target.id,
                role="member",
                status="invited",
                invited_by=initiator.id,
                joined_at=None,
            )
        )

    message = Message(
        conversation_id=conversation.id,
        seq=1,
        sender_id=initiator.id,
        type=message_type,
        schema_version=schema_version,
        payload=payload,
    )
    session.add(message)
    await session.flush()

    _audit(
        session,
        actor_sub=actor_sub,
        action="conversation.start",
        agent_id=initiator.id,
        conversation_id=conversation.id,
        detail={"type": conversation_type, "target_agent_ids": [str(t) for t in target_ids]},
    )
    _audit(
        session,
        actor_sub=actor_sub,
        action="message.post",
        agent_id=initiator.id,
        conversation_id=conversation.id,
        message_id=message.id,
        detail={"seq": 1, "message_type": message_type},
    )
    await session.commit()
    return conversation


async def accept_invite(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Participant:
    """Flip the caller's participant status ``invited`` -> ``active``.

    Requires the caller to currently be an ``invited`` participant on this
    conversation; any other state (not a participant at all, already
    ``active``, ``left``, or ``declined``) raises the uniform
    ``AccessDeniedError`` — see the module docstring's audit contract for how
    the audit trail still distinguishes each cause.
    """
    conversation, participant = await _load_participant_for_transition(
        session,
        actor_sub=actor_sub,
        agent_id=agent_id,
        conversation_id=conversation_id,
        required_status="invited",
    )
    participant.status = "active"
    participant.joined_at = _now()
    _audit(
        session,
        actor_sub=actor_sub,
        action="participant.accept",
        agent_id=agent_id,
        conversation_id=conversation.id,
    )
    await session.commit()
    return participant


async def decline_invite(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> None:
    """Set the caller's pending invite to ``declined``. No access is granted.

    Requires the caller to currently be ``invited``. Declining is terminal:
    it does not flip through ``active`` first, so no message content is
    ever disclosed to a declining caller (DESIGN.md §4: "Calling decline
    sets 'declined' directly: no access is ever granted").
    """
    conversation, participant = await _load_participant_for_transition(
        session,
        actor_sub=actor_sub,
        agent_id=agent_id,
        conversation_id=conversation_id,
        required_status="invited",
    )
    participant.status = "declined"
    _audit(
        session,
        actor_sub=actor_sub,
        action="participant.decline_invite",
        agent_id=agent_id,
        conversation_id=conversation.id,
    )
    await session.commit()
    return None


async def invite(
    session: AsyncSession,
    *,
    actor_sub: str,
    inviter_agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    target_agent_id: uuid.UUID,
) -> Participant:
    """Add ``target_agent_id`` to a conversation as a new ``invited`` row.

    ``inviter_agent_id`` must currently be an ``active`` participant
    (``may_invite`` — v1: any active member, tightenable to owner-only
    later without a migration). The target must be a board-active agent
    that accepts the conversation's type and must not already have a
    participant row in ANY status (re-inviting a former member is out of
    scope for v1 — DESIGN.md does not define re-invite semantics, and a
    ``declined`` row in particular must never be overridable by another
    member, since decline is the consent mechanism).
    """
    conversation, inviter_participant = await _load_participant_for_transition(
        session,
        actor_sub=actor_sub,
        agent_id=inviter_agent_id,
        conversation_id=conversation_id,
        required_status="active",
    )
    if not may_invite(inviter_participant.status):  # pragma: no cover — v1 always True here
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.invite_not_allowed",
            agent_id=inviter_agent_id,
            conversation_id=conversation.id,
        )
    if conversation.state != "active":
        await _deny_bad_state(
            session,
            actor_sub=actor_sub,
            agent_id=inviter_agent_id,
            conversation_id=conversation.id,
            current_state=conversation.state,
            message_type="invite",
        )
    target = await _find_agent_by_id(session, target_agent_id)
    if target is None or target.status != "active":
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.unknown_agent",
            agent_id=inviter_agent_id,
            conversation_id=conversation.id,
            detail={"target_agent_id": str(target_agent_id)},
        )
    if not may_open(target, conversation.type):
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.type_not_accepted",
            agent_id=inviter_agent_id,
            conversation_id=conversation.id,
            detail={
                "target_agent_id": str(target_agent_id),
                "conversation_type": conversation.type,
            },
        )
    existing = await _find_participant(session, conversation.id, target.id)
    if existing is not None:
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.already_participant",
            agent_id=inviter_agent_id,
            conversation_id=conversation.id,
            detail={"target_agent_id": str(target_agent_id), "current_status": existing.status},
        )
    participant = Participant(
        conversation_id=conversation.id,
        agent_id=target.id,
        role="member",
        status="invited",
        invited_by=inviter_agent_id,
        joined_at=None,
    )
    session.add(participant)
    await session.flush()
    _audit(
        session,
        actor_sub=actor_sub,
        action="participant.invite",
        agent_id=target.id,
        conversation_id=conversation.id,
        detail={"invited_by_agent_id": str(inviter_agent_id)},
    )
    await session.commit()
    return participant


async def leave(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> None:
    """Leave a conversation: participant status -> ``left``, access revoked.

    Requires the caller to currently be ``active``. This is pure exit
    bookkeeping — it does not affect ``conversation.state`` or trigger the
    decline cascade. To decline a negotiation (with cascade-to-``canceled``
    semantics), post a ``decline`` message via ``post_message`` instead;
    that is the consent mechanism, ``leave`` is not (DESIGN.md §4/§6).
    """
    conversation, participant = await _load_participant_for_transition(
        session,
        actor_sub=actor_sub,
        agent_id=agent_id,
        conversation_id=conversation_id,
        required_status="active",
    )
    participant.status = "left"
    _audit(
        session,
        actor_sub=actor_sub,
        action="participant.leave",
        agent_id=agent_id,
        conversation_id=conversation.id,
    )
    await session.commit()
    return None


async def _enforce_message_rate_limit(
    session: AsyncSession,
    *,
    actor_sub: str,
    sender_agent_id: uuid.UUID,
    conversation: Conversation,
) -> None:
    one_hour_ago = _now() - timedelta(hours=1)
    count = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.sender_id == sender_agent_id,
                Message.created_at > one_hour_ago,
            )
        )
    ).scalar_one()
    if count >= MAX_MESSAGES_PER_CONVERSATION_PER_HOUR:
        await _deny_rate_limited(
            session,
            actor_sub=actor_sub,
            agent_id=sender_agent_id,
            conversation_id=conversation.id,
            limit_name="messages_per_conversation_per_hour",
            message=(
                f"rate_limited: at most {MAX_MESSAGES_PER_CONVERSATION_PER_HOUR} "
                "messages per conversation per hour"
            ),
        )


async def _all_non_owners_declined(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Whether every ``role='member'`` participant is currently ``declined``.

    A conversation with no members at all (shouldn't happen — every
    conversation is created with at least one target) never counts as
    "all declined". A member who is ``invited``/``active``/``left`` blocks
    the cascade: only an explicit ``decline`` counts.
    """
    member_statuses = (
        (
            await session.execute(
                select(Participant.status).where(
                    Participant.conversation_id == conversation_id,
                    Participant.role == "member",
                )
            )
        )
        .scalars()
        .all()
    )
    return bool(member_statuses) and all(status == "declined" for status in member_statuses)


async def post_message(
    session: AsyncSession,
    *,
    actor_sub: str,
    sender_agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_type: str,
    payload: dict[str, Any],
    schema_version: int = 1,
) -> Message:
    """Append a schema-validated message; apply state-machine side effects.

    Requires ``sender_agent_id`` to be a board-active agent (uniform denial
    otherwise) AND a currently-``active`` participant on ``conversation_id``
    (same uniform denial, identical whether the conversation doesn't
    exist, the sender was never invited, is still ``invited``, or has
    ``left``/``declined`` — DESIGN.md §4/§8's anti-enumeration rule).

    ``seq`` is assigned under ``SELECT ... FOR UPDATE`` on the conversation
    row (acquired while loading the participant), so concurrent posters to
    the same conversation serialize and every seq is gapless and race-safe.

    Side effects: ``confirm`` transitions the conversation to
    ``completed``; ``decline`` sets the sender's OWN participant status to
    ``declined`` and, only once every non-owner participant is now
    ``declined`` (``_all_non_owners_declined``), transitions the
    conversation to ``canceled``.

    Raises ``RateLimitExceededError`` past the per-sender-per-conversation
    hourly cap; ``InvalidConversationStateError`` if ``message_type`` is not
    legal in the conversation's current state (state-machine violation,
    including after lazy expiry); ``schemas.PayloadValidationError`` if
    ``payload`` fails schema validation, or if a ``needs_clarification``'s
    ``about_seq`` does not reference an existing prior message.
    """
    await _require_active_agent(session, actor_sub=actor_sub, agent_id=sender_agent_id)
    conversation, participant = await _load_participant_for_transition(
        session,
        actor_sub=actor_sub,
        agent_id=sender_agent_id,
        conversation_id=conversation_id,
        required_status="active",
        for_update=True,
    )

    await _enforce_message_rate_limit(
        session, actor_sub=actor_sub, sender_agent_id=sender_agent_id, conversation=conversation
    )

    if not is_message_legal(conversation.state, message_type):
        await _deny_bad_state(
            session,
            actor_sub=actor_sub,
            agent_id=sender_agent_id,
            conversation_id=conversation.id,
            current_state=conversation.state,
            message_type=message_type,
        )

    try:
        validated = validate_payload(conversation.type, message_type, schema_version, payload)
    except PayloadValidationError as exc:
        await _deny_bad_schema(
            session,
            actor_sub=actor_sub,
            agent_id=sender_agent_id,
            conversation_id=conversation.id,
            message_type=message_type,
            exc=exc,
        )

    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(Message.seq), 0)).where(
                Message.conversation_id == conversation.id
            )
        )
    ).scalar_one() + 1

    if message_type == "needs_clarification":
        about_seq = int(validated["about_seq"])
        if about_seq >= next_seq:
            await _deny_bad_schema(
                session,
                actor_sub=actor_sub,
                agent_id=sender_agent_id,
                conversation_id=conversation.id,
                message_type=message_type,
                exc=PayloadValidationError(
                    f"payload failed schema validation: about_seq: {about_seq} does not "
                    "reference a prior message in this conversation"
                ),
            )

    message = Message(
        conversation_id=conversation.id,
        seq=next_seq,
        sender_id=sender_agent_id,
        type=message_type,
        schema_version=schema_version,
        payload=validated,
    )
    session.add(message)
    await session.flush()
    _audit(
        session,
        actor_sub=actor_sub,
        action="message.post",
        agent_id=sender_agent_id,
        conversation_id=conversation.id,
        message_id=message.id,
        detail={"seq": next_seq, "message_type": message_type},
    )

    if message_type == "decline":
        participant.status = "declined"
        _audit(
            session,
            actor_sub=actor_sub,
            action="participant.decline",
            agent_id=sender_agent_id,
            conversation_id=conversation.id,
            detail={"reason": validated.get("reason")},
        )
        all_declined = await _all_non_owners_declined(session, conversation.id)
        new_state = resulting_conversation_state("decline", all_non_owners_declined=all_declined)
        if new_state is not None:
            conversation.state = new_state
            _audit(
                session,
                actor_sub=actor_sub,
                action="conversation.close",
                agent_id=sender_agent_id,
                conversation_id=conversation.id,
                detail={"new_state": new_state, "via": "decline"},
            )
    elif message_type == "confirm":
        new_state = resulting_conversation_state("confirm")
        if new_state is not None:
            conversation.state = new_state
            _audit(
                session,
                actor_sub=actor_sub,
                action="conversation.close",
                agent_id=sender_agent_id,
                conversation_id=conversation.id,
                detail={"new_state": new_state, "via": "confirm"},
            )

    await session.commit()
    return message


async def get_conversation(
    session: AsyncSession,
    *,
    actor_sub: str,
    caller_agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    since_seq: int = 0,
) -> dict[str, Any]:
    """Combined read: conversation + participants + messages since ``since_seq``.

    An ``invited`` (not yet accepted) caller gets METADATA ONLY: the
    returned dict has ``"invited": True`` and an empty ``"messages"`` list
    — never any message content — and the caller's ``last_read_seq`` is
    NOT advanced (there is nothing to mark read). An ``active`` caller gets
    full history (respecting ``since_seq``) and their ``last_read_seq`` is
    advanced to the max seq actually returned (only if any messages were
    returned). A caller who is not a participant, or who previously
    left/declined, gets the uniform ``AccessDeniedError`` — identical to a
    non-existent conversation (DESIGN.md §4/§8).
    """
    conversation, participant = await _load_participant_for_read(
        session,
        actor_sub=actor_sub,
        agent_id=caller_agent_id,
        conversation_id=conversation_id,
    )

    part_rows = (
        await session.execute(
            select(Participant, Agent)
            .join(Agent, Agent.id == Participant.agent_id)
            .where(Participant.conversation_id == conversation.id)
            .order_by(Agent.sub)
        )
    ).all()
    participants_view = [
        {
            "agent_id": str(a.id),
            "sub": a.sub,
            "display_name": a.display_name,
            "role": p.role,
            "status": p.status,
            "invited_by": str(p.invited_by) if p.invited_by else None,
        }
        for p, a in part_rows
    ]

    if participant.status == "invited":
        await session.commit()
        return {
            "conversation": _conversation_dict(conversation),
            "participants": participants_view,
            "messages": [],
            "invited": True,
            "invited_by": str(participant.invited_by) if participant.invited_by else None,
        }

    msg_rows = (
        await session.execute(
            select(Message, Agent.sub)
            .join(Agent, Agent.id == Message.sender_id)
            .where(Message.conversation_id == conversation.id, Message.seq > since_seq)
            .order_by(Message.seq)
        )
    ).all()
    max_seq = max((m.seq for m, _ in msg_rows), default=None)
    if max_seq is not None and max_seq > participant.last_read_seq:
        participant.last_read_seq = max_seq
    await session.commit()

    return {
        "conversation": _conversation_dict(conversation),
        "participants": participants_view,
        "messages": [_message_dict(m, sender_sub) for m, sender_sub in msg_rows],
        "invited": False,
        "total_count": len(msg_rows),
        "last_read_seq": participant.last_read_seq,
    }


async def inbox(session: AsyncSession, *, caller_agent_id: uuid.UUID) -> dict[str, Any]:
    """Unread-first inbox for the caller's agent: unread + pending invites.

    ``unread``: every conversation where the caller is an ``active``
    participant and ``max(seq) > last_read_seq`` — regardless of
    conversation state, so a completion/cancelation message still
    surfaces once. ``pending_invites``: every conversation where the
    caller has a pending ``invited`` row (metadata only — no message
    peek, matching ``get_conversation``'s invited-caller behavior).

    Explicit empty state: always returns the same three keys, even when
    both lists are empty, so a tools layer can render "nothing needs your
    attention" rather than reasoning about an ambiguous bare empty list
    (AXI convention, DESIGN.md §7).

    Read-only with no denial path for a valid ``caller_agent_id`` (no
    audit rows) — lazy expiry is intentionally NOT applied here (it would
    require touching every returned conversation individually); it is
    applied on whichever read/write path next touches a given conversation
    directly (``get_conversation``, ``post_message``, etc.).
    """
    unread_rows = (
        await session.execute(
            select(
                Conversation,
                Participant.last_read_seq,
                func.max(Message.seq).label("max_seq"),
                func.count(Message.id)
                .filter(Message.seq > Participant.last_read_seq)
                .label("unread"),
            )
            .join(Participant, Participant.conversation_id == Conversation.id)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(Participant.agent_id == caller_agent_id, Participant.status == "active")
            .group_by(Conversation.id, Participant.last_read_seq)
            .having(func.max(Message.seq) > Participant.last_read_seq)
            .order_by(func.max(Message.created_at).desc())
        )
    ).all()

    # Fetch every unread conversation's latest message + sender sub in a
    # single round trip (instead of one SELECT per conversation in a Python
    # loop): join Message/Agent against the exact (conversation_id, max_seq)
    # pairs already computed above via a composite-tuple IN.
    latest_by_conversation_id: dict[uuid.UUID, tuple[Message, str]] = {}
    conversation_seq_pairs = [
        (conversation.id, max_seq) for conversation, _, max_seq, _ in unread_rows
    ]
    if conversation_seq_pairs:
        latest_rows = (
            await session.execute(
                select(Message, Agent.sub)
                .join(Agent, Agent.id == Message.sender_id)
                .where(tuple_(Message.conversation_id, Message.seq).in_(conversation_seq_pairs))
            )
        ).all()
        latest_by_conversation_id = {
            message.conversation_id: (message, sender_sub) for message, sender_sub in latest_rows
        }

    unread: list[dict[str, Any]] = []
    for conversation, last_read_seq, _max_seq, unread_count in unread_rows:
        latest, sender_sub = latest_by_conversation_id[conversation.id]
        unread.append(
            {
                **_conversation_dict(conversation),
                "unread_count": unread_count,
                "last_read_seq": last_read_seq,
                "latest_message": _message_dict(latest, sender_sub),
            }
        )

    pending_rows = (
        await session.execute(
            select(Conversation, Participant)
            .join(Participant, Participant.conversation_id == Conversation.id)
            .where(Participant.agent_id == caller_agent_id, Participant.status == "invited")
            .order_by(Participant.invited_at.desc())
        )
    ).all()
    pending_invites = [
        {
            **_conversation_dict(conversation),
            "invited_by": str(p.invited_by) if p.invited_by else None,
            "invited_at": _iso(p.invited_at),
        }
        for conversation, p in pending_rows
    ]

    return {
        "unread": unread,
        "pending_invites": pending_invites,
        "total_count": len(unread) + len(pending_invites),
    }


# --- Tasks (internal.coordination, TECH-5094) -----------------------------------
#
# A dedicated table, not conversations/messages (see TECH-5094 §1): a task's
# ``status`` mutates in place (like ``participants.status``), and visibility
# is simply "caller is created_by or assignee_id" — no invite/accept
# ceremony, since a task is intrinsically two-party. No
# ``internal.coordination`` entry is added to ``CONVERSATION_TYPES``: agents
# cannot ``start_conversation`` of that "type" — ``TASK_NAMESPACE`` exists
# only as the schema-registry coordinate for ``TaskSpecV1``.


class OwnershipClient(Protocol):
    """Resolves a board agent's verified owner set — the seam for ``may_assign``.

    The real implementation calls the reclaw platform's ownership lookup
    (not yet built as of TECH-5094; tracked as a follow-up). Tests fake
    this protocol directly. Every caller of ``get_agent_owners`` MUST fail
    closed on any exception — never treat a lookup error as "no match" vs.
    "match", since either silently loosens or tightens admission depending
    on what the caller assumes. ``agents.owner_sub``/``owner_email`` must
    NEVER be read directly for this decision (single-valued columns a
    shared agent's row cannot faithfully represent) — this protocol is
    the only sanctioned path.

    Implementations must not hold a live DB session open across their own
    ``get_agent_owners`` call: the eventual real implementation makes an
    external HTTP call to the reclaw platform, and holding a checked-out
    ``AsyncSession`` (and its connection-pool slot) for the duration of
    that round trip risks pool exhaustion under concurrency
    (``db.py``'s ``pool_size``/``max_overflow`` are small). Construct a
    future HTTP-backed implementation independently of any request's
    session, not inside the ``async with get_session_factory()()`` block
    that owns it — unlike the interim ``AgentTableOwnershipClient`` below,
    which is a same-transaction DB read and has no such constraint.
    """

    async def get_agent_owners(self, agent_id: uuid.UUID) -> dict[str, Any]:
        """Return ``{"is_shared": bool, "owners": list[str]}`` for ``agent_id``.

        Raise on any lookup failure/timeout/empty result — the caller
        treats every exception as fail-closed (``denied.ownership_unverified``).
        """
        ...


class AgentTableOwnershipClient:
    """Interim ``OwnershipClient`` until the reclaw platform's real ownership
    endpoint ships (TECH-5094 follow-up).

    Wraps the existing ``agents.owner_sub`` column as a single-element
    owner set; ``is_shared`` is always ``False`` since no shared-agent
    concept exists in this schema yet. This is exactly correct for every
    agent registered today: two agents bound to the same ``owner_sub``
    (e.g. two agents belonging to one person) intersect via ``may_assign``
    precisely as they should, and unrelated agents correctly do not. Swap
    this for a real platform-backed client the moment shared agents exist
    — the ``OwnershipClient`` protocol is the seam, not this class.

    Safe to use as an authorization input specifically because
    ``register_agent`` freezes ``owner_sub`` at first registration and
    never overwrites it on re-registration (see that function) — reading
    it here would otherwise let a caller forge a victim's ``owner_sub``
    via a re-register call.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_agent_owners(self, agent_id: uuid.UUID) -> dict[str, Any]:
        agent = await _find_agent_by_id(self._session, agent_id)
        if agent is None:
            raise LookupError(f"unknown agent {agent_id}")
        return {"is_shared": False, "owners": [agent.owner_sub]}


def may_assign(creator_owners: set[str], assignee_owners: set[str]) -> bool:
    """v1 ``add_task`` admission policy (TECH-5094 gap #1): symmetric verified
    owner-set intersection — ``owners(creator) ∩ owners(assignee) ≠ ∅``.

    Degenerates to an exact same-owner check for two non-shared agents
    (each owner set is a singleton); generalizes symmetrically once a
    shared agent's verified owner set has more than one entry, so a shared
    agent may be either the requester (report-back direction) or the
    target.
    """
    return not creator_owners.isdisjoint(assignee_owners)


def _task_public(
    task: Task, *, caller_agent_id: uuid.UUID, created_by_sub: str, assignee_sub: str
) -> dict[str, Any]:
    """The one canonical AXI shape for a task — used by both ``add_task`` and
    ``get_tasks``, so the same resource never comes back with two different
    shapes depending on which tool returned it. Private/unauthorized by
    design (TECH-5094 Argus round 2, security/B1): performs no visibility
    check of its own, so every caller MUST have already established that
    ``caller_agent_id`` may see ``task`` before calling this. Never export
    this at module level — a future caller reaching for a "lower-level"
    export could otherwise bypass whatever authorization its call sites
    are relying on today (``add_task``'s caller is trivially the task's own
    creator; ``get_tasks``'s ``role_filter`` already scopes its query to
    rows the caller may see)."""
    return {
        "task_id": str(task.id),
        "role": "created" if task.created_by == caller_agent_id else "assigned",
        "status": task.status,
        "created_by": str(task.created_by),
        "created_by_sub": created_by_sub,
        "assignee_agent_id": str(task.assignee_id),
        "assignee_sub": assignee_sub,
        "payload": task.payload,
        "schema_version": task.schema_version,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
    }


async def _enforce_task_create_rate_limit(
    session: AsyncSession, *, actor_sub: str, creator: Agent
) -> None:
    one_hour_ago = _now() - timedelta(hours=1)
    count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.created_by == creator.id, Task.created_at > one_hour_ago)
        )
    ).scalar_one()
    if count >= MAX_TASK_CREATES_PER_HOUR:
        await _deny_rate_limited(
            session,
            actor_sub=actor_sub,
            agent_id=creator.id,
            conversation_id=None,
            limit_name="task_creates_per_hour",
            message=f"rate_limited: at most {MAX_TASK_CREATES_PER_HOUR} task creates per hour",
        )


async def add_task(
    session: AsyncSession,
    *,
    actor_sub: str,
    creator_agent_id: uuid.UUID,
    assignee_agent_id: uuid.UUID,
    task: dict[str, Any],
    ownership_client: OwnershipClient,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Create a task assigned from ``creator_agent_id`` to ``assignee_agent_id``.

    Returns the same canonical AXI shape ``get_tasks`` returns for this
    resource (``task_id``, ``role``, ``status``, ``created_by``,
    ``created_by_sub``, ``assignee_agent_id``, ``assignee_sub``,
    ``payload``, ``schema_version``, ``created_at``, ``updated_at``) —
    ``role`` is always ``"created"`` here, since the caller is the task's
    own creator.

    Bidirectional (DESIGN.md/TECH-5094 §5): either party in an admitted
    pair may call this — a Chief-of-Staff assigning work down, or an EA
    reporting status back up via a ``report_status``-action task. The
    ownership predicate (``may_assign``) is the entire authorization story;
    there is no separate reporting-lines concept.

    Raises ``ValueError`` for malformed input (unknown/inactive assignee is
    NOT this — see below); ``AccessDeniedError`` (uniform) if the assignee
    is unknown/inactive, ownership can't be verified, or the two agents'
    owner sets don't intersect; ``RateLimitExceededError`` past
    ``MAX_TASK_CREATES_PER_HOUR``; ``schemas.PayloadValidationError`` if
    ``task`` fails schema validation or ``related_conversation_id`` doesn't
    reference a conversation the caller belongs to.
    """
    creator = await _require_active_agent(session, actor_sub=actor_sub, agent_id=creator_agent_id)
    if assignee_agent_id == creator_agent_id:
        raise ValueError("assignee_agent_id must differ from the caller's own agent id")

    assignee = await _find_agent_by_id(session, assignee_agent_id)
    if assignee is None or assignee.status != "active":
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.unknown_agent",
            agent_id=creator.id,
            detail={"assignee_agent_id": str(assignee_agent_id)},
        )

    # Rate limit before the ownership lookups (not after, per
    # start_conversation's established ordering; TECH-5094 Argus round 1,
    # code quality/S15): once OwnershipClient is swapped for a real HTTP
    # call to the reclaw platform, an unadmitted caller could otherwise
    # force two external round-trips per request before ever being capped.
    await _enforce_task_create_rate_limit(session, actor_sub=actor_sub, creator=creator)

    try:
        creator_owner_info = await ownership_client.get_agent_owners(creator.id)
        assignee_owner_info = await ownership_client.get_agent_owners(assignee.id)
    except Exception as exc:
        # Full exception + traceback go to the server-side log only (never
        # the audit_log row below -- see its comment) so an ownership-
        # lookup outage is still diagnosable in CloudWatch instead of being
        # silently indistinguishable from a legitimate denial (TECH-5094
        # Argus round 4). Logged before _deny(), which raises.
        logger.error("ownership_unverified: %s", type(exc).__name__, exc_info=True)
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.ownership_unverified",
            agent_id=creator.id,
            detail={
                "assignee_agent_id": str(assignee.id),
                # ONLY the exception's type name — never str(exc)/repr(exc)
                # (TECH-5094 Argus rounds 2-3, security): AgentTableOwnershipClient's
                # LookupError carries only benign agent UUIDs today, but a
                # future HTTP-backed OwnershipClient's exceptions routinely
                # embed Authorization headers, full request URLs with token
                # query params, and raw response bodies in both str() and
                # repr() — a length-truncated str(exc) does not bound where
                # a credential falls inside that string, so nothing but the
                # type name is safe to persist into the append-only
                # audit_log ahead of that swap.
                "error_type": type(exc).__name__,
            },
        )
    creator_owners = set(creator_owner_info.get("owners") or [])
    assignee_owners = set(assignee_owner_info.get("owners") or [])
    if not creator_owners or not assignee_owners:
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.ownership_unverified",
            agent_id=creator.id,
            detail={"assignee_agent_id": str(assignee.id)},
        )
    if not may_assign(creator_owners, assignee_owners):
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.not_same_owner",
            agent_id=creator.id,
            detail={
                "creator_is_shared": bool(creator_owner_info.get("is_shared")),
                "assignee_is_shared": bool(assignee_owner_info.get("is_shared")),
                "matched": False,
            },
        )

    try:
        normalized = validate_payload(TASK_NAMESPACE, "task_spec", schema_version, task)
    except PayloadValidationError as exc:
        await _deny_bad_schema(
            session,
            actor_sub=actor_sub,
            agent_id=creator.id,
            conversation_id=None,
            message_type="task_spec",
            exc=exc,
        )

    related_conversation_id = normalized.get("related_conversation_id")
    if related_conversation_id is not None:
        related = await _find_participant(session, uuid.UUID(related_conversation_id), creator.id)
        # Must currently be active, not merely "has a participant row" —
        # a left/declined former participant no longer belongs to the
        # conversation (same policy _load_participant_for_read enforces
        # for reads; TECH-5094 Argus round 1, authorization/B2).
        if related is None or related.status != "active":
            await _deny_bad_schema(
                session,
                actor_sub=actor_sub,
                agent_id=creator.id,
                conversation_id=None,
                message_type="task_spec",
                exc=PayloadValidationError(
                    "payload failed schema validation: related_conversation_id does not "
                    "reference a conversation the caller belongs to"
                ),
            )

    new_task = Task(
        created_by=creator.id,
        assignee_id=assignee.id,
        status="open",
        schema_version=schema_version,
        payload=normalized,
    )
    session.add(new_task)
    await session.flush()
    _audit(
        session,
        actor_sub=actor_sub,
        action="task.create",
        agent_id=creator.id,
        task_id=new_task.id,
        detail={"assignee_agent_id": str(assignee.id)},
    )
    await session.commit()
    # Build the same canonical AXI shape get_tasks returns for this
    # resource (TECH-5094 Argus round 1, api contract/S5) directly from
    # the creator/assignee Agent rows already loaded above — no second
    # query, no second service call from the tools layer (round 2,
    # architecture: providers/comms.py must call exactly one service.py
    # function per tool), and no separately-exported, unauthorized
    # formatter for a caller to reach for by mistake (round 2, security/
    # B1's IDOR fix supersedes the earlier get_task_public seam entirely
    # rather than patching it). The caller is trivially the task's own
    # creator here, so no additional visibility check is needed.
    return _task_public(
        new_task, caller_agent_id=creator.id, created_by_sub=creator.sub, assignee_sub=assignee.sub
    )


def _parse_task_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        iso_part, id_part = cursor.split("|", 1)
        return datetime.fromisoformat(iso_part), uuid.UUID(id_part)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid_request: malformed cursor {cursor!r}") from exc


async def get_tasks(
    session: AsyncSession,
    *,
    caller_agent_id: uuid.UUID,
    role: str = "all",
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Paginated "visible to me" task list: caller is ``created_by`` OR ``assignee_id``.

    No other agent's tasks are ever visible, including same-owner
    siblings (an owner-wide view is a later extension, not v1). Read-only
    with no denial path for a registered caller (no audit rows — same as
    ``inbox``/``list_agents``). ``role`` narrows to ``"created"``/
    ``"assigned"`` (relative to the caller) or ``"all"`` (default, OR of
    both); ``status`` optionally narrows further. Keyset-paginated over
    ``(created_at DESC, id DESC)`` — ``cursor`` is an opaque
    ``"{iso_created_at}|{task_id}"`` string from a previous page's
    ``next_cursor``.
    """
    limit = max(1, min(limit, 200))
    if role == "assigned":
        role_filter = Task.assignee_id == caller_agent_id
    elif role == "created":
        role_filter = Task.created_by == caller_agent_id
    elif role == "all":
        role_filter = or_(Task.assignee_id == caller_agent_id, Task.created_by == caller_agent_id)
    else:
        raise ValueError(f"invalid role {role!r} — expected 'assigned', 'created', or 'all'")

    creator_agent = aliased(Agent)
    assignee_agent = aliased(Agent)
    stmt = (
        select(Task, creator_agent.sub, assignee_agent.sub)
        .join(creator_agent, creator_agent.id == Task.created_by)
        .join(assignee_agent, assignee_agent.id == Task.assignee_id)
        .where(role_filter)
    )
    count_stmt = select(func.count()).select_from(Task).where(role_filter)
    if status is not None:
        stmt = stmt.where(Task.status == status)
        count_stmt = count_stmt.where(Task.status == status)
    if cursor:
        cursor_created_at, cursor_id = _parse_task_cursor(cursor)
        stmt = stmt.where(tuple_(Task.created_at, Task.id) < (cursor_created_at, cursor_id))
    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    total_count = (await session.execute(count_stmt)).scalar_one()

    tasks = [
        _task_public(
            t,
            caller_agent_id=caller_agent_id,
            created_by_sub=created_by_sub,
            assignee_sub=assignee_sub,
        )
        for t, created_by_sub, assignee_sub in rows
    ]
    next_cursor = None
    if has_more and rows:
        last_task = rows[-1][0]
        next_cursor = f"{last_task.created_at.isoformat()}|{last_task.id}"
    return {
        "tasks": tasks,
        "total_count": total_count,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


async def _find_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()


async def _deny_bad_task_state(
    session: AsyncSession,
    *,
    actor_sub: str,
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_status: str,
    attempted_status: str,
) -> NoReturn:
    """Audit + raise the specific (non-uniform) task-status transition violation."""
    _audit(
        session,
        actor_sub=actor_sub,
        action="denied.bad_state",
        agent_id=agent_id,
        task_id=task_id,
        detail={"status": current_status, "attempted_status": attempted_status},
    )
    await session.commit()
    raise InvalidConversationStateError(
        f"task cannot transition to '{attempted_status}' while its status is '{current_status}'"
    )


async def update_task(
    session: AsyncSession,
    *,
    actor_sub: str,
    caller_agent_id: uuid.UUID,
    task_id: uuid.UUID,
    status: str,
) -> dict[str, Any]:
    """Transition a task: ``open`` -> ``done`` (either party) or ``open`` ->
    ``declined`` (assignee only — the consent/refusal mechanism, terminal).

    TECH-5099. Only the task's creator or assignee may call this (uniform
    ``AccessDeniedError`` for a non-party or an unknown task_id — anti-
    enumeration, identical treatment to a non-existent task). ``declined``
    is further restricted to the assignee; the creator attempting it gets
    the same uniform denial. No transition out of a terminal status
    (``done``/``declined``) is legal — that is a state-machine violation
    (``InvalidConversationStateError``, specific: the caller already knows
    the task's current status via ``get_tasks``), not an authorization one.

    Raises ``ValueError`` for a ``status`` this tool never accepts (``open``
    is only ever written by ``add_task``; anything outside
    ``{"done", "declined"}`` is malformed input).
    """
    if status not in ("done", "declined"):
        raise ValueError(f"status must be 'done' or 'declined', got {status!r}")

    task = await _find_task(session, task_id)
    is_creator = task is not None and task.created_by == caller_agent_id
    is_assignee = task is not None and task.assignee_id == caller_agent_id
    if task is None or not (is_creator or is_assignee):
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.not_party",
            agent_id=caller_agent_id,
            task_id=task.id if task is not None else None,
            detail={"attempted_task_id": str(task_id)},
        )
    if status == "declined" and not is_assignee:
        await _deny(
            session,
            actor_sub=actor_sub,
            action="denied.not_assignee",
            agent_id=caller_agent_id,
            task_id=task.id,
            detail={"attempted_status": status},
        )
    if task.status != "open":
        await _deny_bad_task_state(
            session,
            actor_sub=actor_sub,
            agent_id=caller_agent_id,
            task_id=task.id,
            current_status=task.status,
            attempted_status=status,
        )

    task.status = status
    _audit(
        session,
        actor_sub=actor_sub,
        action="task.update_status",
        agent_id=caller_agent_id,
        task_id=task.id,
        detail={"from": "open", "to": status},
    )
    await session.commit()
    # updated_at's onupdate=text("now()") is computed server-side on the
    # UPDATE just committed -- SQLAlchemy marks it expired regardless of
    # expire_on_commit, so a plain attribute read would trigger an async
    # lazy-load outside any await (MissingGreenlet). Refresh explicitly.
    await session.refresh(task)

    creator_agent = aliased(Agent)
    assignee_agent = aliased(Agent)
    created_by_sub, assignee_sub = (
        await session.execute(
            select(creator_agent.sub, assignee_agent.sub)
            .select_from(Task)
            .join(creator_agent, creator_agent.id == Task.created_by)
            .join(assignee_agent, assignee_agent.id == Task.assignee_id)
            .where(Task.id == task.id)
        )
    ).one()
    return _task_public(
        task,
        caller_agent_id=caller_agent_id,
        created_by_sub=created_by_sub,
        assignee_sub=assignee_sub,
    )


__all__ = [
    "CONVERSATION_TTL",
    "MAX_CONVERSATION_STARTS_PER_HOUR",
    "MAX_MESSAGES_PER_CONVERSATION_PER_HOUR",
    "MAX_TASK_CREATES_PER_HOUR",
    "AgentTableOwnershipClient",
    "OwnershipClient",
    "accept_invite",
    "add_task",
    "decline_invite",
    "get_agent_by_sub",
    "get_conversation",
    "get_tasks",
    "inbox",
    "invite",
    "leave",
    "list_agents",
    "may_assign",
    "may_invite",
    "may_open",
    "post_message",
    "register_agent",
    "start_conversation",
    "update_task",
]
