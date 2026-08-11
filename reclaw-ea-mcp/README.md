# reclaw-ea-mcp

MCP service wrapping `reclaw-ea`'s `Negotiator` (TECH-5065): per-owner scheduling negotiation,
holds/booking discipline, and the autonomy gate, exposed as a tool surface for the reclaw agent
run-loop host (TECH-5084, not yet built). See [`../reclaw-ea/docs/DESIGN.md`](../reclaw-ea/docs/DESIGN.md)
§1a for the platform/deployment decision this service implements.

This is one of several sibling projects in the `rh-reclaw` monorepo (see `../reclaw-comms-mcp/`,
the agent-to-agent comms hub this service will eventually speak to as an MCP client, and
`../reclaw-ea/`, the library this service wraps) -- all commands below assume you're running them
from *inside this directory*, not the repo root.

## Tool surface

Mounted under the `ea` namespace. This service performs no LLM reasoning of its own -- callers
supply already-scored candidate slots (`ea_react_to_conversation`'s `my_candidates`) and get back
plain dicts, never `Negotiator` internals or wire-schema objects.

| Tool | Scope | Purpose |
|---|---|---|
| `ea_whoami` | `ea:read` | Return the caller's identity, issuer, caller type, and scopes |
| `ea_check_completion` | `ea:read` | Return the agreed slot for a conversation, or `None` |
| `ea_negotiate` | `ea:write` | Open a negotiation with another owner's EA |
| `ea_react_to_conversation` | `ea:run` | Process the counterparty's latest message; propose/confirm/decline |
| `ea_request_booking` | `ea:write` | Request booking through the autonomy gate once a negotiation completes |
| `ea_respond_to_approval` | `ea:write` | Resolve a pending booking-approval hold |

`owner_identity` is derived exclusively from the verified token's claims (`identity.
require_owner_identity`) -- never accepted as a tool parameter. This is load-bearing: a bug here
would let one owner's agent read or spend another owner's ledger/approval history.

## Known interim gaps

See `providers/ea.py`'s module docstring for the full list and tracking tickets. Summary:

* **Board**: a single process-wide `FakeBoard`, not a real `reclaw-comms-mcp` client -- works for
  a same-process internal pilot pair, not across processes/services (TECH-5055, in progress).
* **Persistence**: in-memory `Ledger`/`ApprovalSurface`/`OutcomeStore` per owner -- state does not
  survive a restart (TECH-5083).
* **Rules**: no owner-authored rule UI; every owner is seeded with the shipped defaults
  (`scheduler_mcp.rules.apply_defaults`) and cannot edit them through this service, by design.
* **Booking**: `ea_request_booking`/`ea_respond_to_approval` run the deterministic booking
  discipline but do not create a real calendar event -- the caller is expected to do that itself
  and retry the tool call if the invite creation fails.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                # install dependencies (see "Network requirements" below)
uv sync --group test    # also install pytest for local test runs
uv run pytest           # run the test suite
```

### Network requirements

Same as `../reclaw-ea/README.md`: this service depends on `reclaw-ea` (a local path dependency,
editable), which in turn pins `scheduler-mcp` to a git SHA whose own `rh-auth` dependency lives on
a **private Gitea package index** reachable only over the Redesign Health Tailscale tailnet
(`https://gitea.drum-mackarel.ts.net/...`). Resolving or syncing this repo's dependencies
therefore requires being on that tailnet.

`uv sync --frozen` also requires tailnet access -- `uv.lock` embeds direct download URLs to the
private Gitea host, so a frozen install still needs tailnet access for the artifact download
itself, not just for dependency resolution. The registry allows anonymous reads on-tailnet -- no
additional PAT or service-account token is required beyond tailnet membership.

`[tool.uv] exclude-newer` (the supply-chain-defense cutoff `reclaw-comms-mcp/pyproject.toml` sets)
is deliberately NOT set here: Gitea's simple index does not report real upload-date metadata for
`rh-auth`, so any active cutoff makes uv treat every version as excluded. `[tool.uv.workspace]`
below (`members = ["."]`) is what stops that setting from being inherited from the root
`pyproject.toml` one directory up -- without it, uv walks up to the nearest ancestor pyproject.toml
and applies its `[tool.uv]` table key-by-key for anything left unset here.

## Linting and type-checking

```bash
uv sync --group dev               # installs ruff and mypy into this project's venv
uv run ruff check --config pyproject.toml .
uv run ruff format --config pyproject.toml .
uv run mypy . --config-file pyproject.toml
```

The explicit `--config`/`--config-file pyproject.toml` above is not optional, for the same reason
as `reclaw-ea/README.md`'s identical note: both `ruff` and `mypy` walk up parent directories
looking for a config file, and `reclaw-comms-mcp/pyproject.toml` one directory up sets
`[tool.ruff]` (a 100-char line length plus extra lint rules) and `[tool.mypy] strict = true` --
without pinning the config file explicitly, running these commands from inside `reclaw-ea-mcp/`
can silently pick up the sibling project's settings instead of this project's own.

## Running locally

```bash
MCP_TOKEN_STORAGE_PATH=/tmp/reclaw-ea-mcp-tokens \
OKTA_ISSUER_URL=... OKTA_CLIENT_ID=... OKTA_CLIENT_SECRET=... \
MCP_JWT_SECRET=... RH_AUTH_SECRET=... \
uv run python main.py   # http://127.0.0.1:8081/mcp
```

Port 8081, not `reclaw-comms-mcp`'s 8080, so both services can run side by side locally without a
port collision.

## Deployment

Not yet built -- see TECH-5067 (Terraform + deployment flow for `reclaw-ea-mcp`, following the
same `mcp-server` Terraform module pattern as `rh-mcp`/`rh-scheduler-mcp`/`reclaw-comms-mcp`).
