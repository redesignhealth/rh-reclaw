# reclaw-comms-mcp

MCP service for **permissioned, structured agent-to-agent communications** at
Redesign Health. First use case: a user's main agent delegates to a dedicated
EA agent, which communicates with other people's EA agents to negotiate
availability (including judgment, not just calendar overlap). Communications
are scoped and structured — no free text initially. See
[`docs/DESIGN.md`](docs/DESIGN.md) for the full spec (data model, permission
model, message schemas). EA agent logic lives elsewhere — this repo is only
the comms layer.

## Layout

```
main.py              # FastMCP server, observability + scope-enforcement middleware
auth.py              # Okta OIDCProxy (humans) + rh-auth JWTVerifier (agents) via MultiAuth
scopes.py            # TOOL_SCOPES catalog + fail-closed scope helpers
identity.py          # Issuer-gated JWT identity resolution (anti-impersonation guards)
observability.py     # structlog JSON events (tool_call, scope_denial, auth_flow, ...)
providers/comms.py   # Comms provider sub-server (placeholder whoami tool)
models.py            # SQLAlchemy 2.x async ORM models (agents, conversations,
                      #   participants, messages, audit_log — DESIGN.md §5)
db.py                # Async engine/session factory (DATABASE_URL, fail-fast)
migrations/          # Alembic migrations (async env.py); run `alembic upgrade head`
tests/               # pytest suite (composition, scope fail-closed, whoami, schema)
```

The layout mirrors [rh-mcp](https://github.com/redesignhealth/rh-data-platform/tree/main/services/rh-mcp),
the reference MCP implementation in the [RH tech guide](https://github.com/redesignhealth/rh-tech-guide).

## Auth model

Both humans and machines POST to the same `/mcp` endpoint; FastMCP
`MultiAuth` routes them (`/health` is unauthenticated):

- **Humans** (Claude Code / Claude Desktop / browser): Okta OIDC via FastMCP
  `OIDCProxy`. Identity claims (email) are available to tools via
  `get_access_token().claims`. Interactive callers bypass per-tool scope
  checks.
- **Agents / services**: rh-auth HS256 Bearer JWT (issued by the Tech Team
  via `rh-auth issue --sub <agent> --scopes comms:read,...`), verified by a
  `JWTVerifier` keyed to `RH_AUTH_SECRET`. Every tool call is then gated by
  the `TOOL_SCOPES` catalog in `scopes.py` — **fail-closed**: a tool without
  a registry entry rejects every rh-auth call, denial messages are uniform
  (anti-enumeration), and each denial emits a structured `scope_denial` log
  event.

When adding a tool, enroll its mounted name (`comms_<tool>`) in
`TOOL_SCOPES` in the same PR — `tests/test_main.py` fails otherwise.

## Local development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # install deps from uv.lock
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy .                # strict type check

# Run the server (needs real Okta + secret config)
cp .env.example .env         # fill in values; .env is gitignored
uv run python main.py        # http://127.0.0.1:8080/mcp

# Or the full stack (server + Postgres) in Docker
docker compose up --build
```

### Database / migrations

Postgres is provisioned by `docker-compose.yml` (`DATABASE_URL` points at
it). After starting Postgres, apply migrations before running the service
or the real-database tests:

```bash
docker compose up -d postgres      # start Postgres only
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/reclaw_comms
uv run alembic upgrade head        # create/upgrade the 5-table schema
```

To generate a new migration after changing `models.py`:

```bash
uv run alembic revision --autogenerate -m "<description>"
```

`tests/test_db_models.py` runs against this same real Postgres instance (no
mocking, per the RH standard) and skips gracefully with a clear reason if it
can't connect.

Configuration is env-driven and **fail-fast**: the service refuses to start
if any required variable (`OKTA_ISSUER_URL`, `OKTA_CLIENT_ID`,
`OKTA_CLIENT_SECRET`, `MCP_JWT_SECRET`, `RH_AUTH_SECRET`) is missing or
empty. See `.env.example` for the full list. No secrets are committed
anywhere in this repo.

## Observability

Structured JSON logs via `structlog` to stdout → CloudWatch. Event schema
matches the MCP fleet (`tool_call`, `user_active`, `auth_flow`,
`auth_rejected`, `scope_denial`) so existing Metric Filters / Logs Insights
queries apply. Never log message content or attacker-controlled claim
values.

## Deployment (not yet wired)

Like the other RH MCP services, this deploys as an **ECS Fargate task with a
Tailscale sidecar** (tailnet-only, no public endpoint) via the shared
[`mcp-server` Terraform module](https://github.com/redesignhealth/rh-data-platform/tree/main/infrastructure/modules/mcp-server).
When ready:

1. Add a `module "reclaw_comms_mcp"` block in
   `rh-data-platform/infrastructure/environments/{dev,prod}/` invoking
   `../../modules/mcp-server` (see the `rh_mcp` invocation in
   `environments/prod/main.tf` for the shape: ECR image, Tailscale hostname,
   EFS mount for `/data/fastmcp-tokens`, and `secret_ssm_paths` for
   `OKTA_*`, `MCP_JWT_SECRET`, `RH_AUTH_SECRET`, and later `DATABASE_URL`).
2. Provision the SSM parameters (Terraform `random_password` for
   `MCP_JWT_SECRET`; the rest via the Tech Team's SSM process) and register
   the Okta application for this service's `BASE_URL`.
3. Add an ECR repository + a deploy workflow (build/push via GitHub OIDC,
   then update the image tag), modeled on
   `rh-data-platform/.github/workflows/deploy-rh-mcp.yml`.

No Terraform lives in this repo — rh-data-platform keeps infrastructure in
its own `infrastructure/` tree, and this repo follows that convention.
