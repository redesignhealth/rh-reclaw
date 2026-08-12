# Agent config — reclaw-ea

Version-controlled source of the two files reclaw auto-loads into this EA agent's prompt every
run: `identity.md` and `user.md`. See `docs/DESIGN.md` for the underlying negotiation/autonomy
design these files instruct the agent to operate; see `../../reclaw-ea-mcp/README.md` for the
tool surface `identity.md` references.

- **`identity.md`** — the agent's own role, tool usage, and negotiation loop. Same for every
  owner; only changes when the design or tool surface changes. Copy verbatim into a new owner's
  agent memory.
- **`user.md`** — a STARTER TEMPLATE, not real owner data. Copy into a new owner's memory
  directory and let the agent fill it in over time as the owner states preferences, tone, and
  per-meeting priority flags. Never commit a real owner's filled-in version back here.

## Known limitation

`user.md`'s "Hard constraints" section is agent-memory enforcement only — it relies on the agent
correctly recalling and re-applying a stated preference every negotiation turn, with no
deterministic backstop. A structured, code-enforced alternative (a `Rule` store the deterministic
scorer reads directly, regardless of what the agent does in any given turn) is designed but not
yet built — see `docs/DESIGN.md`'s movability/rules sections and the delivery plan's "Movability
annotations" stage. Until that lands, treat anything in `user.md` as best-effort, not a guarantee,
for anything safety-critical (e.g. "never double-book the CEO").
