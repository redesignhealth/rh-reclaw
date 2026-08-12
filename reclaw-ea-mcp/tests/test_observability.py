"""Tests for observability.py's log_security_event -- new in this PR's
Argus round 1/2 fixes, previously untested (Argus round 2 finding)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
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


@pytest.fixture
def real_logging_pipeline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configures the REAL structlog/stdlib logging pipeline for a test,
    and guarantees it's restored afterward regardless of what the test
    does or raises.

    Argus round 4/5/6 findings, all folded into this one fixture instead
    of duplicated per-test:
    - round 4: no teardown at all initially -- config leaked between tests.
    - round 5: `configure_logging()` was OUTSIDE the guarded block (a raise
      there skipped teardown), and `structlog.reset_defaults()` alone
      doesn't undo `logging.basicConfig()`'s stdlib handler.
    - round 6: the three-step teardown (`reset_defaults`, restore
      handlers, restore level) ran sequentially with no isolation between
      steps -- an exception from the first would skip the rest, the exact
      leak this fixture exists to prevent. Each step now has its own
      `finally`. Handlers removed here are also explicitly `.close()`d
      (never done before), since `logging.basicConfig`'s `StreamHandler`
      participates in the stdlib `_handlerList` weak-ref registry and
      dropping the reference without closing it leaves a stale entry.
    - round 6 also flagged this test's ambient `LOG_LEVEL` dependency:
      `configure_logging()` reads it from the environment, and
      `log_security_event` logs at `warning` -- on a runner with
      `LOG_LEVEL=ERROR` the event is filtered out and the test fails with
      an opaque empty-output message. Pinned here so the fixture (and
      every test using it) is hermetic regardless of the ambient env.
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


def test_exc_info_renders_a_real_traceback_through_the_full_pipeline(
    real_logging_pipeline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Argus round 3 finding: the round-1/2 tests mocked `obs_log.warning`,
    so removing `ExceptionRenderer()` from the processor chain would leave
    them green -- no regression protection for the actual behavioral claim
    (that `exc_info=True` renders a traceback, not a bare `{"exc_info":
    true}`). This exercises the REAL configured pipeline end to end."""
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
    real_logging_pipeline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Argus round 5 finding: `severity` was verified only via a mock
    (`test_log_security_event_emits_structured_fields` above) -- this
    exercises the real structlog pipeline the way an actual CloudWatch
    Metric Filter would see it."""
    configure_logging()
    log_security_event("okta_id_token_rejected", reason="alg_none", severity="critical")

    output = capsys.readouterr().out
    assert output.strip(), _NO_OUTPUT_MESSAGE
    record = json.loads(output.strip().splitlines()[-1])
    assert record["event"] == "okta_id_token_rejected"
    assert record["severity"] == "critical"
    # Argus round 6 finding: the **fields passthrough (everything besides
    # `severity`) was never verified against the real pipeline.
    assert record["reason"] == "alg_none"
