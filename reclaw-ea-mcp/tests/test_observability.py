"""Tests for observability.py's log_security_event -- new in this PR's
Argus round 1/2 fixes, previously untested (Argus round 2 finding)."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
import structlog

from observability import configure_logging, log_security_event, obs_log


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


def test_exc_info_renders_a_real_traceback_through_the_full_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argus round 3 finding: the round-1/2 tests mocked `obs_log.warning`,
    so removing `ExceptionRenderer()` from the processor chain would leave
    them green -- no regression protection for the actual behavioral claim
    (that `exc_info=True` renders a traceback, not a bare `{"exc_info":
    true}`). This exercises the REAL configured pipeline end to end.

    Argus round 4 finding: this is the first test in the suite to mutate
    structlog's process-global config (`configure_logging()`), with no
    teardown -- a `structlog.reset_defaults()` in a `try/finally` stops
    that config from silently leaking into whichever test happens to run
    next.

    Argus round 5 finding: `configure_logging()` itself was OUTSIDE the
    `try`, so a raise from it would skip teardown entirely; and
    `structlog.reset_defaults()` alone doesn't undo `logging.basicConfig`
    (which `configure_logging()` also calls) -- the stdlib root handler it
    installs could still leak into a later test. Both are fixed below:
    `configure_logging()` now runs inside the `try`, and `finally` also
    restores `logging.root`'s prior handlers/level."""
    root_logger = logging.getLogger()
    prior_handlers = list(root_logger.handlers)
    prior_level = root_logger.level
    try:
        configure_logging()
        try:
            raise ValueError("boom-for-traceback-test")
        except ValueError:
            log_security_event("test_exception_event", exc_info=True)

        output = capsys.readouterr().out
        # Argus round 4 finding: a prior test-ordering issue elsewhere in
        # the session caching structlog's PrintLogger against the real
        # (uncaptured) sys.stdout would make `output` empty here --
        # asserting this explicitly turns that into a clear failure
        # message instead of an opaque IndexError from splitlines()[-1].
        assert output.strip(), (
            "configure_logging() produced no captured stdout -- capsys may not be "
            "capturing structlog output; check test ordering and PrintLogger caching"
        )
        record = json.loads(output.strip().splitlines()[-1])
        assert record["event"] == "test_exception_event"
        assert "exception" in record
        assert "ValueError" in record["exception"]
        assert "boom-for-traceback-test" in record["exception"]
    finally:
        structlog.reset_defaults()
        root_logger.handlers = prior_handlers
        root_logger.setLevel(prior_level)


def test_severity_field_appears_in_real_json_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argus round 5 finding: `severity` was verified only via a mock
    (`test_log_security_event_emits_structured_fields` above) -- this
    exercises the real structlog pipeline the way an actual CloudWatch
    Metric Filter would see it."""
    root_logger = logging.getLogger()
    prior_handlers = list(root_logger.handlers)
    prior_level = root_logger.level
    try:
        configure_logging()
        log_security_event("okta_id_token_rejected", reason="alg_none", severity="critical")

        output = capsys.readouterr().out
        assert output.strip(), "configure_logging() produced no captured stdout"
        record = json.loads(output.strip().splitlines()[-1])
        assert record["event"] == "okta_id_token_rejected"
        assert record["severity"] == "critical"
    finally:
        structlog.reset_defaults()
        root_logger.handlers = prior_handlers
        root_logger.setLevel(prior_level)
