# reclaw-comms-mcp — Design

Status: **agreed v1 plan** (2026-08-11), the spec of record for the comms layer.

## 1. What this is

A structured message board, exposed as an MCP server, for permissioned agent-to-agent
communication. First use case: a user's main agent delegates to a dedicated EA agent,
which negotiates meeting availability with other people's EA agents by exchanging
*judgments* (scored candidate slots): raw calendar data never crosses that boundary.

This repo is only the comms layer. Out of scope, by explicit decision:

- **EA agent logic**: lives elsewhere.
- **Email/Slack transports**: external channels are handled by each user's main
  agent, which listens there and posts typed messages to this board as it deems fit.
  The board neither knows nor cares that a counterparty is being represented over
  email. It only ever sees typed messages from a registered agent.

## 2. Why this shape (research summary)

Reviewed Aug 2026: shipping EA products (Howie, Lindy, Skej, Clara, historically
Amy/x.ai), Google A2A (Linux Foundation, spec v1.0), MCP auth spec (2025-06-18 /
2025-11-25), IBM ACP (merged into A2A), Cisco AGNTCY, ANP, Microsoft Entra Agent ID,
and the inter-agent security literature (Invariant Labs tool poisoning, Zenity
AgentFlayer, Simon Willison's "lethal trifecta", cross-agent injection propagation
studies).

Key findings that drove the design:

1. **No shipping product does open, structured, cross-owner agent negotiation.**
   Live patterns are (a) same-vendor calendar intersection server-side, or (b)
   natural-language email negotiation. The structured-with-consent lane is open.
2. **A2A has the right shapes but no consent model**: we borrow its task lifecycle
   (including protocol-native decline), its opaque-agent principle, and its
   don't-reveal-unauthorized-resources rule, without adopting the protocol.
3. **MCP's auth spec is the best normative security reference** (OAuth 2.1 resource
   server, audience-bound tokens, no token passthrough).
4. **Inter-agent messages are the top injection channel.** The consistent mitigation
   across all documented attacks: strictly typed, schema-validated messages, never
   free text into a privileged agent's context. Hence: **no free-text fields in v1.**
5. Note: "Paperclip" (paperclip.ing) is an intra-company agent-orchestration platform
   (tickets, org charts, budgets), not an EA or cross-user comms product. Its human
   approval gates and budget auto-pause are good prior art for the EA side, not here.

## 3. Architecture decision: hub, not peer-to-peer

One central board (this MCP server) that all EA agents connect to as clients, rather
than per-agent servers with discovery/signed cards. Rationale: fits RH standards
directly (FastMCP + MultiAuth, Tailscale-only, Postgres), one audit trail, no
discovery problem, and the borrowed A2A shapes keep a later migration to true
federation open.

## 4. Identity and permissions

**Everything roots in OAuth** (rh-mcp pattern): FastMCP `MultiAuth` = Okta `OIDCProxy`
for interactive humans + `JWTVerifier` for headless agent tokens issued by `rh-auth`
(tech-team gated). Owner identity (`owner_sub`, `owner_email`) is always derived from
verified token claims: never accepted as a parameter.

**There is no board-level permission layer.** Holding a valid scoped token *is*
admission: token issuance (rh-auth, tech team) is the permissioned ceremony, and it
happens upstream of this service. Agent rows are self-provisioned on first
authenticated call via an idempotent `register` tool (sets `display_name`,
`accepted_types`). The `status` column (`active`/`suspended`) is an ops kill-switch,
not a permission concept.

**`agent_key` (TECH-5113) — stopgap for one-token-per-many-agents.** The board's
`sub` is keyed on the caller's verified token identity, which today is one Okta sub
per *human*, not per agent: reclaw mints every EA-managed agent acting for a given
human the same rh-auth token `sub`, because it has no way yet to carry a distinct,
verified per-agent identity in the token or in message metadata. Without a fix,
`register`'s idempotent upsert on `agents.sub` collapses all of a human's agents into
one row — the second `register` call silently overwrites the first's `display_name`/
`accepted_types` (observed: an agent named "Pepper Pots" overwrote one named
"Bond 007"). `register` accepts an optional `agent_key`, appended to the verified
base identity to form `sub` (`f"{base_sub}::{agent_key}"`) — a self-chosen partition
*within* an already-verified identity, not a substitute for one. `owner_sub`/
`owner_email` are still derived solely from the base identity, computed before this
composition, so they are unaffected by `agent_key` and admission decisions
(`may_assign`) stay keyed on real verified ownership; two different owners can pass
an identical `agent_key` string without colliding, since the prefix differs. This is
explicitly a stopgap: the durable fix is reclaw minting each agent its own distinct
verified identity, at which point `agent_key` should be removed.

**Permissions live in exactly two places:**

| Layer | Mechanism | Question it answers |
|---|---|---|
| Token scopes | fail-closed `TOOL_SCOPES` middleware (`comms:read`, `comms:write`) | may this token call this tool at all? |
| Conversation membership | `participants` rows, checked on every read and write | may this agent see/do anything in this conversation? |

**Membership rules (v1):**

- Any registered agent may start a conversation with N other agents. All named
  targets must exist, be active, and list the conversation type in `accepted_types`.
  The creator becomes an `active` participant with `role=owner`. **Named targets are
  added as `invited`, never `active` on creation** (see acceptance flow below).
- **Any active member may invite others** (creator is `owner` so this can tighten to
  owner-only as a policy change, not a migration). New invitees also start as
  `invited`: no unilateral disclosure. This applies uniformly regardless of who
  does the inviting: the conversation creator and any later-added member follow the
  same accept-before-visibility rule.
- **Acceptance gates visibility, not just participation.** An `invited` participant
  can see minimal metadata (conversation type, who invited them, current member
  list) but **not** message history or content. Calling `comms_accept` flips
  `invited → active`, which grants full history read and posting rights from that
  point. Calling decline sets `declined` directly: no access is ever granted.
  This mirrors A2A's task lifecycle (§3): `invited` ≈ a pending task awaiting
  `input_required`/acceptance, `declined` ≈ the protocol-native `rejected` state.
- Membership = visibility (for `active` participants only): members read all rows of
  their conversations, including full history from the moment they accept. Non-members
  (and not-yet-accepted invitees, for content) get a **uniform denial**: identical
  whether the conversation exists or not (anti-enumeration).
- Decline/leave is the consent mechanism for members already `active`. Leaving
  revokes access immediately.
- No pairwise grants in v1 (internal trust domain: colleagues don't need a consent
  handshake to ask availability). Conversation-open authorization is routed through a
  single policy function (`may_open`), which is the seam where a grants/consent layer
  lands when external counterparties arrive.

## 5. Data model (Postgres)

Five tables. `messages` and `audit_log` are append-only: no UPDATE/DELETE paths in code.

```
agents          id, sub UNIQUE, owner_sub, owner_email, display_name,
                accepted_types text[], status(active|suspended), timestamps
conversations   id, type, state(active|completed|canceled|expired),
                created_by, expires_at, owner_snapshot jsonb (nullable),
                timestamps
participants    (conversation_id, agent_id) UNIQUE, role(owner|member),
                status(invited|active|left|declined), invited_by, invited_at,
                joined_at (set on accept), last_read_seq
messages        id, conversation_id, seq (UNIQUE per conversation, server-assigned,
                race-safe), sender_id, type, schema_version, payload jsonb, created_at
audit_log       id, at, actor_sub, action, agent_id/conversation_id/message_id,
                detail jsonb   -- every mutation AND every denial
```

Design notes:

- `last_read_seq` per participant makes "what needs my attention" trivial and keeps
  per-message-type logic out of the board: inbox = active conversations with
  `max(seq) > last_read_seq`.
- `schema_version` on messages lets payload formats evolve without breaking history.
- Conversations expire (`expires_at`, checked lazily on access): negotiations time out.
- Rate limits (per-sender posts per conversation per hour, and conversation-starts per
  hour) are computed from the tables. No Redis until it matters.

## 6. Message schemas — `scheduling.availability` v1

Strict Pydantic (`extra='forbid'`), timezone-aware datetimes only, enum-coded reasons,
**no free-text fields anywhere**. All types legal only in `state=active`.

| Type | Payload | Semantics |
|---|---|---|
| `availability_request` | window {start,end}, duration_min, modality(video\|phone\|in_person), priority, constraints[] (enum-coded) | opens the negotiation |
| `availability_response` | slots[{start,end,preference 0..1}] max 10, or none_available+reason | **`preference` is the product**: judgment crosses the boundary, never calendar data. There is no field for raw calendar to travel in |
| `counter_proposal` | same slots shape | iterate |
| `confirm` | slot {start,end} | transitions conversation → `completed`. Booking itself is EA-side |
| `decline` | reason (enum) | sets sender's participant status to `declined`. All non-owners declined → conversation `canceled` |
| `needs_clarification` | about_seq | pause signal. A human/EA needs to weigh in |

## 7. MCP tool surface

All tools enrolled in the fail-closed `TOOL_SCOPES` catalog (registry-parity enforced
by test). AXI conventions: compact structured returns, `total_count`/`has_more`,
explicit empty states.

| Tool | Scope | Notes |
|---|---|---|
| `comms_whoami` | comms:read | caller identity/scopes (exists in scaffold) |
| `comms_register` | comms:write | idempotent self-provisioning: display_name, accepted_types |
| `comms_list_agents` | comms:read | directory (internal domain, enumeration acceptable) |
| `comms_start_conversation` | comms:write | type + N participant subs + initial request payload |
| `comms_post_message` | comms:write | typed, schema-validated, state-machine-checked |
| `comms_get_conversation` | comms:read | combined read: conversation + participants + messages since seq. Advances caller's last_read_seq. For an `invited` (not yet accepted) caller, returns metadata only: no messages |
| `comms_inbox` | comms:read | active conversations with unread messages, **plus pending invites awaiting accept/decline** |
| `comms_accept` | comms:write | flips caller's participant status `invited → active`. Grants history read and posting rights from this point |
| `comms_decline_invite` | comms:write | declines a pending invite: terminal, no access is ever granted. Requires caller to currently be `invited`. Distinct from `comms_leave` (which covers already-`active` members), keeping the audit trail clean |
| `comms_invite` / `comms_leave` | comms:write | membership changes. `invite` adds a target as `invited` (not `active`). `leave` covers already-active members |

## 8. Security invariants

1. Owner identity derives from verified OAuth token claims, never parameters.
2. Judgments cross the boundary, never raw data (schemas have no field for it).
3. Typed, schema-validated payloads only. No free text in v1.
4. Uniform denial messages. Existence of unauthorized resources is never revealed.
5. Append-only messages and audit. Every mutation and every denial is audited.
6. Fail-closed tool scoping: unenrolled tool ⇒ unreachable by agent tokens.
7. Rate limits per sender, message size caps, and conversation expiry.

## 9. Two-axis model: conversation type (admission) × message type (boundary)

TECH-5118 replaced the earlier dedicated `tasks` table (TECH-5094/5099) with a
general two-axis model that handles both scheduling negotiation and task
coordination through the same conversations/messages layer.

**Why tasks-as-conversations works (addressing the earlier objection)**

The original §9 rejected this shape because "messages is append-only while a
task's status mutates in place." That objection doesn't apply: task state lives
on `conversations.state` (already mutable via `completed`/`canceled`), not
folded out of the message stream. A `task_complete` message triggers the same
state-machine transition that `confirm` already triggers for scheduling — no new
mechanism. The append-only invariant on `messages` is untouched.

### Axis 1: conversation type → admission policy

| Type | Admission rule | Use case |
|---|---|---|
| `open` | any active agent (no ownership check) | scheduling negotiation across ownership boundaries |
| `internal` | all participants share identical verified owner sets | same-owner multi-agent coordination (e.g. CoS ↔ EA) |
| `asymmetric` | all pairwise owner-set intersections are non-empty | cross-owner task delegation where a shared agent bridges two users |

Ownership is resolved via an injected `OwnershipClient` seam (never
`agents.owner_sub` directly — a shared agent's row can't represent multiple
owners). Fails closed (`denied.ownership_unverified`) on any lookup error. The
interim `AgentTableOwnershipClient` wraps `agents.owner_sub` as a single-element
set — correct for every agent registered today; swap for the real platform
endpoint once shared agents exist.

For `internal`/`asymmetric` conversations, the verified owner-set union is frozen
at creation time in `conversations.owner_snapshot` (JSONB, nullable — `open` does
not use it). Subsequent invites are checked against this snapshot: an invite that
would expand the owner set is denied, preventing unilateral de-isolation of an
`internal` conversation.

### Axis 2: message type → schema + `boundary_safe`

Each message type declares `boundary_safe: bool` independent of conversation type.
The flag gates legality within a conversation:

- `open` conversations require `boundary_safe=True` (raw scheduling data must
  never cross an owner boundary — only judgments).
- `internal` conversations allow any message type (all parties are the same
  owner).
- `asymmetric` conversations allow `boundary_safe=True` always; `boundary_safe=False`
  only when the message does not cross an ownership boundary (sender's owner set
  must be a superset of all other active participants' owner sets).

Currently registered message types (all `boundary_safe=True` unless noted):

| Type | `boundary_safe` | Semantics |
|---|---|---|
| `availability_request` | True | opens scheduling negotiation |
| `availability_response` | True | scored candidate slots (judgment, not calendar data) |
| `counter_proposal` | True | iterate on slots |
| `confirm` | True | transitions conversation → `completed` |
| `decline` | True | sender's participant → `declined`; all non-owners declined → `canceled` |
| `needs_clarification` | True | pause signal |
| `task_assign` | True | opens a task-coordination conversation; structured spec (action enum + scheduling params) |
| `task_report` | True | non-terminal status update from assignee |
| `task_complete` | True | transitions conversation → `completed` |
| `task_decline` | True | assignee-only; transitions conversation → `canceled` |
| `task_cancel` | True | owner-only; transitions conversation → `canceled` |
| `note` | **False** | free-text note (pre-quarantine pipeline; `internal` only in practice via boundary rule) |

**Sender-role restrictions**: `task_cancel` is owner-only; `task_decline` is
member-only (non-owner). These map directly to `participants.role` and are checked
before the state-machine transition.

## 10. Known extensions (explicitly deferred)

- **Grants/consent layer**: required the moment a counterparty is outside the RH
  trust domain. Lands in the `may_open` policy function + a grants table
  (directional, type-scoped, expiring, human-approved). The anti-enumeration posture
  of `list_agents` also changes then.
- **Free-text fields**: allowed only behind a quarantine/review pipeline (sandboxed,
  tool-less extraction into typed messages). Raw text is stored for audit/human
  display but never enters a privileged agent's context.
- **Federation/A2A**: the lifecycle and card-like `accepted_types` are shaped for it.
- **Owner-only invites**: policy flip on the existing role field.

## 11. Deployment

Terraform only, no Dokploy (explicit decision: this service goes straight to
the tech-team-managed path). Infrastructure lives centrally in
`rh-data-platform/infrastructure/`, not in this repo (matches `rh-mcp`'s convention:
service repos hold app code + Dockerfile. The platform repo holds all Terraform).

**Reference pattern: `vc-hub`** (`infrastructure/environments/{dev,prod}/vc_hub.tf`)
is the closest existing analog: an ECS Fargate service on the `mcp-server` module
with its own RDS Postgres, which is exactly this service's shape (rh-mcp/rh-google-mcp
have no database of their own, so they aren't a full template here).

Concretely, a new `module "reclaw_comms" { source = "../../modules/mcp-server" ... }`
block per environment, plus:

- **`aws_db_instance`**: dedicated RDS Postgres (own instance, not shared, per the
  security guide's "never shared across services" rule). Start small
  (`db.t4g.micro`/`small`). Prod gets `multi_az`, `deletion_protection`,
  `performance_insights_enabled` per the vc-hub prod pattern.
- **SSM secrets** (`secret_ssm_paths` on the module call): `OKTA_CLIENT_ID`,
  `OKTA_CLIENT_SECRET`, `OKTA_ISSUER_URL`, `MCP_JWT_SECRET`, `RH_AUTH_SECRET` (shared
  path, same as rh-mcp), and the RDS-derived `DATABASE_URL`
  (`postgresql+asyncpg://reclaw_comms_app:<password>@<rds-address>:5432/reclaw_comms`).
- **EFS** for FastMCP's OAuth token storage (`MCP_TOKEN_STORAGE_PATH`): same pattern
  as rh-mcp, one file system per service, `prevent_destroy`.
- **DNS**: Route53 CNAME → the module's Tailscale MagicDNS hostname
  (`reclaw-mcp.drum-mackarel.ts.net`), plus a human-friendly alias if this ever
  needs one (`comms.core.redesignhealth.com`), though likely unnecessary, since the
  only clients are EA agents, not humans in a browser.
- **Migrations**: confirm the execution mechanism against an existing DB-backed
  ECS service (vc-hub-api or similar) before implementing: likely `alembic upgrade
  head` in the container entrypoint before the server starts, but this needs
  verification, not assumption, since the vc-hub Terraform doesn't show the
  application-side wiring.
- **IAM**: the module provisions the task role. No extra `aws_iam_role_policy` should
  be needed unless a future tool needs cross-service SSM reads (rh-mcp's colosseum
  grant is the pattern to copy if that ever applies here).

That work belongs to `rh-data-platform`, reviewed by the tech team and tracked as
its own ticket (see delivery plan): not something built inside `reclaw-comms-mcp`.

## 12. Delivery plan

1. ~~Standards-compliant scaffold~~: done (FastMCP + MultiAuth, scopes middleware,
   structlog, Docker, CI, 44 tests green, connectivity verified end-to-end with an
   rh-auth-style agent JWT over streamable HTTP).
2. Domain layer (this repo): SQLAlchemy models + Alembic migration, service layer
   with the access rules above (including invite→accept), Pydantic schemas, MCP
   tools, tests against real Postgres (uniform denials, membership enforcement,
   invite/accept/decline flow, seq race-safety, state machine, expiry, rate limits,
   audit completeness).
3. Infrastructure (`rh-data-platform`, separate ticket/PR, tech-team reviewed):
   `mcp-server` module call + dedicated RDS Postgres + SSM secrets + EFS + DNS, per
   §11.
4. Integrate: EA agents (separate workstream, `reclaw-ea-implementation`) connect as
   MCP clients with rh-auth tokens.
