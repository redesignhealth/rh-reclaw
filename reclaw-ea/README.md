# reclaw-ea

The per-person EA agent library -- scheduling negotiation, preference scoring,
holds/booking discipline, and the autonomy gate. See [`docs/DESIGN.md`](docs/DESIGN.md) for the
full design, and [`agent-config/`](agent-config/) for the agent config (identity + starter
user-memory template) reclaw auto-loads into the deployed EA agent's prompt.

This is one of several sibling projects in the `rh-reclaw` monorepo (see `reclaw-comms-mcp/` one
directory up, the agent-to-agent comms hub this library's `reclaw-ea-mcp` deployment will speak
to) -- all commands below assume you're running them from *inside this directory*, not the repo
root.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                # install dependencies (see "Network requirements" below)
uv sync --group test    # also install pytest for local test runs
uv run pytest           # run the test suite
```

### Network requirements

`uv sync`/`uv lock` need more than public GitHub access. `pyproject.toml` pins `scheduler-mcp` to
a `rh-scheduler-mcp` git SHA (public GitHub), but `rh-scheduler-mcp`'s own `pyproject.toml` pins
`rh-auth` to a **private Gitea package index** reachable only over the Redesign Health Tailscale
tailnet (`https://gitea.drum-mackarel.ts.net/...`). Resolving or syncing this repo's dependencies
therefore requires being on that tailnet -- off-tailnet, `uv sync`/`uv lock` will fail with an
opaque network error rather than a clear "you're not on the tailnet" message. This includes
`uv sync --frozen` (the invocation CI/Docker builds should use): `uv.lock` embeds direct download
URLs to the private Gitea host, so a frozen install still needs tailnet access for the artifact
download itself, not just for dependency resolution. When CI is added for this repo, the CI
runner will need tailnet access too (see TECH-5067). The registry allows anonymous reads
on-tailnet -- no additional PAT or service-account token is required beyond tailnet membership
(verified while generating `uv.lock` for this repo).

## Linting and type-checking

```bash
uv sync --group dev               # installs ruff and mypy into this project's venv
uv run ruff check --config pyproject.toml src/reclaw_ea tests
uv run ruff format --config pyproject.toml src/reclaw_ea tests
uv run mypy src/reclaw_ea --ignore-missing-imports --config-file pyproject.toml
```

The explicit `--config`/`--config-file pyproject.toml` above is not optional: both `ruff` and
`mypy` walk up parent directories looking for a config file, and `reclaw-comms-mcp/pyproject.toml`
one directory up sets `[tool.ruff]` (a 100-char line length plus extra lint rules) and
`[tool.mypy] strict = true` -- without pinning the config file explicitly, running these commands
from inside `reclaw-ea/` silently picks up the sibling project's (much stricter) settings instead
of this project's own `[tool.ruff]`/`[tool.mypy]` tables.
