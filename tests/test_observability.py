"""Tests for observability.py's structured logging helpers.

``hash_user`` is tested under its current (deliberately-not-yet-renamed)
name per a deferred follow-up ticket — see observability.py's own
docstring for why it returns the email local-part.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import structlog

import observability
from observability import (
    _resolve_log_level,
    configure_logging,
    hash_user,
    log_auth_flow,
    log_auth_rejected,
    log_scope_denial,
    log_tool_call,
    log_user_active,
)


class TestHashUser:
    def test_email_shaped_input_returns_local_part(self) -> None:
        assert hash_user("dan.costanza@redesignhealth.com") == "dan.costanza"

    def test_non_email_slug_shaped_input_passes_through_unchanged(self) -> None:
        assert hash_user("ea-agent-svc") == "ea-agent-svc"

    def test_strips_surrounding_whitespace(self) -> None:
        assert hash_user("  person@redesignhealth.com  ") == "person"


class TestConfigureLoggingIdempotent:
    def test_calling_twice_does_not_raise(self) -> None:
        configure_logging()
        configure_logging()  # must not error or otherwise blow up


class TestResolveLogLevel:
    def test_default_is_info(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("LOG_LEVEL", None)
            assert _resolve_log_level() == logging.INFO

    def test_debug_is_honored(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}):
            assert _resolve_log_level() == logging.DEBUG

    def test_lowercase_value_is_normalized(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "warning"}):
            assert _resolve_log_level() == logging.WARNING

    def test_invalid_value_falls_back_to_info(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "NOT_A_REAL_LEVEL"}):
            assert _resolve_log_level() == logging.INFO

    def test_configure_logging_applies_debug_level_to_structlog(self) -> None:
        """LOG_LEVEL=DEBUG must reach structlog's filtering wrapper, not just
        stdlib logging.basicConfig (the bug this round fixed)."""
        with patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}):
            configure_logging()
        # ``structlog.get_logger()`` returns a lazy proxy; ``.bind()``
        # resolves it against the just-applied configuration so
        # ``is_enabled_for`` reflects the real filtering level.
        bound_logger = structlog.get_logger().bind()
        assert bound_logger.is_enabled_for(logging.DEBUG)

        # Restore INFO so later tests (and the module-level default) are
        # not left running at DEBUG.
        configure_logging()


class TestFallbackLoggerPaths:
    """Every ``log_*`` helper swallows its own failures — force the primary
    ``obs_log.info`` call to raise and assert the fallback path executes
    without the exception propagating to the caller."""

    def test_log_tool_call_fallback_does_not_raise(self) -> None:
        with patch.object(observability.obs_log, "info", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_tool_call("some_tool", 12.3, True)
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_log_user_active_fallback_does_not_raise(self) -> None:
        with patch.object(observability.obs_log, "info", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_user_active("person@redesignhealth.com")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_log_auth_flow_fallback_does_not_raise_and_uses_exc_info_true(self) -> None:
        """Regression test for this round's fix: ``log_auth_flow``'s fallback
        previously passed ``exc_info=False``, inconsistent with every other
        helper's fallback in this module."""
        with patch.object(observability.obs_log, "info", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_auth_flow("new_auth")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_log_auth_rejected_fallback_does_not_raise(self) -> None:
        with patch.object(observability.obs_log, "info", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_auth_rejected("sub_missing")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_log_scope_denial_fallback_does_not_raise(self) -> None:
        with patch.object(observability.obs_log, "info", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_scope_denial(tool="comms_whoami", reason="missing_token", client_id="unknown")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True
