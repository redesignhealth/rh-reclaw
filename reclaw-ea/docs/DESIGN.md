# reclaw-ea — Design: the EA agent

Status: **draft v1** (2026-08-11, platform binding resolved 2026-08-11) · Owner: Dan Costanza
Companions: [`reclaw-comms-mcp/docs/DESIGN.md`](https://github.com/redesignhealth/reclaw-comms-mcp)
(the board — spec of record for the comms layer),
[`rh-scheduler-mcp/docs/ea-scheduling-negotiation-and-autonomy.md`](https://github.com/redesignhealth/rh-scheduler-mcp)
(negotiation + autonomy design this doc builds on).
`rh-paperclip/docs/design-personal-agents-v3.md` is no longer a companion — Paperclip is discarded
as a hosting platform for reclaw agents (decided 2026-08-11); see §1a and open question 1.
v3's `responsibleUserId`-per-run-identity *concept* is still the right shape for credential
propagation, just implemented reclaw-natively rather than inherited from Paperclip.

## 1. What this is

The design for the **EA agent**: a dedicated scheduling agent, one per person, enclosed in that
person's agent environment in reclaw. The person's main agent delegates scheduling work to it;
it negotiates meeting times with other people's EA agents over the comms board, and with
EA-less humans via the main agent's email/Slack adapters. It owns judgment (what to offer, what
to concede, what to escalate) and commitment discipline (holds, confirms, booking). This repo is
the EA agent logic and nothing else.

**Division of the system, restated so nothing is built twice:**

| Component | Owns | Explicitly does NOT own |
|---|---|---|
| comms board (`reclaw-comms-mcp`) | typed message transport, conversation membership/visibility, message legality, the deterministic completion rule, audit of the wire | negotiation strategy, holds, booking, any judgment |
| scheduler mediator (`rh-scheduler-mcp`) | cross-person calendar computation under disclosure policy: intersection-only mutual availability, conflict checks, anti-probing limits | negotiation state, judgment, any calendar write |
| main agent (per person) | external transports (email/Slack in and out), the principal's approval surface, translating untrusted free text into typed board messages | scheduling judgment — it relays, the EA decides |
| **EA agent (this repo)** | negotiation state machine, slot scoring/preferences, holds + reservation ledger, confirm/booking discipline, the autonomy gate and everything it gates, escalation, learning from feedback | seeing another person's raw calendar (mediator's job), enforcing wire schemas (board's job) |

One consequence of the board's existence, recorded as a decision: the scheduling-negotiation
doc's open question 8 (mediator-vs-coordinator-vs-direct orchestration, "leaning: fold into the
mediator") is **resolved differently — negotiation runs EA-to-EA over the board.** The board
enforces message legality and the completion rule; each EA runs its own negotiation state
machine. The mediator stays exactly what `comms-architecture.md` designed: deterministic
calendar computation and disclosure, no negotiation state. v3's "stateful scheduler" section is
superseded to the same extent — state lives in the EAs and the board's append-only log, not in
the mediator.

## 1a. Platform and deployment shape (decided 2026-08-11)

**This is its own MCP server — `reclaw-ea-mcp` — not code folded into `rh-scheduler-mcp`, and not
hosted on Paperclip (discarded as a platform option).** Two decisions, made together:

- **Not inside the mediator.** `rh-scheduler-mcp`'s entire trust model rests on being neutral and
  judgment-free — both owners' EAs implicitly trust it *because* it holds nothing but calendar-
  intersection math, no rules, no autonomy state, no ledger. Its own docs are explicit about this
  ("the one component allowed to see both sides... so that neither calling agent has to";
  comms-architecture.md principle 4, "Mediators are deterministic services, not LLM agents"), and
  the negotiation doc that introduced the autonomy/negotiation/rules packages says outright this
  reasoning "lives in the EA, not in the mediator." That the `negotiation`/`autonomy`/`rules`/
  `counterparty_identity` Python packages physically sit inside `rh-scheduler-mcp`'s source tree
  today is an accident of where those tickets were filed (TECH-4944 etc.), not a decision that
  they should *run* there. Folding `Negotiator`'s per-owner state (ledger, approval holds,
  confidence/outcome history, rules) into the same service as the mediator would collapse the
  property that makes the mediator trustworthy to both sides at once.
- **Not Paperclip.** Paperclip is discarded as the hosting platform for reclaw agents. `reclaw-
  ea-mcp` deploys the same way its siblings do — FastMCP + `MultiAuth`, Tailscale-only ECS
  Fargate, one multi-tenant service (not one process per person), matching the board's own
  explicit "hub, not peer-to-peer" call and every other MCP service in this system.

**Shape:** `reclaw-ea-mcp` imports `scheduler_mcp.{negotiation,autonomy,rules,
counterparty_identity}` as a library dependency (already true today via `pyproject.toml`, pinned
to a `rh-scheduler-mcp` git SHA rather than a local path — see §9's provenance table and the
Argus-round-1 fix that replaced the original laptop-only path dependency; a versioned Gitea
release is the eventual target, not yet done), calls `rh-scheduler-mcp` as an MCP client for the
feasibility fast path (§2.3), calls `reclaw-comms-mcp` as an MCP client for the actual wire
messages, and exposes a small tool surface to the reclaw agent's own LLM loop —
`ea_negotiate`, `ea_react_to_conversation`, `ea_check_completion`, `ea_request_booking`,
`ea_respond_to_approval` — with `orchestrator.Negotiator` as the implementation behind them. The
LLM never touches `Negotiator`'s internals directly; it calls these tools, matching this repo's
existing "no LLM calls in this module" scope note (§2) generalized one level up: the LLM doesn't
call the board or the ledger directly either — it calls `reclaw-ea-mcp`, which does.

**Auth:** identical pattern to `reclaw-comms-mcp`/`rh-scheduler-mcp`, not a new one. `FastMCP
MultiAuth` — Okta `OIDCProxy` for interactive humans, an `rh-auth`-issued JWT (`JWTVerifier`) for
agent callers. `owner_identity` is derived from the verified token's claims on every call, never
accepted as a request parameter — the same anti-impersonation invariant `reclaw-comms-mcp/auth.py`
already enforces, load-bearing here specifically because a bug in it would let one owner's agent
read or spend another owner's ledger/approval/confidence history. Whatever hosts the reclaw
agent's own run loop needs to mint a per-owner, per-run token for each call — the same shape as
v3's `responsibleUserId`/`mintConnectionTokenForAgent` concept and `rh-scheduler-mcp`'s own
`mint_token_for_subject` (TECH-5043), implemented reclaw-natively now that Paperclip itself is
out of the picture. **`reclaw-ea-mcp` never needs a caller to present a different owner's
identity** — every tool call is "act on behalf of the token's own owner," full stop. Alice's EA
never authenticates as Bob or reads Bob's state from this service; Alice-talking-to-Bob happens
exclusively through typed messages on `reclaw-comms-mcp`. That keeps this service's trust boundary
as simple as the mediator's own.

## 2. The negotiation engine

### 2.1 State machine

Adopted wholesale from `rh-scheduler-mcp/src/scheduler_mcp/negotiation/` (built and tested,
TECH-4954–4962; currently unwired — this repo is where it gets wired):

- **Outcomes:** `in_progress | booked | no_agreement_possible | escalated_to_humans |
  stood_down_by_circuit_breaker` (`rounds.py`). `no_agreement_possible` ≠ `escalated_to_humans`:
  budget-exhausted-with-payload-assembled vs. a-human-was-actually-notified.
- **Round budget:** fixed global 3, not owner-tunable in v1 (asymmetric budgets read as rudeness
  to the other principal).
- **Transitions:** declarative allowlist (`transitions.py`), enforced at write time; derived
  needs-attention set computed as the complement of resolved states so a future outcome defaults
  to the safe side. maiea's `VALID_TRANSITIONS` doctrine, kept.
- **Identity:** `negotiation_id` is the board's `conversation_id` — stamped once at open, never
  recomputed from participants/subject (maiea #298 double-send lesson). The board's
  server-assigned `seq` is the ordering authority.
- **Circuit breaker:** identity-wide, separate from the round budget (`circuit_breaker.py`,
  ported from maiea TECH-1877: 15 sends / 5 min across all of one EA's negotiations → stand down
  every in-flight negotiation for that owner, alert once, terminal outcome
  `stood_down_by_circuit_breaker`).
- **Escalation ladder:** bounded retry then silence (`escalation_ladder.py`, 3 strikes) for
  classifier misses and unresponsive counterparties — never infinite nagging, never infinite
  silent retry.

### 2.2 Protocol semantics on the board (`scheduling.availability` v1)

The board carries `availability_request / availability_response / counter_proposal / confirm /
decline / needs_clarification`. EA-side semantics, agreed 2026-08-11:

1. **`confirm` is a per-participant commitment, not a completion trigger.** The conversation
   completes when every active participant's *latest* substantive message is a `confirm` naming
   the identical slot (board-side deterministic rule). A later `counter_proposal` or
   `needs_clarification` from any participant supersedes that participant's own standing confirm
   and resumes negotiation — this is the retract path when a slot dies mid-round, and it is how
   "reject" works without a dedicated message type. `decline` remains the nuclear option
   (participant exits).
2. **An EA posts `confirm` only after it has re-checked freebusy and promoted its hold to
   `booked` in its own ledger** (§4). By the time all confirms stand, every party is already
   committed and held — the remaining race is the calendar write itself.
3. **On completion, the conversation owner's EA books — but booking itself passes through the
   autonomy gate, decided 2026-08-11.** Completion (all confirms standing) is not itself
   authorization to write the calendar invite; it only makes the owner's EA *eligible* to request
   booking. The owner's EA evaluates the booking action through the gate (§5) exactly like any
   other gated action — `send_external_invite` if the counterparty is outside the trust domain
   (permanently gated, always `ask_first`, per §5); otherwise the applicable internal action type,
   subject to the same confidence-earned autonomy as everything else. On `ask_first`, the owner's
   EA opens an approval hold and books only once approved (`approve-must-advance`, §5) — the
   negotiation's own completion state does not change until then. Exactly one EA is ever eligible
   to book (the owner), or we rebuild the maiea/Howie duplicate-invite class; that EA simply may
   not book *unconditionally*. All other EAs accept the resulting invite and reconcile their
   ledgers once it lands.
4. **Completed conversations never reopen.** A failed booking write after completion escalates
   to the principal or opens a new conversation. The window is small by construction (point 2).
5. **Offer firmness:** slots in an `availability_response`/`counter_proposal` are held by the
   sender until superseded by the sender's own later message or the conversation's `expires_at`.
   (If per-slot `held_until` lands in the board schema later, it refines this; the convention is
   sufficient for v1.)
6. **`priority` in a request is a hint, never an instruction.** The receiving EA computes its own
   tier for the meeting from its principal's rules and counterparty identity (§3.3). Requester
   self-declared priority is one input; it cannot buy autonomy or a better slot by itself.

### 2.3 Mediator fast path

Before opening or while conducting a negotiation, an EA uses the mediator for feasibility:
`find_mutual_availability` / `check_conflicts` answer "what could work at all" in one call,
under the mediator's disclosure policy. The board exchange then carries what the mediator
cannot: each side's *judgment* (preference scores, willingness to move things). Rule of thumb:
**never spend a negotiation round discovering something the mediator answers deterministically.**
Two internal parties with mediator coverage should converge in one round in the common case —
same-network scheduling collapses toward lookup speed (the Blockit lesson), with rounds reserved
for genuine preference conflict.

## 3. Availability judgment — the preference scorer

This is the genuinely new build. Every prior system has the constraint half (what's impossible);
none has shipped the utility half (what's *good*). `preference: 0..1` on the wire is this
scorer's output.

### 3.1 Constraint layer (exists — wire it)

`rh-scheduler-mcp/rules/`: rule vocabulary (time windows, holidays as a date-level primitive,
meeting direction, type, organizer role, attendee count, recurrence, counterparty ref),
`FlexibilityRange = IMMOVABLE | SAME_DAY | SAME_WEEK | UNRESTRICTED`, hard rules with override
bars, **most-specific-matching-rule-wins** precedence, and shipped defaults (work hours,
hard-block, deep-work) that owners edit rather than author. Constraints filter the feasible set;
nothing below overrides a hard rule (§5).

### 3.2 Utility layer (new)

For each feasible slot, a deterministic score composed of:

- **Incumbent cost** — what is already there and how movable it is (`FlexibilityRange` of the
  incumbent's matched rule, organizer role, attendee count). A free slot costs 0; a slot over a
  `SAME_WEEK`-movable internal 1:1 costs little; over an `IMMOVABLE` incumbent it is infeasible,
  not merely expensive. This is Reclaim's key move — advertised availability may include slots
  over bumpable events, priced accordingly — with the caveat that offering such a slot to a
  counterparty requires the movability round (§6) to have succeeded, or the offer marks itself
  contingent.
- **Fragmentation cost** — Clockwise's objective: penalize slots that shatter the largest
  remaining focus block; prefer slots adjacent to existing meetings.
- **Time-of-day / energy fit** — owner-rule-derived (deep-work windows, "afternoons for external
  calls"), defaulting sensibly.
- **Buffers and travel** — 5-minute minimum around busy (maiea default), rule-extensible;
  location-aware padding deferred.
- **Timezone fairness** — one-off cross-zone: only offer inside the counterparty's inferred local
  window (maiea's generate-in-host-then-filter, ported with the provenance model in §7.3).
  Recurring cross-zone: a fairness ledger that rotates the painful slot is deferred but the
  score reserves the term.
- **Tier of the request** (§3.3) — a Tier-1 request may see slots a Tier-3 request never sees
  (bumpable-incumbent slots, hard-rule-adjacent asks routed through approval).

Weights are per-owner with shipped defaults; the scorer is deterministic and unit-testable —
**no LLM in the scoring path.** The LLM's role in availability is upstream classification only
(what kind of meeting is this, who is the counterparty), mirroring the gate's design.

### 3.3 Tiers, SLAs, and hold TTLs

Adopted from human-EA practice (the strongest published schema — Workmate's playbook — matches
how EAs at RH actually operate), computed recipient-side from counterparty identity (VIP store,
org role) + meeting type:

| Tier | Example | Respond within | Hold TTL | Confirm by |
|---|---|---|---|---|
| 1 | CEO/board, named VIPs | 1 business hour | 24h | immediately |
| 2 | directs, investors, candidates | 4 business hours | 48h | ≥12h before |
| 3 | standard cross-functional | EOD next day | 72h | ≥24h before |
| 4 | low/info-only | best-effort | none (offer without hold) | — |

Same-tier conflicts escalate to the principal — that is the human-EA convention and it maps to
`ask_first`. The VIP store ports from `rh-scheduler-mcp/counterparty_identity/vip.py`
(propose-from-behavior, owner confirms; never hand-scored — the division-of-labor principle from
the negotiation doc, kept intact).

### 3.4 The owner's preference document

Alongside structured rules, each owner has a free-text preference doc the EA reads at
classification time and proposes diffs to after feedback ("pitch calls are 25 minutes on video";
Howie's strongest design, and consistent with "owner-supplied policy, EA-executed judgment").
The doc **informs classification and scoring inputs only** — it cannot create autonomy: nothing
in it can move an action across the gate (§5), and learned preferences can never touch the
autonomy tier or gated action classes (maiea's `PERMANENTLY_EXCLUDED_FIELDS` boundary, kept as
a hard invariant).

## 4. Holds, reservations, and booking discipline

Ported from maiea's `tools/memory/slots.py` semantics with its three known gaps fixed:

- **Ledger:** `(owner, slot_start)` primary key; states `offered` (TTL per tier table, §3.3;
  maiea's flat 72h becomes the tier default) and `booked` (no TTL). Atomic claim
  (`INSERT … ON CONFLICT`), same-negotiation re-claim allowed, promote-on-confirm,
  release-on-terminal.
- **Fixes over maiea:** (1) claims **fail closed** — maiea failed open on ledger errors and
  relied on freebusy as the only backstop; here the ledger is authoritative and freebusy-at-book
  is defense-in-depth, per the negotiation doc's "one authoritative arbiter" shape; (2) an
  active expiry **reaper**, not only lazy sweep-on-claim; (3) `booked` holds reconcile against
  the calendar (event cancelled outside the EA → hold released) on a periodic pass.
- **Release on every terminal outcome** — all four terminal negotiation outcomes and `decline`
  release that negotiation's offered holds (maiea TECH-4098 Gap-4, generalized).
- **Claim-before-act, write-ahead:** the state write committing "this negotiation is booking"
  precedes the calendar call, with a transient state and a stuck-claim reaper (30-min threshold,
  `booking.py` — exists, needs its recovery action built). Prefer silent non-delivery caught by
  the reaper over a duplicate invite.
- **One booking choke point:** a single code path performs calendar writes, gated by the
  autonomy gate, the ledger claim, freebusy-at-book (fail-closed), and the circuit breaker.
  No second path — maiea's worst incidents were exactly a second path (LLM poller) bypassing
  the guarded one. In this design the LLM never calls the calendar directly; it requests a
  booking through the choke point.

## 5. The autonomy gate and approval flows

Adopted wholesale from `rh-scheduler-mcp/autonomy/` (gate, confidence, audit, golden scenarios —
built, tested, unwired). Restated here only where this repo adds or binds:

- **Three-valued gate** (`act | ask_first | cannot_determine`, never-raise, evaluation order
  contractual). **Permanently gated, no confidence override, exactly three:** cross a hard
  rule's override bar; cancel/reschedule an already-booked meeting; send an external invite.
- **Confidence is a deterministic function of logged evidence** per (owner × action-type ×
  counterparty-class): 5 clean approvals for autonomy (3 if rule-covered), any rejection forces
  ask-first. **This repo closes the loop that makes that real:** every approval-surface response
  (approve / reject / edit / expire) writes an owner-response outcome record (the TECH-4996 gap —
  `rejection_count` is currently always 0, so fast demotion is inert until this lands). Fast
  demotion is the trust-critical behavior; it ships in v1, not later.
- **Approval surface:** the EA never talks to the principal directly — it emits an approval
  request through the main agent (Slack DM / reclaw inbox) with three-tier routing: **do
  silently / do and tell / ask first** (v3's notification design; the middle tier is the
  fatigue-killer and calibrates from reaction feedback). Approval invariants, each a regression
  test because each was a real maiea bug: **approve-must-advance** the held negotiation;
  **expire-must-release** it (2h TTL; expiry returns the negotiation to its prior state or
  terminal-escalates). **Corrected 2026-08-11 (Argus round 2 finding):** an earlier version of
  this line paired the 2h TTL with a "batched daily nudge," which doesn't cohere — a once-a-day
  reminder cadence would fire, at best, after most 2h-TTL holds have already expired, making the
  nudge nearly useless for the case it exists to help. These are genuinely two different
  mechanisms with two different, currently-decoupled cadences: the **expiry sweep** (releasing a
  hold once its TTL lapses) is implemented and runs on every `Negotiator.react()` tick, tight
  enough that a hold rarely sits expired-but-unswept for long (§4,
  `sweep_expired_booking_approvals`); the **nudge** (telling the principal "you still have a
  pending approval") is a notification concern that is not implemented in this repo at all yet
  (tracked as TECH-5074) — its cadence is genuinely open, and "daily" was carried over from an
  earlier draft without being checked against the 2h TTL it now sits next to. Whoever builds the
  nudge should pick a cadence that actually falls inside the TTL window (e.g. a single reminder
  at the TTL's midpoint), not reuse "daily" by default. Button payloads carry resume-state
  identifiers, never
  draft bodies. **Clarified 2026-08-11 (Argus round 1 finding):** "returns to prior state or
  terminal-escalates" was ambiguous for the one case where a booking-gate approval hold expires
  *after* every participant's confirm is already standing — there is no earlier `in_progress`
  negotiation state to fall back to in the usual sense, since the negotiation itself completed
  before booking was ever requested. The implemented answer: the negotiation's own round-state
  stays exactly where completion left it (still `IN_PROGRESS` at the round-tracking level —
  completion and booking are deliberately separate gates, §2.2 point 3); the expiry releases this
  identity's own promoted ledger holds and clears the pending-approval marker, so the *booking
  decision* — not the negotiation — is what's retryable: a later `maybe_finalize` call re-requests
  the gate from scratch against the still-standing confirms (`sweep_expired_booking_approvals`,
  §4). This is a deliberately softer outcome than a full terminal-escalation of the negotiation
  itself, matching "fast demotion, quick to lose, but recoverable" rather than discarding a
  completed negotiation over an unactioned approval.
- **Non-response is a decline, everywhere.** Movability inquiries default to "no move" at 30
  minutes; approval holds expire; counterparty silence hits the follow-up ladder then goes
  quiet. Never implicit consent, never pending forever.
- **Gate placement in the negotiation:** `confirm` (commitment — gated as
  external-invite-adjacent until confidence earns `act` per counterparty class); **booking the
  completed negotiation** (§2.2 point 3, decided 2026-08-11) — a distinct gate check from
  `confirm`, evaluated only once every participant's confirm stands, using `send_external_invite`
  (permanently gated) when the counterparty is outside the trust domain or the applicable internal
  action type otherwise; any counter-offer contingent on moving an existing meeting (ask-first
  until earned; the *incumbent side's* gate, via the movability round); opening an outbound
  negotiation with a new external counterparty (ask-first for new owners). `availability_response`
  with plain free slots to a known internal counterparty is the day-one autonomous act
  (grab-open-slot class). Booking itself starts ask-first for every owner and, for internal
  counterparties, earns autonomy the same way every other confidence-gated action does — there is
  no special-cased "always ask to book" rule beyond what `send_external_invite`'s permanent gate
  already provides for the external case. **Noted 2026-08-11 (Argus round 1 finding):** this
  produces a real, intentional asymmetry for an external counterparty specifically — `confirm`
  itself can earn `act` autonomy per counterparty class (§2.2), but the *booking* step for that
  same external counterparty stays permanently `ask_first` via `send_external_invite`, with no
  confidence escape. An owner can therefore reach a state where their EA confidently confirms
  times with a given external party unattended, yet still asks permission every single time
  before actually sending that party the calendar invite. Not a bug: confirming is an internal
  negotiation-state commitment (this EA agreeing on a time on its own owner's behalf), while
  booking is the moment a third-party-visible artifact leaves the building — the design doc's own
  standing rationale for `send_external_invite`'s permanence ("the action is third-party-visible
  and mistakes there are the expensive, hard-to-recover kind") applies to the send, not the
  agreement that precedes it. The two steps are allowed to have different trust ceilings on
  purpose.
- **Golden scenarios** extend to negotiation: fixed (rule set, conversation transcript, expected
  gate decisions + expected wire messages) pairs run on every change to judgment logic. The
  scorer (§3.2) gets the same treatment: fixed (calendar, rules, request) → expected ranked
  slots.

## 6. Movability

- **Internal movability round:** exists (`negotiation/movability.py`) — gate-routed
  (`act` → movable; `ask_first`/`cannot_determine` → pending with 30-min timeout-as-decline);
  only `MOVE | NO_MOVE` crosses to the counterparty, never the reasoning, and `not_movable` vs.
  `declined_timeout` are indistinguishable on the wire by design.
- **Moveability annotations (adopt from v3):** the EA pre-annotates its owner's events with a
  movability assessment offline (rule match + organizer/attendee heuristics + owner feedback),
  so negotiation-time scoring reads an annotation instead of running judgment in the hot path.
  Annotations are advisory inputs to the scorer; the movability round remains the authority
  before any contingent offer is confirmed.
- **External bumping (moving a meeting with an external counterparty to make room) is out of
  scope for v1.** Nobody has shipped it; it composes cleanly later as: incumbent's EA opens a
  reschedule negotiation — which is a permanently-gated action class and therefore always
  principal-approved.

## 7. The EA-to-human path (counterparty has no EA)

The main agent is the transport; the EA is still the brain. **Identity, decided 2026-08-11: one
shared email address for every automated EA** (e.g. `ea@redesignhealth.com`), not one address per
owner and not one per external counterparty — the earlier open question (per-owner vs.
per-counterparty proxy identities) is resolved in favor of a third option, a single shared
front door. A **routing service** sits in front of that address: it receives all inbound mail,
resolves which owner/negotiation a message belongs to (thread/Message-ID correlation against
open conversations, since there is no address-based signal to route on), and dispatches it to
that owner's EA. Outbound mail from any EA also sends *from* that one address, with the owner's
display name set per-message so a human counterparty still sees "Neil's EA" or equivalent,
never a bare shared mailbox. This is genuinely new infrastructure, not a repurposed piece of any
EA's own logic — see the delivery plan (§12) and the tracked follow-up ticket.

On the board, the routing service (not each EA) owns the proxy agent identity: it opens/joins
conversations as a distinct proxy per external thread (registered with
`accepted_types=[scheduling.availability]`, display-named for audit as a proxy, one board
identity per external thread so conversation membership stays legible — the *email address* is
shared, the *board identity* is still one per thread), translating between prose email and typed
messages, then handing typed messages to the correct owner's EA and prose replies back out
through the shared address. The EA negotiates identically in both modes — one state machine, one
gate, one ledger; it does not know or care that its counterparty is a routing-service proxy
rather than another EA. Mode selection is the board-registry lookup, **failing toward "no EA"**
(a human getting a slightly terse email is
recoverable; protocol payload in a human inbox is not).

What the adapter ports from maiea (the hard-won text layer, kept at the boundary where the
untrusted text lives):

- **`detect_explicit_acceptance`** wholesale (`scheduling_ask.py` / `scheduling_vocab.py`):
  decline-cues-first, word-boundary accept tokens, date+time both required, exactly-one-slot,
  **promote-only** — it may force a correct `confirm`, never veto; ambiguity routes to the LLM
  classifier and, past its floor, to the principal.
- **Injection fencing** on every classifier reading counterparty text
  (`<untrusted_email_content>` wrapping + do-not-follow framing) — hard requirement, not
  hygiene.
- **Anti-assertion guards** on outbound prose (`sentiment_guard`, `copy_guard`): never fabricate
  the principal's mental state; never imply times were arranged at the counterparty's expense.
  Draft → scan → one regeneration → park.
- **Re-offer guard:** durable history of offered slot sets; never re-send an identical set;
  one excluded-slot retry then park with a DM (maiea's `ok | empty | exhausted` verdicts).
- **Cadence and silence:** one follow-up at 48h, then permanent silence; post-booking go-quiet
  with a narrow explicit-ask unlock. Knowing when not to reply is a feature (Howie's founder,
  and maiea's duplicate-send history, agree).
- **Timezone provenance** (`_tz.py`): `detected | host_default | unknown` — detected constrains
  offers to the counterparty's window with dual-zone rendering; unknown → hold-and-ask, never
  book; **no domain-based inference ever.**
- **Booking-link option** (open question in the negotiation doc, kept open): for the common
  EA-to-human case a self-serve booking page over mediator-computed slots may beat prose rounds.
  Disclosure rules unchanged — the link only changes the medium.

## 8. Learning

- **Confidence** learns from gate audit records + owner-response outcomes (§5) — deterministic,
  already designed.
- **Preferences** learn through proposals: the EA proposes rule edits, VIP additions, and
  preference-doc diffs from observed behavior and corrections; the owner confirms. The EA never
  silently rewrites its own policy, and nothing learned crosses the autonomy boundary (§3.4).
- **The weekly "what did your EA do" digest** (v3's decisions-view) is the legibility mechanism
  that makes continuous confidence owner-visible — it substitutes for the tier table a discrete
  system would have given for free.

## 9. Provenance

| Piece | Source | Status |
|---|---|---|
| negotiation outcomes/rounds/transitions, circuit breaker, escalation ladder, booking claims | `rh-scheduler-mcp/negotiation/` | built + tested, unwired — wire here |
| autonomy gate, confidence engine, audit, golden scenarios, rules engine + defaults, VIP store, disclosure/movability | `rh-scheduler-mcp/{autonomy,rules,counterparty_identity,disclosure,negotiation/movability}.py` | built + tested, unwired — wire here |
| slot arithmetic, mutual availability, moveable-event heuristic, tz handling | `rh-scheduler-mcp/core/` (ported from maiea `732c491` with cited provenance) | deployed read-only in mediator dev |
| acceptance detector, injection fencing, copy/sentiment guards, re-offer guard, cadence/silence, reservation-ledger semantics, approval invariants, choke-point doctrine | maiea (`tools/email_scheduling/`, `tools/memory/slots.py`, `scheduling_ask.py`) | port per §4/§5/§7, with the three ledger fixes |
| wire schema + completion rule | `reclaw-comms-mcp` | board-side, in delivery |
| preference scorer, tier/SLA computation, owner-response records, approval surface binding, moveability annotations, orchestration loop | **new — this repo** | unbuilt |

Owner-response records land the fast-demotion path (tracked upstream as TECH-4996);
`check_conflicts` attribution leakage — cited as a deploy blocker in the v3 doc — was fixed on
`rh-scheduler-mcp` main 2026-08-08 (`690060a`); the v3/pilot docs are stale on that point.

## 10. Failure-mode ledger

Every mechanism above exists because of a specific observed failure. The regression suite is
organized around this table, not around modules.

| Failure (observed in) | Mechanism here |
|---|---|
| double-book across negotiations (maiea E1) | ledger claim fail-closed + freebusy-at-book + confirm-after-hold (§2.2, §4) |
| confirm + re-offer in one turn (maiea E2) | single choke point; LLM never writes calendar/wire directly (§4) |
| accepted time re-offered (maiea E3) | promote-only acceptance detector floor (§7) |
| counterparty timezone lost on re-offer (maiea E4) | tz provenance threaded through every render path (§7) |
| duplicate send on key drift (maiea #298) | negotiation_id = conversation_id, stamped once (§2.1) |
| duplicate send on restart | write-ahead states + gate-claim-before-network + reapers (§4) |
| stale confirm books a dead slot (protocol race) | per-participant confirm + latest-message-supersedes + hold-before-confirm (§2.2) |
| orphaned negotiation after approval (maiea D17–D19) | approve-must-advance / expire-must-release, both tested (§5) |
| runaway negotiation loops (maiea TECH-1877) | round budget + identity-wide circuit breaker (§2.1) |
| autonomy tier typo falls through to send (maiea D8) | positive allowlist, exact match (§5) |
| "works" in a decline books a declined time | decline-cues-first detector (§7) |
| notification fatigue → middle tier ignored (v3 risk) | reaction-calibrated three-tier routing + weekly digest (§5, §8) |
| calendar API storm (maiea #357) | mediator fast path; annotation reads over hot-path judgment (§2.3, §6) |
| owner rejection silently superseded by TTL expiry, dropping the confidence signal (Argus round 4, TECH-5076) | known gap, not yet fixed — `respond()` checks hold status, not TTL, so a rejection racing a delayed sweep can lose the `OwnerResponseOutcome` the fast-demotion invariant in §5 depends on (§5) |

## 11. Open questions

1. ~~Platform binding~~ — resolved 2026-08-11: reclaw only; Paperclip is discarded as a hosting
   platform. The EA runs as its own MCP server (`reclaw-ea-mcp`), not folded into `rh-scheduler-
   mcp`; auth mirrors the sibling services (Okta + rh-auth JWT, owner identity from verified
   claims only). See §1a.
2. Scorer weights: shipped defaults vs. owner-tunable in v1; and whether tier computation needs
   org-chart data beyond the VIP store.
3. ~~Proxy-agent identity for the EA-to-human path~~ — resolved 2026-08-11: one shared email
   address for all automated EAs, fronted by a routing service that owns a per-thread board proxy
   identity and dispatches by thread/Message-ID correlation, not by address (§7).
4. Booking-link build-vs-integrate (carried from the negotiation doc, still open).
5. Multi-party (>2) negotiations: the board's completion rule generalizes (all-confirm), but
   convergence strategy, nudge cadence, and quorum-refusal need their own pass — maiea's
   multiparty invariants (explicit consent only, verbatim objection quotes, all-must-accept)
   are the constraints; defer past v1.
6. Recurring-meeting fairness ledger (§3.2) — design pass deferred.

## 12. Delivery plan

1. **Skeleton + ledger + scorer v0** — reservation ledger (with the three fixes), deterministic
   scorer over rules-engine constraints with default weights, golden scorer tests. No wire yet.
2. **Board integration** — register, open/conduct/complete `scheduling.availability`
   conversations EA-to-EA (internal pilot pair), mediator fast path, gate wired with
   ask-first-everything defaults, approval surface through the main agent.
3. **Confidence loop** — owner-response records, fast demotion, weekly digest, loosen day-one
   autonomous set (grab-open-slot).
4. **EA-to-human adapter** — email proxy path with the ported maiea text layer; booking-link
   decision.
5. **Movability annotations + contingent offers** — internal movability round wired end-to-end.

maiea stays in production throughout (per v3); this replaces it person-by-person as pilots
graduate.
