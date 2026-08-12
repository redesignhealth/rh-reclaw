"""Tests for observability.py's log_security_event -- new in this PR's
Argus round 1/2 fixes, previously untested (Argus round 2 finding)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

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
    true}`). This exercises the REAL configured pipeline end to end."""
    configure_logging()
    try:
        raise ValueError("boom-for-traceback-test")
    except ValueError:
        log_security_event("test_exception_event", exc_info=True)

    output = capsys.readouterr().out
    record = json.loads(output.strip().splitlines()[-1])
    assert record["event"] == "test_exception_event"
    assert "exception" in record
    assert "ValueError" in record["exception"]
    assert "boom-for-traceback-test" in record["exception"]
