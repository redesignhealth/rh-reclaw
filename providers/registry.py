"""Plugin registry for reclaw-comms-mcp's mountable providers.

``main.py`` reads ``ENABLED_PROVIDERS`` at startup, resolves it against
``_REGISTRY`` below, and mounts exactly the providers that name lists.
Adding a future agent means writing one new ``providers/<name>.py`` module
and one ``_REGISTRY`` entry -- ``main.py`` never changes.

The registry itself must never import ``providers.comms`` or
``providers.ea`` at module load time. ``providers/ea.py`` builds
``FakeBoard()``, ``InMemoryRuleStore()``, and its FastMCP server as
module-level state, and pulls in the ``reclaw_ea``/``scheduler_mcp``/
``rh-auth`` dependency chain (installed separately from this project's own
``uv.lock`` -- see ``ea-deps/`` and ``scripts/install-ea-deps.sh``). Since
prod never enables ``ea``, an eager import at registry-load time would mean
prod's comms-only process still depends on that entire chain succeeding at
startup. Each ``_load_<name>`` function imports its provider module only
inside its own body, so the import happens exactly once, only when that
provider is actually resolved as enabled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastmcp import FastMCP


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
    "ea": ProviderSpec("ea", _load_ea, requires_db=False),
}


def resolve_enabled_providers(raw: str) -> list[ProviderSpec]:
    """Parse and validate a comma-separated ``ENABLED_PROVIDERS`` value.

    Fails closed: an unknown provider name or an empty resolved list raises
    immediately, before any provider actually loads or the FastMCP app /
    auth provider gets built -- a config typo must crash the process at
    startup, never silently drop a provider.

    ``dict.fromkeys`` on the split names removes duplicates while keeping
    first-occurrence order, so ``"comms,comms"`` resolves to a single mount
    instead of double-mounting the same namespace.
    """
    names = list(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        raise RuntimeError(f"Unknown provider(s) in ENABLED_PROVIDERS: {unknown}")
    if not names:
        raise RuntimeError("ENABLED_PROVIDERS resolved to empty list")
    return [_REGISTRY[n] for n in names]


__all__ = ["ProviderSpec", "resolve_enabled_providers"]
