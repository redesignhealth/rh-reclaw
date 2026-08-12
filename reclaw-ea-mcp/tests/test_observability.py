"""Tests for observability.py's log_security_event -- new in this PR's
Argus round 1/2 fixes, previously untested (Argus round 2 finding)."""

from __future__ import annotations

from unittest.mock import patch

from observability import log_security_event, obs_log


def test_log_security_event_emits_structured_fields() -> None:
    with patch.object(obs_log, "warning") as mock_warning:
        log_security_event("okta_id_token_rejected", reason="alg_none", severity="critical")
    mock_warning.assert_called_once_with(
        "okta_id_token_rejected", reason="alg_none", severity="critical"
    )


def test_log_security_event_fallback_does_not_raise() -> None:
    """A failure inside the observability pipeline itself must never break
    the caller -- same contract every other log_* helper in this module
    has, verified for this one too."""
    with patch.object(obs_log, "warning", side_effect=RuntimeError("boom")):
        log_security_event("some_event", foo="bar")  # must not raise
