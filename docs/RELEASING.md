# Upgrading the deployed agent-comms-mcp version

This repo deploys `agent-comms-mcp` from PyPI. The published package and its
source live at [redesignhealth/agent-comms-mcp](https://github.com/redesignhealth/agent-comms-mcp).

## Steps to bump the version

1. **Verify migration continuity.** Unpack the new wheel and diff the migration
   filenames against this repo's `migrations/versions/`:
   ```sh
   pip download agent-comms-mcp==X.Y.Z --dest /tmp/acm-dl --no-deps
   unzip /tmp/acm-dl/agent_comms_mcp-X.Y.Z-*.whl 'migrations/versions/*' -d /tmp/acm-inspect
   diff <(ls /tmp/acm-inspect/migrations/versions/) <(ls migrations/versions/)
   ```
   The diff must be empty. If the wheel adds new migration files that are not yet
   in this repo, add them here too before proceeding. A filename diff only
   confirms the *set* of migrations matches -- also diff each new file's full
   content against the unpacked wheel (`diff /tmp/acm-inspect/migrations/versions/<f> migrations/versions/<f>`)
   to catch an amended migration body or a corrected `down_revision` that a
   filename-only comparison would miss.

   This diff is against the target version's *final* wheel state, not an
   incremental version-by-version comparison, so it's safe to skip
   intermediate versions (e.g. bumping straight from 0.1.1 to 0.1.5): whatever
   migrations exist in the 0.1.5 wheel are exactly the ones this repo needs,
   regardless of how many releases were skipped to get there.

2. **Bump the version** in `pyproject.toml`:
   ```toml
   version = "X.Y.Z"
   ```

3. **Regenerate `uv.lock`**:
   ```sh
   uv lock
   ```

4. **Regenerate `requirements.lock`** from the workspace root:
   ```sh
   WHEEL_HASH1=sha256:<sdist-hash-from-pypi>
   WHEEL_HASH2=sha256:<wheel-hash-from-pypi>
   {
     printf '# Hashed requirements for the agent-comms-mcp container image.\n'
     printf '# Generated via: uv export --no-dev --format requirements-txt (workspace root)\n'
     printf '# with agent-comms-mcp==X.Y.Z PyPI wheel hashes prepended.\n'
     printf '# Regenerate: see docs/RELEASING.md\n'
     printf 'agent-comms-mcp==X.Y.Z \\\n'
     printf '    --hash=%s \\\n' "$WHEEL_HASH1"
     printf '    --hash=%s\n' "$WHEEL_HASH2"
     uv export --no-dev --format requirements-txt | grep -v '^-e \.' | grep -v '^# This file\|^#.*uv export'
   } > requirements.lock
   ```
   Get the wheel hashes from PyPI's "Download files" page for the release, or via:
   ```sh
   pip download agent-comms-mcp==X.Y.Z --dest /tmp/dl --no-deps
   sha256sum /tmp/dl/agent_comms_mcp-X.Y.Z-*.whl
   # For the sdist hash, check the PyPI JSON API or the release page directly
   ```

5. **Open a PR** with the changes to `pyproject.toml`, `uv.lock`, and
   `requirements.lock`. CI will:
   - Verify `requirements.lock` pin matches `pyproject.toml` version
   - Smoke-test both entry points (`agent-comms-mcp`, `agent-comms-mcp-migrate`)
     inside the built container
   - Run lint, type-check, and tests against the workspace source
