#!/usr/bin/env bash
# Installs the ea provider's extra dependency chain (reclaw_ea, scheduler_mcp,
# rh-auth) into an already-`uv sync`'d venv, on top of reclaw-comms-mcp's own
# packages -- additively, via `uv pip install`, never via `uv sync` (which
# reconciles the target venv to match ONLY its own lockfile, uninstalling
# anything not in it -- see docs/proposals/reclaw-ea-plugin-registry.md's
# uv-workspace-spike section for why this two-lockfile split exists at all).
#
# Root's own uv.lock/pyproject.toml cannot include `reclaw-ea` as a direct
# workspace member or dependency: `rh-auth` (a private Gitea package, no
# reported upload dates, no Windows build) makes the combined resolution
# unsatisfiable under root's `exclude-newer`/multi-platform settings, even
# after loosening them. `ea-deps/` is a separate, minimal uv project whose
# only job is resolving+locking that dependency chain in isolation
# (mirroring the isolation `reclaw-ea-mcp/pyproject.toml` used before this
# merge). This script installs its resolved packages into the SAME venv
# root already populated, constraining any package the two trees share to
# root's own exact-pinned version -- so a shared package (fastmcp, pydantic,
# sqlalchemy, ...) never silently drifts off root's pin merely because
# ea-deps' independent resolution landed on a different version.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${1:?usage: install-ea-deps.sh <path-to-venv-python>}"

CONSTRAINTS_FILE="$(mktemp)"
trap 'rm -f "$CONSTRAINTS_FILE"' EXIT

# Root's own DIRECT dependency names (not the full transitive closure --
# purely-transitive packages like uvicorn/starlette/anyio are fine to let
# each tree resolve independently; only root's own exact-pin policy targets
# need protecting from ea-deps' independent resolution overriding them).
DIRECT_NAMES=$(python3 -c "
import tomllib
with open('$ROOT_DIR/pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
for dep in data['project']['dependencies']:
    # Strip extras (e.g. 'sqlalchemy[asyncio]==2.0.51' -> 'sqlalchemy') and
    # the version pin, keeping just the package name.
    name = dep.split('[')[0].split('=')[0].strip()
    print(name)
")

NAME_PATTERN=$(echo "$DIRECT_NAMES" | tr '\n' '|' | sed 's/|$//')
uv export --project "$ROOT_DIR" --frozen --no-dev --no-hashes --no-editable 2>/dev/null \
  | grep -E "^(${NAME_PATTERN})==" \
  > "$CONSTRAINTS_FILE"

echo "Constraining ea-deps' install to root's own pins for:"
cat "$CONSTRAINTS_FILE"

cd "$ROOT_DIR/ea-deps"
uv pip install --python "$VENV_PYTHON" \
  -c "$CONSTRAINTS_FILE" \
  "reclaw-ea @ file://$ROOT_DIR/reclaw-ea"
