# Agent config — reclaw-ea

Version-controlled source of the two files reclaw auto-loads into this EA agent's prompt every
run (`identity.md`, `user.md`), plus the onboarding interview `identity.md` triggers on first run
(`startup-prompt.md`). See `docs/DESIGN.md` for the underlying negotiation/autonomy design these
files instruct the agent to operate; see `../../reclaw-ea-mcp/README.md` for the tool surface
`identity.md` references.

- **`identity.md`** — the agent's own role, tool usage, and negotiation loop. Same for every
  owner; only changes when the design or tool surface changes. Copy verbatim into a new owner's
  agent memory. Includes the first-run check that triggers `startup-prompt.md`.
- **`user.md`** — a STARTER TEMPLATE, not real owner data. Copy into a new owner's memory
  directory and let the agent fill it in over time as the owner states preferences, tone, and
  per-meeting priority flags. Never commit a real owner's filled-in version back here.
- **`startup-prompt.md`** — not auto-loaded by reclaw; referenced by `identity.md`. A one-time
  onboarding interview (timezone/hours, never-move list, approval philosophy, tone, etc.) that
  front-loads `user.md` with real content on an owner's first session, instead of starting from
  a blank template and only learning things reactively mid-negotiation.

## Known limitation

`user.md`'s "Hard constraints" section is agent-memory enforcement only — it relies on the agent
correctly recalling and re-applying a stated preference every negotiation turn, with no
deterministic backstop. A structured, code-enforced alternative (a `Rule` store the deterministic
scorer reads directly, regardless of what the agent does in any given turn) is designed but not
yet built — see `docs/DESIGN.md`'s movability/rules sections and the delivery plan's "Movability
annotations" stage. Until that lands, treat anything in `user.md` as best-effort, not a guarantee,
for anything safety-critical (e.g. "never double-book the CEO").
