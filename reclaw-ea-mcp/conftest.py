"""Pytest configuration -- flat-layout imports + auth env defaults.

Identical to reclaw-comms-mcp/conftest.py one directory up (same fleet
idiom, mirrors rh-mcp's conftest): the service is a flat-layout app
(main.py, providers/, etc.), so the service root goes on sys.path, and the
env vars required by ``auth.build_auth_provider()`` get dummy values so
module-level FastMCP construction doesn't raise when tests import ``main``.
"""

import os
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).parent
sys.path.insert(0, str(SERVICE_ROOT))


@pytest.fixture(autouse=True)
def _auth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point token storage at a temp dir and default the required auth env.

    The FileTree sanitization strategies call os.pathconf on the storage
    directory at construction time, so the path must exist.
    """
    monkeypatch.setenv("MCP_TOKEN_STORAGE_PATH", str(tmp_path))
    _setdefault(monkeypatch, "OKTA_ISSUER_URL", "https://example.okta.com/oauth2/default")
    _setdefault(monkeypatch, "OKTA_CLIENT_ID", "test-client-id")
    _setdefault(monkeypatch, "OKTA_CLIENT_SECRET", "test-client-secret")
    _setdefault(monkeypatch, "BASE_URL", "http://localhost:8081")
    _setdefault(monkeypatch, "MCP_JWT_SECRET", "test-jwt-secret-for-unit-tests-only")
    _setdefault(monkeypatch, "RH_AUTH_SECRET", "test-rh-auth-secret-long-enough-for-hs256")


@pytest.fixture(autouse=True)
def _reset_ea_provider_state() -> None:
    """Reset providers.ea's process-global registries between tests.

    ``_negotiators``/``_rule_store``/``_rules_seeded`` are module-level
    (one multi-tenant service, per TECH-5065) -- without resetting them,
    one test's owner identity would carry ledger/negotiation state into
    the next test that happens to reuse the same identity string.
    """
    from reclaw_ea.fake_board import FakeBoard
    from scheduler_mcp.rules import InMemoryRuleStore

    import providers.ea as ea

    ea._negotiators.clear()
    ea._rules_seeded.clear()
    ea._rule_store = InMemoryRuleStore()
    ea._board = FakeBoard()


def _setdefault(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """``monkeypatch.setenv`` equivalent of ``os.environ.setdefault``."""
    if name not in os.environ:
        monkeypatch.setenv(name, value)
