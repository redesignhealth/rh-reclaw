"""Tests for providers/registry.py's plugin resolution/loading.

``pytest.importorskip`` guards the tests that actually ``.load()`` the
``ea`` provider (its extra dependency chain -- reclaw_ea, scheduler_mcp --
isn't installed by a plain ``uv sync``, see ea-deps/ and
scripts/install-ea-deps.sh); the resolution-only and lazy-import tests
don't need it at all.
"""

from __future__ import annotations

import sys

import pytest

from providers.registry import resolve_enabled_providers


class TestResolveEnabledProviders:
    def test_unknown_provider_name_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Unknown provider"):
            resolve_enabled_providers("comms,bogus")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(RuntimeError, match="empty list"):
            resolve_enabled_providers("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(RuntimeError, match="empty list"):
            resolve_enabled_providers("   ,  ,")

    def test_duplicated_name_resolves_to_a_single_spec(self) -> None:
        specs = resolve_enabled_providers("comms,comms")
        assert [s.name for s in specs] == ["comms"]

    def test_comms_alone_resolves_to_comms_only(self) -> None:
        specs = resolve_enabled_providers("comms")
        assert [s.name for s in specs] == ["comms"]
        assert specs[0].requires_db is True

    def test_order_and_dedup_preserve_first_occurrence(self) -> None:
        specs = resolve_enabled_providers("ea,comms,ea")
        assert [s.name for s in specs] == ["ea", "comms"]


class TestLazyImport:
    def test_importing_registry_alone_never_imports_providers_ea(self) -> None:
        """The registry itself must never import providers.ea at module
        load time -- prod (which never enables `ea`) must not depend on
        providers.ea's heavy dependency chain (reclaw_ea, scheduler_mcp,
        rh-auth) succeeding at import time just because
        providers/registry.py got imported. Resolving (without loading)
        must not trigger it either.

        Only checks providers.ea, not providers.comms: sys.modules is
        process-global, and some other test in this session may well have
        already imported providers.comms (a cheap, always-needed import,
        unlike ea's chain) -- that's not the invariant this test protects."""
        sys.modules.pop("providers.ea", None)
        sys.modules.pop("providers.registry", None)

        import providers.registry as registry

        assert "providers.ea" not in sys.modules

        registry.resolve_enabled_providers("comms,ea")

        assert "providers.ea" not in sys.modules, (
            "resolve_enabled_providers must not import provider modules -- only .load() should"
        )


class TestLoadComms:
    def test_comms_loads_its_own_server_and_scopes(self) -> None:
        (spec,) = resolve_enabled_providers("comms")
        server, tool_scopes, resource_scopes = spec.load()

        from providers.comms import RESOURCE_SCOPES, TOOL_SCOPES, comms_server

        assert server is comms_server
        assert tool_scopes == TOOL_SCOPES
        assert resource_scopes == RESOURCE_SCOPES


class TestLoadEa:
    def test_ea_loads_its_own_server_and_scopes(self) -> None:
        pytest.importorskip("reclaw_ea")
        pytest.importorskip("scheduler_mcp")

        (spec,) = resolve_enabled_providers("ea")
        server, tool_scopes, resource_scopes = spec.load()

        from providers.ea import RESOURCE_SCOPES, TOOL_SCOPES, ea_server

        assert server is ea_server
        assert tool_scopes == TOOL_SCOPES
        assert resource_scopes == RESOURCE_SCOPES
        assert spec.requires_db is False

    def test_comms_and_ea_together_have_no_scope_key_collisions(self) -> None:
        pytest.importorskip("reclaw_ea")
        pytest.importorskip("scheduler_mcp")

        specs = resolve_enabled_providers("comms,ea")
        assert [s.name for s in specs] == ["comms", "ea"]

        loaded = [spec.load() for spec in specs]
        merged_tool_scopes: dict[str, str] = {}
        for _server, tool_scopes, _resource_scopes in loaded:
            collisions = set(merged_tool_scopes) & set(tool_scopes)
            assert not collisions, f"scope key collision across providers: {collisions}"
            merged_tool_scopes.update(tool_scopes)

        assert any(k.startswith("comms_") for k in merged_tool_scopes)
        assert any(k.startswith("ea_") for k in merged_tool_scopes)
