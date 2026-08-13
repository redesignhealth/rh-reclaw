# Merge reclaw-ea-mcp into reclaw-comms-mcp as a config-driven pluggable provider

*Design proposal. No code changes in this PR — implementation follows in a separate PR once this is approved and merged, tracked by a Linear ticket referencing this doc.*

## Context

`reclaw-comms-mcp` (repo root) and `reclaw-ea-mcp` (sibling directory) are two independent FastMCP services today. They share nearly identical `main.py`, `auth.py`, `scopes.py`, `observability.py`, and `identity.py` files: same middleware pattern, same MultiAuth composition (Okta plus rh-auth JWT), same fail-closed scope enforcement. They differ in which provider they mount, and three of the five duplicated files have picked up real, functional divergence over the course of their history. Only `reclaw-comms-mcp` is deployed (dev and prod). `reclaw-ea-mcp` has zero Terraform, ECR, or IAM footprint anywhere, and its own README ties deployment to TECH-5067, a ticket that has produced no infra artifact yet.

Standing up `reclaw-ea-mcp` as a second full ECS service doubles every piece of infrastructure already built for `reclaw-comms-mcp`: a second Tailscale hostname (with a manual DNS record required for each new host on the Retool BYOC side), a second SSM and Okta bootstrap, a second rotation-loop entry, a second set of IAM grants. That cost buys nothing today, because `reclaw-ea-mcp` still has real gaps per its own code comments: `FakeBoard` holds in-memory-only state that's wiped on every restart (TECH-5055 and TECH-5083 haven't landed), the "always ask a human before booking with an external counterparty" safety invariant is unenforced because every counterparty is currently treated as internal (TECH-5069), and no run-loop host exists yet to actually drive its tools (TECH-5084).

Mount `ea` as a second namespace on the same FastMCP process as `comms` instead, behind a config-driven `ENABLED_PROVIDERS` env var, with a small plugin registry so future agents slot in the same way without touching the host's mounting logic. This halves the ops surface and gives `ea` a home now, while keeping prod exposure a separate decision from merging the code. This proposal enables `ea` in dev only (`ENABLED_PROVIDERS=comms,ea`). Prod stays `comms`-only.

## Architecture: one host process, multiple mounted providers

`reclaw-comms-mcp` becomes a single FastMCP host process that mounts one or more providers as separate namespaces on the same server, the same auth setup, and the same deploy. Each provider is a self-contained module under `providers/`, exposing its own `FastMCP` sub-server and its own `TOOL_SCOPES` dict. The host reads `ENABLED_PROVIDERS` at startup, resolves it against `providers/registry.py`'s `_REGISTRY`, and mounts exactly the providers that name lists. An unknown or empty value crashes the process at startup, so a config typo never silently drops a provider.

**Two providers exist today: `comms` and `ea`.** `comms` is the permissioned agent-to-agent messaging bus and is enabled everywhere. `ea` wraps `reclaw_ea.orchestrator.Negotiator` and is enabled in dev only. Enabling `ea` in prod is a separate decision from merging its code, gated on TECH-5055 (real board client, replacing `FakeBoard`), TECH-5069 (external-counterparty tier resolution, needed to actually enforce the "ask a human before booking externally" invariant), and TECH-5084 (the run-loop host that will actually drive these tools).

**Adding a new agent means writing one new provider module and one registry entry.** No changes to `main.py`, `auth.py`, or the middleware stack. Each provider brings its own scoped tool names (`<namespace>_<tool>`) and its own scope requirements. The host merges only the scopes for whatever's currently enabled.

**Why not a separate service per agent.** A second ECS service per agent means a second Tailscale hostname, a second SSM and Okta bootstrap, a second rotation-loop entry, and a second set of IAM grants, for every agent added. One host process with multiple mounted providers pays that cost once. The tradeoff is coupling: a bug in one provider's code path shares blast radius and deploy cadence with every other mounted provider, which matters more once a provider serves traffic from parties outside this host's own trust boundary.

## Implementation approach

### 1. Reconcile the three diverged shared files first

`reclaw-ea-mcp` picked up real improvements over `reclaw-comms-mcp`'s copies. Port them into the shared root files instead of discarding them when `reclaw-ea-mcp` goes away.

**`observability.py`.** Add `log_security_event()` (structured, carries a `severity` field, e.g. `"critical"` for an `alg=none` JWT rejection) and swap the current bare `exc_info: true` structlog config for `structlog.processors.ExceptionRenderer()`, which renders an actual traceback. Rename `hash_user` to `email_local_part`, since the function's actual behavior is truncation. Grep every call site first, including any saved CloudWatch Logs Insights query or alarm that references the field name in log output.

**`identity.py`.** Add `require_owner_identity(token)` alongside the existing `try_resolve_email`. It fails closed, raising `ToolError`, where `try_resolve_email` fails open. Any tool that turns resolved identity into a security-critical state key (as `ea`'s tools do) needs the closed-failing version.

**`auth.py`.** Route JWT-rejection paths through `log_security_event()` instead of the current bare `logger.error(...)`, which CloudWatch Metric Filters can't see. Verify against the existing auth test suite that accept-or-reject behavior stays identical, with only the logging path changed.

**Before merging this step, audit existing CloudWatch metric filters and alarms for anything keyed on `logger.error`'s output shape or on comms' current auth-rejection log severity.** This log-format change touches prod's live monitoring as much as it touches source code. `ExceptionRenderer` also changes how exceptions render in logs generally, beyond just auth rejections. Check for any saved Logs Insights query or alarm that parses the old plain-text format before this ships, the same way the `hash_user` rename check already calls for.

### 2. Split scopes.py into host helpers and per-provider dicts

`scopes.py` today holds one flat `TOOL_SCOPES: dict[str, str]` covering only `comms_*` keys, plus generic helpers (`required_scope_for`, `required_scope_for_resource`, `is_interactive_token`, `safe_client_id`, `scopes_for_token`) that already operate over whatever dict they're handed.

Keep only the generic helpers in `scopes.py` and delete the module-level `TOOL_SCOPES`/`RESOURCE_SCOPES` dict literals. Give `providers/comms.py` its own `TOOL_SCOPES = {...}`, moving the existing dict over verbatim since its keys already carry the post-mount `comms_*` prefix, plus `RESOURCE_SCOPES = {}`. Give `providers/ea.py` its own `TOOL_SCOPES` once it moves in step 5, keeping the `ea_*` keys it has today, including the `ea:run` verb unique to `ea_react_to_conversation`. `main.py` merges only the dicts for currently enabled providers at startup and passes the merged dict into `ScopeEnforcementMiddleware`'s existing constructor, no interface change there, just a different source for the dict. Assert no key collisions across providers as a defensive check, even though namespacing should already prevent them.

### 3. Add the provider registry

New file, `providers/registry.py`. The registry itself must never import `providers.comms` or `providers.ea` at module load time. `providers/ea.py` builds `FakeBoard()`, `InMemoryRuleStore()`, and its FastMCP server as module-level state, so an eager import of that module happens the moment `registry.py` is imported, regardless of whether `ea` is actually enabled. Since prod never enables `ea`, an eager import would mean prod's comms-only process still depends on `reclaw_ea`'s full dependency chain succeeding at startup, including a private-index dependency ea doesn't share with comms today. A genuinely lazy load, one that imports a provider module only when that provider is actually selected, keeps that failure mode out of prod entirely:

```python
@dataclass(frozen=True)
class ProviderSpec:
    name: str
    load: Callable[[], tuple[FastMCP, dict[str, str], dict[str, str]]]
    requires_db: bool = False

def _load_comms() -> tuple[FastMCP, dict[str, str], dict[str, str]]:
    from providers.comms import RESOURCE_SCOPES, TOOL_SCOPES, comms_server
    return comms_server, TOOL_SCOPES, RESOURCE_SCOPES

def _load_ea() -> tuple[FastMCP, dict[str, str], dict[str, str]]:
    from providers.ea import RESOURCE_SCOPES, TOOL_SCOPES, ea_server
    return ea_server, TOOL_SCOPES, RESOURCE_SCOPES

_REGISTRY: dict[str, ProviderSpec] = {
    "comms": ProviderSpec("comms", _load_comms, requires_db=True),
    "ea":    ProviderSpec("ea",    _load_ea,    requires_db=False),
}

def resolve_enabled_providers(raw: str) -> list[ProviderSpec]:
    names = list(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        raise RuntimeError(f"Unknown provider(s) in ENABLED_PROVIDERS: {unknown}")
    if not names:
        raise RuntimeError("ENABLED_PROVIDERS resolved to empty list")
    return [_REGISTRY[n] for n in names]
```

`dict.fromkeys` on the split names removes duplicates while keeping the first occurrence's order, so `ENABLED_PROVIDERS=comms,comms` resolves to a single mount instead of double-mounting the same namespace. `resolve_enabled_providers` still runs and validates before any provider actually loads. `main.py` calls `.load()` only on the specs it resolved, so the import of `providers.ea` (and everything it pulls in) happens exactly once, only when `ea` is in the enabled list.

Fail closed at startup, before the FastMCP app or auth provider gets built. An unknown provider name or an empty config crashes the process. It never silently drops a provider. Adding a future agent becomes: write `providers/<name>.py`, add one `_load_<name>` function and one `_REGISTRY` entry, done. `main.py` never changes.

### 4. Update main.py

Read `ENABLED_PROVIDERS = os.environ.get("ENABLED_PROVIDERS", "comms")`, so an unset value reproduces today's behavior exactly. Call `specs = resolve_enabled_providers(ENABLED_PROVIDERS)` before building anything else, then call `spec.load()` once per resolved spec to get each provider's `(server, tool_scopes, resource_scopes)` tuple, the only point at which a provider module actually gets imported. Build the merged `tool_scopes` and `resource_scopes` dicts from those loaded results and pass them into `ScopeEnforcementMiddleware`. Replace the single `mcp.mount(comms_server, namespace="comms")` call with a loop over the loaded `(spec, server)` pairs: `mcp.mount(server, namespace=spec.name)`. Gate the existing eager `database_url()` fail-fast call on `any(s.requires_db for s in specs)` instead of calling it unconditionally, so `main.py` never has to know provider names to decide whether a DB is needed.

Middleware order stays fixed: `ObservabilityMiddleware` before `ScopeEnforcementMiddleware`, per the existing comment warning against reordering. `build_auth_provider()` and the unauthenticated `/health` route stay untouched.

### 5. Move reclaw-ea-mcp in, then delete it

Move `reclaw-ea-mcp/providers/ea.py` to root `providers/ea.py`, pointing its imports at the reconciled root `observability.py` and `identity.py` instead of its own copies. Drop the `ea`-specific port default of 8081. One merged process runs on one port, comms' 8080 default.

**Adding `reclaw-ea` as a plain root workspace member is not a safe default and needs a spike before it's attempted.** `reclaw-ea-mcp/pyproject.toml` deliberately declared itself as its own single-member workspace (`members = ["."]`), kept isolated from a larger one, and its own comments explain why: root's `[tool.uv] exclude-newer = "7 days"` breaks resolution of the `reclaw_ea` dependency graph, and `reclaw_ea` needs `requires-python <3.14` where root is unbounded `>=3.12`, because the chain `reclaw_ea` → `scheduler-mcp` (a git-SHA-pinned dependency) → `rh-auth` (a private Gitea package index) doesn't resolve under root's current constraints. Folding `reclaw-ea` into the root workspace as originally planned would very likely break `uv lock` for the whole project, `comms` included.

Before writing any code for this step, run `uv lock` against a root `pyproject.toml` with `reclaw-ea` added as a workspace member and see what actually happens. If it resolves cleanly (constraints may have loosened since `reclaw-ea-mcp` was first split out), proceed as planned. If it fails the same way `reclaw-ea-mcp`'s own comments anticipate, use one of these instead, in order of preference:

1. Loosen root's `requires-python` upper bound to match `reclaw-ea`'s `<3.14` ceiling and relax or remove `exclude-newer`, if doing so doesn't break `comms`'s own dependency resolution (check `uv lock` again after each change).
2. If root's constraints exist for reasons that can't move, keep `reclaw-ea` as its own separately-locked project (not a workspace member) and reference it from root via a non-workspace path dependency, accepting a second lockfile for that subtree as the cost of avoiding a resolution conflict.

Either way, the actual constraint conflict `reclaw-ea-mcp` was built to route around needs an explicit, tested resolution here.

Delete `reclaw-ea-mcp/main.py`, `auth.py`, `scopes.py`, `observability.py`, `identity.py`, `pyproject.toml`, and the directory itself once tests are ported in step 6. Fold its README's deployment status and TECH-ticket notes into root `README.md` or `docs/DESIGN.md` so that context survives the deletion.

Update `docs/DESIGN.md`, which today documents EA connecting "as an MCP client" from a separate repo (section 12). This proposal reverses that framing. Replace it with the plugin-registry model described above, and note explicitly that `ea` is merged into the code but not prod-enabled, pending TECH-5055, TECH-5069, and TECH-5084.

Update root `pyproject.toml`'s `[tool.ruff]` and `[tool.mypy]` exclude lists to drop `reclaw-ea-mcp`, since it's folded in. Decide whether `reclaw-ea` itself joins the same lint config or stays independently excluded, based on how much its own config diverges.

### 6. Merge the test suites

Move `reclaw-ea-mcp`'s tests into root `tests/`, mirroring wherever `tests/test_comms_tools.py` already lives, updating imports for the reconciled `observability`, `identity`, and `scopes` modules. Reconcile `reclaw-ea-mcp/conftest.py` with root `conftest.py` as its own explicit step. Check both for fixtures with the same name and different behavior (e.g. a database-session fixture in root that ea's tests don't currently need but might collide with) before merging them into one file. Add `tests/test_registry.py`: an unknown provider name raises, an empty string raises, a duplicated name like `"comms,comms"` mounts once, `"comms"` resolves to a comms-only mount and scope set, `"comms,ea"` resolves to both with no key collisions, and importing `providers.registry` alone (with `ENABLED_PROVIDERS` unset) never triggers an import of `providers.ea`. Extend or add a `main.py`-level test parametrized over `ENABLED_PROVIDERS=comms` and `comms,ea`, asserting the mounted namespace set and merged scope dict for each. Update any test that imports `scopes.TOOL_SCOPES` directly to import from `providers.comms.TOOL_SCOPES` instead. Add regression tests for the reconciled files: `log_security_event`, `require_owner_identity`, `ExceptionRenderer` behavior, and the `hash_user` to `email_local_part` rename.

Check root `pyproject.toml`'s build configuration (`[tool.setuptools]` or equivalent) covers the new `providers/ea.py` module and any `reclaw_ea` package data the Docker image build needs to pick up. This is easy to miss since today's Docker build only ever needed to package `comms`.

### 7. Terraform follow-up, separate PR against rh-data-platform

Once the `rh-reclaw` code merge lands and a new image is pushed: add `ENABLED_PROVIDERS = "comms,ea"` to the `environment_variables` map in `infrastructure/environments/dev/reclaw_comms.tf`. Leave prod's map as is, or add `ENABLED_PROVIDERS = "comms"` explicitly for clarity. No new SSM parameters or IAM grants are needed. `RH_AUTH_SECRET`, the Okta credentials, and `MCP_JWT_SECRET` are already shared, non-comms-specific secrets `ea` can reuse as is, and `ENABLED_PROVIDERS` is a plain, non-secret env var. Don't rename the existing `rh-reclaw-comms`/`reclaw-mcp` Terraform resources, ECS service, or Tailscale hostname to something more generic. That's a separate, riskier identity change this proposal avoids.

## Verification

1. Run `uv run pytest` with `ENABLED_PROVIDERS` unset. The full suite must pass. Then run parametrized against both `comms` and `comms,ea`. Then a negative case: `ENABLED_PROVIDERS=comms,bogus` must raise immediately at process construction time.
2. Start the app with `ENABLED_PROVIDERS=comms` (or unset), connect an MCP client (`fastmcp dev` or the MCP inspector), and capture the `tools/list` response. Diff that JSON against a `tools/list` capture taken from the current, pre-refactor `reclaw-comms-mcp` running the same way. The tool names, schemas, and scopes exposed to a comms-only caller need to match exactly. A pytest pass alone doesn't verify what an actual MCP client sees on the wire. Confirm `/health` still responds unauthenticated.
3. Start the app with `ENABLED_PROVIDERS=comms,ea`. Confirm `comms_*` (12) and `ea_*` (6) both appear. Call an `ea_*` tool with a token lacking `ea:*` scope and confirm scope denial via `log_scope_denial`. Call it again with proper scope and confirm it succeeds against the in-memory `FakeBoard`/`InMemoryRuleStore`, matching today's standalone `reclaw-ea-mcp` behavior.
4. This step's file reconciliation changes prod's log output shape regardless of `ENABLED_PROVIDERS`, since `observability.py` and `auth.py` are shared by every provider. Before merging, confirm with the CloudWatch metric filters/alarms audit from step 1 that nothing downstream breaks, since prod picks up this change the moment the new image ships, whether or not `ea` itself is enabled there.
5. After merge to `main` and the image push, dispatch the dev Terraform apply the same way prod's `reclaw_comms_enabled` flag was rolled out (`gh workflow run terraform.yml` with `dry_run=false`). Verify via `aws ecs describe-tasks` and logs that dev's task is healthy, then confirm an MCP call against `ea_whoami` succeeds over the tailnet.
6. Prod pulls the same image tag as dev once it's pushed, so it picks up this merge's code, including the reconciled logging, even while `ENABLED_PROVIDERS` keeps it on `comms` only. Run the same reachability and `/health` check against prod's comms tools, in addition to dev, after that image lands there.
