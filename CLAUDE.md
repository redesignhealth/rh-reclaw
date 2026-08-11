# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Project: reclaw-comms-mcp

MCP service for permissioned agent-to-agent communications: a structured message board. First use case: a user's main agent delegates to a dedicated EA agent, which negotiates availability with other people's EA agents (including judgment, not just calendar overlap).

**The agreed v1 design is documented in [docs/DESIGN.md](docs/DESIGN.md) — that file is the spec of record.** Summary: no board-level permission layer (valid scoped token = admission; token issuance is the upstream ceremony); permissions live only in token scopes (fail-closed TOOL_SCOPES) and conversation membership. Agents self-register idempotently; any agent starts a conversation with N others (targets must accept the type); membership = visibility (full history on join, uniform denials for non-members); any member may invite (creator recorded role=owner for later tightening); decline/leave is the consent mechanism. Tables: agents, conversations, participants, messages (append-only, schema-validated typed payloads, no free text in v1), audit_log (mutations and denials). Identity always derives from verified OAuth token claims (rh-mcp MultiAuth pattern), never parameters. Deferred: grants layer for external counterparties (lands in the may_open policy function), free text behind a quarantine pipeline, email/Slack transports (handled by users' main agents, out of scope here). EA agent logic lives in reclaw-ea-implementation.

## RH Tech Guide

This project follows the [Redesign Health Technology Guide](https://github.com/redesignhealth/rh-tech-guide)
for all technical decisions. Run `/rh-tech-guide` to load relevant guidance for any topic.

**Always consult `/rh-tech-guide` before deciding on:**
- Stack choices (frontend framework, backend language, database)
- Authentication and authorization patterns
- Deployment approach (Dokploy vs Terraform+AWS)
- CI/CD setup
- Observability and logging
- Security and secrets management
- Agent-first CLI/MCP design

**Common failure patterns**: `~/.claude/rh-tech-guide/common-failure-modes/README.md`
indexes recurring code-review failure patterns, auto-updated weekly — check it before
writing or reviewing code if your task might overlap with something already
documented there. Run `/rh-tech-guide` first if that path doesn't exist yet, to sync
the tech guide.

<!-- rh-tech-guide-initialized -->
