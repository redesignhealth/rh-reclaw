"""Tests for observability.py's structured logging helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from unittest.mock import patch

import pytest
import structlog

import observability
from observability import (
    _resolve_log_level,
    configure_logging,
    email_local_part,
    log_auth_flow,
    log_auth_rejected,
    log_scope_denial,
    log_security_event,
    log_tool_call,
    log_user_active,
    obs_log,
)


class TestEmailLocalPart:
    def test_email_shaped_input_returns_local_part(self) -> None:
        assert email_local_part("dan.costanza@redesignhealth.com") == "dan.costanza"

    def test_non_email_slug_shaped_input_passes_through_unchanged(self) -> None:
        assert email_local_part("ea-agent-svc") == "ea-agent-svc"

    def test_strips_surrounding_whitespace(self) -> None:
        assert email_local_part("  person@redesignhealth.com  ") == "person"


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

    def test_invalid_value_logs_a_warning(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "DBG"}):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                _resolve_log_level()
        mock_warning.assert_called_once_with("Invalid LOG_LEVEL=%r; falling back to INFO", "DBG")

    def test_notset_falls_back_to_info(self) -> None:
        """LOG_LEVEL=NOTSET resolves via getattr to 0, which passes a naive
        isinstance(level, int) check but is structlog's "log everything"
        sentinel, not a real filtering threshold -- must fall back to INFO
        rather than disabling filtering entirely."""
        with patch.dict("os.environ", {"LOG_LEVEL": "NOTSET"}):
            assert _resolve_log_level() == logging.INFO

    def test_notset_logs_a_warning(self) -> None:
        with patch.dict("os.environ", {"LOG_LEVEL": "NOTSET"}):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                _resolve_log_level()
        mock_warning.assert_called_once_with("Invalid LOG_LEVEL=%r; falling back to INFO", "NOTSET")

    def test_invalid_value_does_not_pollute_root_logger_handlers(self) -> None:
        """Regression test for the bug this round fixed: the bare
        ``logging.warning()`` convenience function implicitly calls
        ``logging.basicConfig()`` (installing a root handler) the first
        time it's invoked if the root logger has no handlers yet -- which
        would make the later, real ``logging.basicConfig(level=level)`` in
        ``configure_logging`` a no-op. Routing the fallback warning through
        the module-scoped ``_fallback_logger`` instead must not trigger that
        side effect, even with no patch around the warning call itself."""
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        try:
            # Force the clean "no handlers" precondition this test actually
            # needs: under normal pytest ordering an earlier test may have
            # already installed a root handler (e.g. via configure_logging()
            # elsewhere in the session), which would make the bug this test
            # guards against invisible even if it regressed.
            root.handlers[:] = []
            with patch.dict("os.environ", {"LOG_LEVEL": "NOT_A_REAL_LEVEL"}):
                _resolve_log_level()
            assert root.handlers == []
        finally:
            root.handlers[:] = handlers_before

    def test_configure_logging_applies_debug_level_to_structlog(self) -> None:
        """LOG_LEVEL=DEBUG must reach structlog's filtering wrapper, not just
        stdlib logging.basicConfig (the bug this round fixed)."""
        try:
            with patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}):
                configure_logging()
            # ``structlog.get_logger()`` returns a lazy proxy; ``.bind()``
            # resolves it against the just-applied configuration so
            # ``is_enabled_for`` reflects the real filtering level.
            bound_logger = structlog.get_logger().bind()
            assert bound_logger.is_enabled_for(logging.DEBUG)
        finally:
            # Restore INFO so later tests (and the module-level default) are
            # not left running at DEBUG even if the assertion above fails --
            # structlog's configuration is process-global, not per-test.
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
        """``log_auth_rejected`` logs at ``warning`` (an access-control
        failure, not routine traffic), so the forced failure is on
        ``obs_log.warning``, not ``.info`` like the helpers above."""
        with patch.object(observability.obs_log, "warning", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_auth_rejected("sub_missing")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_log_scope_denial_fallback_does_not_raise(self) -> None:
        """Same reasoning as ``log_auth_rejected`` above: this helper logs
        at ``warning``."""
        with patch.object(observability.obs_log, "warning", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_scope_denial(tool="comms_whoami", reason="missing_token", client_id="unknown")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_log_security_event_fallback_does_not_raise(self) -> None:
        with patch.object(observability.obs_log, "warning", side_effect=RuntimeError("boom")):
            with patch.object(observability._fallback_logger, "warning") as mock_warning:
                log_security_event("some_event", foo="bar")
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs.get("exc_info") is True


class TestLogSecurityEvent:
    def test_emits_structured_fields(self) -> None:
        with patch.object(obs_log, "warning") as mock_warning:
            log_security_event("okta_id_token_rejected", reason="alg_none", severity="critical")
        mock_warning.assert_called_once_with(
            "okta_id_token_rejected", reason="alg_none", severity="critical"
        )

    def test_severity_omitted_when_not_passed(self) -> None:
        with patch.object(obs_log, "warning") as mock_warning:
            log_security_event("some_event", foo="bar")
        mock_warning.assert_called_once_with("some_event", foo="bar")


@pytest.fixture
def real_logging_pipeline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configures the REAL structlog/stdlib logging pipeline for a test,
    and guarantees it's restored afterward regardless of what the test
    does or raises.

    Each teardown step gets its own ``finally`` so an exception in one
    step can't skip the rest -- the exact leak this fixture exists to
    prevent. Handlers removed here are also explicitly ``.close()``d:
    ``logging.basicConfig``'s ``StreamHandler`` participates in the
    stdlib ``_handlerList`` weak-ref registry, and dropping the reference
    without closing it leaves a stale entry.

    ``LOG_LEVEL`` is pinned to ``INFO`` so this fixture (and every test
    using it) is hermetic regardless of the ambient env: ``obs_log``
    events log at ``warning``, so a runner with ``LOG_LEVEL=ERROR`` would
    otherwise filter them out and fail with an opaque empty-output message.
    """
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    root_logger = logging.getLogger()
    prior_handlers = list(root_logger.handlers)
    prior_level = root_logger.level
    try:
        yield
    finally:
        try:
            structlog.reset_defaults()
        finally:
            try:
                for handler in set(root_logger.handlers) - set(prior_handlers):
                    root_logger.removeHandler(handler)
                    handler.close()
            finally:
                root_logger.setLevel(prior_level)


_NO_OUTPUT_MESSAGE = (
    "configure_logging() produced no captured stdout -- capsys may not be "
    "capturing structlog output; check test ordering and PrintLogger caching"
)


class TestRealLoggingPipeline:
    """Exercises the REAL configured structlog/stdlib pipeline end to end,
    not a mocked ``obs_log`` -- mocking would leave these behavioral claims
    (ExceptionRenderer actually renders a traceback; severity/extra fields
    actually reach the JSON output) with no regression protection: removing
    ExceptionRenderer from the processor chain would leave a mock-based
    test green."""

    def test_exc_info_renders_a_real_traceback_through_the_full_pipeline(
        self, real_logging_pipeline: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging()
        try:
            raise ValueError("boom-for-traceback-test")
        except ValueError:
            log_security_event("test_exception_event", exc_info=True)

        output = capsys.readouterr().out
        assert output.strip(), _NO_OUTPUT_MESSAGE
        record = json.loads(output.strip().splitlines()[-1])
        assert record["event"] == "test_exception_event"
        assert "exception" in record
        assert "ValueError" in record["exception"]
        assert "boom-for-traceback-test" in record["exception"]

    def test_severity_field_appears_in_real_json_output(
        self, real_logging_pipeline: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging()
        log_security_event("okta_id_token_rejected", reason="alg_none", severity="critical")

        output = capsys.readouterr().out
        assert output.strip(), _NO_OUTPUT_MESSAGE
        record = json.loads(output.strip().splitlines()[-1])
        assert record["event"] == "okta_id_token_rejected"
        assert record["severity"] == "critical"
        # The **fields passthrough (everything besides severity) also
        # needs verifying against the real pipeline, not just a mock.
        assert record["reason"] == "alg_none"
