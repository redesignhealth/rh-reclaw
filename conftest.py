"""Pytest configuration — flat-layout imports + auth env defaults.

Mirrors rh-mcp's conftest: the service is a flat-layout app (main.py,
providers/, etc.), so the service root goes on sys.path, and the env vars
required by ``auth.build_auth_provider()`` get dummy values so module-level
FastMCP construction doesn't raise when tests import ``main``.
"""

import os
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).parent
sys.path.insert(0, str(SERVICE_ROOT))


@pytest.fixture(autouse=True)
def _auth_env(tmp_path: Path) -> None:
    """Point token storage at a temp dir and default the required auth env.

    The FileTree sanitization strategies call os.pathconf on the storage
    directory at construction time, so the path must exist.
    """
    os.environ["MCP_TOKEN_STORAGE_PATH"] = str(tmp_path)
    os.environ.setdefault("OKTA_ISSUER_URL", "https://example.okta.com/oauth2/default")
    os.environ.setdefault("OKTA_CLIENT_ID", "test-client-id")
    os.environ.setdefault("OKTA_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("BASE_URL", "http://localhost:8080")
    os.environ.setdefault("MCP_JWT_SECRET", "test-jwt-secret-for-unit-tests-only")
    os.environ.setdefault("RH_AUTH_SECRET", "test-rh-auth-secret-long-enough-for-hs256")
