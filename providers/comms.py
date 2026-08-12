"""Comms provider — the MCP tool surface over ``service.py`` (DESIGN.md §7).

Every tool below follows the same shape:

1. Resolve the caller's identity via ``get_access_token()`` — never from
   tool arguments. ``try_resolve_email`` is the single source of truth for
   "who is calling" (rh-auth: raw ``sub`` claim; Okta: email/sub) and is
   used here as the ``actor_sub``/``Agent.sub`` key uniformly, matching
   ``comms_whoami``'s existing identity resolution.
2. Resolve that identity to a board ``Agent`` row via
   ``service.get_agent_by_sub`` (every tool except ``comms_register``,
   which establishes that mapping in the first place). A caller with a
   valid token/scope who has never called ``comms_register`` gets a
   distinct, explicit error — this is about the caller's OWN registration
   state, not conversation access, so it is fine to be specific (unlike
   ``AccessDeniedError``, which must stay uniform).
3. Open one DB session (``db.get_session_factory``) and call exactly one
   ``service.py`` function.
4. Map the three service-layer exception shapes (``exceptions.py``) plus
   ``schemas.PayloadValidationError`` to ``fastmcp.exceptions.ToolError``.
   ``AccessDeniedError``'s message is passed through UNCHANGED (it is
   already the fixed, anti-enumeration-safe string) — never wrapped or
   annotated, which could otherwise leak which denial branch fired.
5. Return an AXI-shaped dict: compact fields, ``total_count``/``has_more``
   where relevant, explicit empty states (never a bare empty list/None).

Registration reminder (fail-closed ``TOOL_SCOPES``, see scopes.py): every
tool added here MUST be enrolled in ``scopes.TOOL_SCOPES`` under its
mounted name (``comms_<tool>``) in the same change, or rh-auth callers can
never reach it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

import service
from db import get_session_factory
from exceptions import AccessDeniedError, InvalidConversationStateError, RateLimitExceededError
from identity import try_resolve_email
from models import Agent
from schemas import MAX_PARTICIPANTS_PER_CONVERSATION, PayloadValidationError
from scopes import is_interactive_token, scopes_for_token

comms_server: FastMCP[Any] = FastMCP("comms")


# --- Identity / session plumbing -------------------------------------------------


def _require_token() -> AccessToken:
    """Fetch the verified access token, or raise if dispatch happened anyway.

    Defense in depth, matching ``comms_whoami``: ``ScopeEnforcementMiddleware``
    should never dispatch an unauthenticated call, but a tool body must not
    silently proceed with a ``None`` token if it ever does.
    """
    token = get_access_token()
    if token is None:
        raise ToolError("no access token provided")
    return token


def _require_identity(token: AccessToken) -> str:
    """Resolve the caller's board identity (``Agent.sub``) from the token.

    Uses ``identity.try_resolve_email`` — the same resolver ``comms_whoami``
    reports as ``identity`` — so the string used as ``Agent.sub`` here is
    identical, per caller, to what every other tool (and the audit trail's
    ``actor_sub``) sees. ``try_resolve_email`` fails open with ``None`` on a
    malformed token (see its docstring); that must not silently become an
    empty-string identity here.
    """
    identity = try_resolve_email(token)
    if identity is None:
        raise ToolError("unable to resolve caller identity from token claims")
    return identity


async def _resolve_caller_agent(session: Any, sub: str) -> Agent:
    """Look up the caller's board ``Agent`` row, or raise a clear, specific error.

    Distinct from ``AccessDeniedError`` on purpose: "you have a valid
    token/scope but never called comms_register" is a fact about the
    caller's own state, not an enumeration risk about someone else's
    conversation, so DESIGN.md's uniform-denial rule does not apply here.
    """
    agent = await service.get_agent_by_sub(session, sub)
    if agent is None:
        raise ToolError(
            "not_registered: no board agent is bound to this caller yet — call comms_register first"
        )
    return agent


@asynccontextmanager
async def _map_service_errors() -> AsyncIterator[None]:
    """Translate the service layer's exception shapes into ``ToolError``.

    ``AccessDeniedError``'s message is the fixed, uniform, anti-enumeration
    string (exceptions.py) and is passed through verbatim — no prefix, no
    detail, nothing that could distinguish denial causes to the caller.
    The next three shapes are already client-safe/specific by design
    (state-machine violations, rate limits, and payload validation are not
    enumeration risks — see exceptions.py's module docstring), so their
    messages pass through unwrapped too.

    A bare ``ValueError`` is different: the service layer raises it for
    internal parameter-shape problems (e.g. an unrecognized
    ``conversation_type``) and its message text can embed internal
    schema/config detail (allowed-value lists, etc.). Those are not
    client-safe, so they are mapped to a single generic, non-leaking
    message instead of being forwarded verbatim.
    """
    try:
        yield
    except AccessDeniedError as exc:
        raise ToolError(str(exc)) from None
    except (
        InvalidConversationStateError,
        RateLimitExceededError,
        PayloadValidationError,
    ) as exc:
        raise ToolError(str(exc)) from None
    except ValueError:
        raise ToolError("invalid_request: the request could not be processed") from None


# --- Parsing helpers --------------------------------------------------------------


def _parse_uuid(field: str, value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolError(f"invalid_request: {field} is not a valid UUID: {value!r}") from exc


def _parse_uuids(field: str, values: Iterable[str]) -> list[uuid.UUID]:
    return [_parse_uuid(field, v) for v in values]


def _parse_expires_at(value: str | None) -> datetime | None:
    """Parse an optional ISO 8601 ``expires_at`` override, rejecting naive datetimes."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            f"invalid_request: expires_at is not a valid ISO 8601 datetime: {value!r}"
        ) from exc
    if dt.tzinfo is None:
        raise ToolError("invalid_request: expires_at must be timezone-aware (include a UTC offset)")
    return dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# --- Identity tool (existing) ------------------------------------------------------


@comms_server.tool
async def whoami() -> dict[str, Any]:
    """Return the authenticated caller's identity, issuer, caller type, and scopes.

    Diagnostic tool: use it to verify that auth (Okta OIDC for humans,
    rh-auth Bearer JWT for agents) and scope enforcement are wired
    correctly. ``scopes`` is the rh-auth ``scopes`` claim for service
    callers; empty for interactive Okta callers (who bypass scope checks).
    """
    token = _require_token()
    interactive = is_interactive_token(token)
    return {
        "identity": try_resolve_email(token),
        "issuer": token.claims.get("iss"),
        "caller_type": "interactive" if interactive else "service",
        "scopes": scopes_for_token(token),
    }


# --- Board admission ---------------------------------------------------------------


@comms_server.tool
async def register(display_name: str, accepted_types: list[str]) -> dict[str, Any]:
    """Idempotently self-provision (or re-bind) the caller's board ``Agent`` row.

    ``owner_sub``/``owner_email`` are NEVER accepted as parameters
    (DESIGN.md §4 security invariant) — they are derived here from verified
    token claims only:

    - ``owner_sub``: the token's ``owner_sub`` claim if present (an rh-auth
      token minted for an EA agent acting on a human's behalf), else the
      caller's own resolved identity (self-owned — e.g. a human calling
      this directly, or an agent token with no distinct owner claim).
    - ``owner_email``: the token's ``owner_email`` claim if present, else
      — ONLY for interactive (Okta) callers — its upstream-verified
      ``email`` claim, else the caller's own identity as a last resort so
      the column is always populated with something attributable rather
      than a placeholder.

      The ``email`` claim fallback is gated on ``is_interactive_token``
      (``scopes.py``). For a token with no ``iss`` at all, that check and
      ``identity``'s internal ``_is_rh_auth_token`` both land on "don't
      trust the ``email`` claim" — but via different mechanisms, not a
      shared rule: ``is_interactive_token`` treats missing/``None`` ``iss``
      as simply "not confirmed interactive" (an unknown/deny outcome — it
      only decides whether to bypass scope checks, making no claim about
      identity), whereas ``_is_rh_auth_token`` affirmatively treats
      missing/``None`` ``iss`` as rh-auth-like (an assume-rh-auth outcome —
      it feeds identity resolution, so it conservatively pins the caller's
      identity to ``sub`` and never trusts ``email``/``preferred_username``).
      Do not "harmonize" these two checks into one shared helper on the
      assumption that they encode the same rule — an rh-auth (agent)
      token's extra claims are caller-supplied and unverified (the
      ``rh-auth issue`` CLI accepts arbitrary ``--sub`` and extra claims),
      so ``email`` must never be trusted as an ``owner_email`` fallback for
      those tokens, regardless of which check is used to detect them.

    Calling again with the same caller identity re-binds ``display_name``/
    ``accepted_types`` in place (see ``service.register_agent``).
    """
    token = _require_token()
    sub = _require_identity(token)
    owner_sub = str(token.claims.get("owner_sub") or sub)
    upstream_email = token.claims.get("email") if is_interactive_token(token) else None
    owner_email = str(token.claims.get("owner_email") or upstream_email or sub)

    async with get_session_factory()() as session, _map_service_errors():
        agent = await service.register_agent(
            session,
            sub=sub,
            owner_sub=owner_sub,
            owner_email=owner_email,
            display_name=display_name,
            accepted_types=accepted_types,
        )

    return {
        "agent_id": str(agent.id),
        "sub": agent.sub,
        "display_name": agent.display_name,
        "accepted_types": list(agent.accepted_types),
        "status": agent.status,
        "owner_email": agent.owner_email,
    }


@comms_server.tool
async def list_agents(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    """List the board directory (paginated, keyset on ``sub``).

    Internal domain — enumeration is acceptable per DESIGN.md §10. Pass the
    returned ``next_cursor`` back as ``cursor`` to page forward.
    """
    _require_token()
    async with get_session_factory()() as session:
        return await service.list_agents(session, limit=limit, cursor=cursor)


# --- Conversation lifecycle ---------------------------------------------------------


@comms_server.tool
async def start_conversation(
    conversation_type: str,
    target_agent_ids: list[str],
    initial_message: dict[str, Any],
    message_type: str = "availability_request",
    expires_at: str | None = None,
    schema_version: Literal[1] = 1,
) -> dict[str, Any]:
    """Open a conversation with N other agents, posting the seq-1 message.

    ``target_agent_ids`` are agent ids (UUID strings, e.g. from
    ``comms_list_agents``), capped at ``schemas.MAX_PARTICIPANTS_PER_CONVERSATION``
    entries — a caller submitting more is rejected outright rather than
    paying for an unbounded number of participant inserts and audit-log
    writes. The caller becomes the ``owner`` participant; every target is
    added as ``invited`` (not visible until they call ``comms_accept``).
    ``expires_at``, if given, must be a timezone-aware ISO 8601 datetime
    string; omit it to use the default 7-day TTL. ``schema_version`` is
    explicit rather than an invisible default — only ``1`` exists today.
    """
    token = _require_token()
    sub = _require_identity(token)
    if len(target_agent_ids) > MAX_PARTICIPANTS_PER_CONVERSATION:
        raise ToolError(
            "invalid_request: target_agent_ids exceeds the participant cap "
            f"({MAX_PARTICIPANTS_PER_CONVERSATION})"
        )
    target_uuids = _parse_uuids("target_agent_ids", target_agent_ids)
    expires_dt = _parse_expires_at(expires_at)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            conversation = await service.start_conversation(
                session,
                actor_sub=sub,
                initiator_agent_id=caller.id,
                conversation_type=conversation_type,
                target_agent_ids=target_uuids,
                initial_message=initial_message,
                message_type=message_type,
                expires_at=expires_dt,
                schema_version=schema_version,
            )

    return {
        "conversation_id": str(conversation.id),
        "type": conversation.type,
        "state": conversation.state,
        "created_by": str(conversation.created_by),
        "target_agent_ids": [str(t) for t in target_uuids],
        "expires_at": _iso(conversation.expires_at),
        "created_at": _iso(conversation.created_at),
    }


@comms_server.tool
async def post_message(
    conversation_id: str,
    message_type: str,
    payload: dict[str, Any],
    schema_version: Literal[1] = 1,
) -> dict[str, Any]:
    """Post a typed, schema-validated message to an active conversation.

    Requires the caller to be a currently-``active`` participant (uniform
    denial otherwise — identical whether the conversation doesn't exist,
    the caller was never invited, is still ``invited``, or has
    left/declined). ``confirm`` completes the conversation; ``decline``
    marks the sender declined and may cancel the conversation if every
    other member has also declined. ``schema_version`` is explicit rather
    than an invisible default — only ``1`` exists today.
    """
    token = _require_token()
    sub = _require_identity(token)
    conv_id = _parse_uuid("conversation_id", conversation_id)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            message = await service.post_message(
                session,
                actor_sub=sub,
                sender_agent_id=caller.id,
                conversation_id=conv_id,
                message_type=message_type,
                payload=payload,
                schema_version=schema_version,
            )

    return {
        "conversation_id": conversation_id,
        "seq": message.seq,
        "type": message.type,
        "schema_version": message.schema_version,
        "payload": message.payload,
        "created_at": _iso(message.created_at),
    }


@comms_server.tool
async def get_conversation(conversation_id: str, since_seq: int = 0) -> dict[str, Any]:
    """Combined read: conversation + participants + messages since ``since_seq``.

    An ``invited`` (not yet accepted) caller gets metadata only — no
    message content, and ``since_seq`` is ignored. An ``active`` caller
    gets full history from ``since_seq`` onward, and their read cursor
    advances. Non-members (and left/declined former members) get the
    uniform denial, identical to a non-existent conversation. ``since_seq``
    must be non-negative — a negative value would silently widen the
    result window in an unintended way.

    The returned ``messages_returned`` count is the size of the returned
    (post-``since_seq``-filter) slice, NOT the conversation's total
    message count — deliberately not named ``total_count`` to avoid
    implying otherwise.
    """
    token = _require_token()
    sub = _require_identity(token)
    conv_id = _parse_uuid("conversation_id", conversation_id)
    if since_seq < 0:
        raise ToolError("invalid_request: since_seq must be >= 0")

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            result = await service.get_conversation(
                session,
                actor_sub=sub,
                caller_agent_id=caller.id,
                conversation_id=conv_id,
                since_seq=since_seq,
            )

    if "total_count" in result:
        result["messages_returned"] = result.pop("total_count")
    return result


@comms_server.tool
async def inbox() -> dict[str, Any]:
    """Return the caller's unread active conversations plus pending invites.

    Always returns the same three keys (``unread``, ``pending_invites``,
    ``total_count``), even when both lists are empty — an explicit
    "nothing needs your attention" rather than an ambiguous bare empty list.
    """
    token = _require_token()
    sub = _require_identity(token)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        return await service.inbox(session, caller_agent_id=caller.id)


@comms_server.tool
async def accept(conversation_id: str) -> dict[str, Any]:
    """Accept a pending invite: flips the caller's status ``invited`` → ``active``.

    Grants full history read and posting rights from this point forward.
    Requires the caller to currently be ``invited`` on this conversation
    (uniform denial otherwise).
    """
    token = _require_token()
    sub = _require_identity(token)
    conv_id = _parse_uuid("conversation_id", conversation_id)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            participant = await service.accept_invite(
                session,
                actor_sub=sub,
                agent_id=caller.id,
                conversation_id=conv_id,
            )

    return {
        "conversation_id": conversation_id,
        "agent_id": str(participant.agent_id),
        "status": participant.status,
        "role": participant.role,
        "joined_at": _iso(participant.joined_at),
    }


@comms_server.tool
async def decline_invite(conversation_id: str) -> dict[str, Any]:
    """Decline a pending invite. Terminal — no access is ever granted.

    Requires the caller to currently be ``invited`` on this conversation
    (uniform denial otherwise). Distinct from ``comms_leave``, which covers
    already-``active`` members, so the audit trail keeps the two actions
    separate.
    """
    token = _require_token()
    sub = _require_identity(token)
    conv_id = _parse_uuid("conversation_id", conversation_id)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            await service.decline_invite(
                session,
                actor_sub=sub,
                agent_id=caller.id,
                conversation_id=conv_id,
            )

    return {"conversation_id": conversation_id, "agent_id": str(caller.id), "status": "declined"}


@comms_server.tool
async def invite(conversation_id: str, target_agent_id: str) -> dict[str, Any]:
    """Invite another board agent into an active conversation.

    Requires the caller to currently be an ``active`` participant (v1: any
    active member may invite, not just the owner). The target must be a
    board-active agent accepting this conversation's type, and must not
    already have a participant row in any status — uniform denial for
    every failure mode.
    """
    token = _require_token()
    sub = _require_identity(token)
    conv_id = _parse_uuid("conversation_id", conversation_id)
    target_id = _parse_uuid("target_agent_id", target_agent_id)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            participant = await service.invite(
                session,
                actor_sub=sub,
                inviter_agent_id=caller.id,
                conversation_id=conv_id,
                target_agent_id=target_id,
            )

    return {
        "conversation_id": conversation_id,
        "target_agent_id": str(participant.agent_id),
        "status": participant.status,
        "invited_by": str(participant.invited_by) if participant.invited_by else None,
    }


@comms_server.tool
async def leave(conversation_id: str) -> dict[str, Any]:
    """Leave a conversation: caller's participant status → ``left``.

    Requires the caller to currently be ``active``. Pure exit bookkeeping —
    to decline a negotiation with cascade-to-``canceled`` semantics, post a
    ``decline`` message via ``comms_post_message`` instead.
    """
    token = _require_token()
    sub = _require_identity(token)
    conv_id = _parse_uuid("conversation_id", conversation_id)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            await service.leave(
                session,
                actor_sub=sub,
                agent_id=caller.id,
                conversation_id=conv_id,
            )

    return {"conversation_id": conversation_id, "agent_id": str(caller.id), "status": "left"}


# --- Tasks (internal.coordination, TECH-5094) -----------------------------------


@comms_server.tool
async def add_task(
    assignee_agent_id: str,
    task: dict[str, Any],
    schema_version: Literal[1] = 1,
) -> dict[str, Any]:
    """Create a task assigned from the caller to ``assignee_agent_id``.

    Bidirectional: either party of an admitted pair may call this — a
    Chief-of-Staff agent assigning work down, or an EA agent reporting
    status back up (a ``report_status``-action task). Admission is gated
    solely by the two agents' verified owner sets intersecting (never by
    ``agents.owner_sub``/``owner_email`` directly) — uniform denial on
    mismatch or on an ownership-lookup failure (fails closed).
    ``assignee_agent_id`` is an agent id (UUID string, e.g. from
    ``comms_list_agents``). ``task`` must validate against the
    ``TaskSpecV1`` schema (no free text). ``schema_version`` is explicit
    rather than an invisible default — only ``1`` exists today.
    """
    token = _require_token()
    sub = _require_identity(token)
    assignee_uuid = _parse_uuid("assignee_agent_id", assignee_agent_id)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        async with _map_service_errors():
            new_task = await service.add_task(
                session,
                actor_sub=sub,
                creator_agent_id=caller.id,
                assignee_agent_id=assignee_uuid,
                task=task,
                ownership_client=service.AgentTableOwnershipClient(session),
                schema_version=schema_version,
            )

    return {
        "task_id": str(new_task.id),
        "status": new_task.status,
        "created_by": str(new_task.created_by),
        "assignee_agent_id": str(new_task.assignee_id),
        "payload": new_task.payload,
        "schema_version": new_task.schema_version,
        "created_at": _iso(new_task.created_at),
    }


@comms_server.tool
async def get_tasks(
    role: Literal["assigned", "created", "all"] = "all",
    status: Literal["open", "done", "declined"] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List tasks visible to the caller: where the caller is creator or assignee.

    No other agent's tasks are ever visible or enumerable, including
    same-owner siblings. ``role`` narrows to ``"created"``/``"assigned"``
    (relative to the caller) or ``"all"`` (default). ``status`` optionally
    narrows further. Pass the returned ``next_cursor`` back as ``cursor``
    to page forward (keyset, newest first).
    """
    token = _require_token()
    sub = _require_identity(token)

    async with get_session_factory()() as session:
        caller = await _resolve_caller_agent(session, sub)
        return await service.get_tasks(
            session,
            caller_agent_id=caller.id,
            role=role,
            status=status,
            limit=limit,
            cursor=cursor,
        )
