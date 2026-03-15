# tests/services/test_logic_core.py

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import MagicMock, patch

import pytest

from src.core.types import Alert, CfarEvent, EventSeverity
from src.services.logic_core import LogicService


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _mk_event(*, freq_hz: float = 433e6, snr_db: float = 10.0) -> CfarEvent:
    return CfarEvent(
        event_id=uuid4(),
        timestamp=_utc_now(),
        center_freq_hz=float(freq_hz),
        bandwidth_hz=12_500.0,
        power_db=-20.0,
        snr_db=float(snr_db),
        noise_floor_db=None,
        duration_s=None,
        duty_cycle=None,
    )


def _last_push_alert(out_q: MagicMock) -> Alert:
    assert out_q.push.call_count >= 1
    return out_q.push.call_args.args[0]


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

def test_execute_no_event_no_push() -> None:
    in_q = MagicMock()
    out_q = MagicMock()

    in_q.pop.return_value = None

    svc = LogicService(in_q, out_q)
    svc.execute()

    out_q.push.assert_not_called()


def test_execute_pushes_alert_when_event_not_ignored() -> None:
    in_q = MagicMock()
    out_q = MagicMock()

    e = _mk_event(freq_hz=433e6, snr_db=10.0)
    in_q.pop.return_value = e
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    with patch("time.monotonic", return_value=100.0):
        svc.execute()

    out_q.push.assert_called_once()
    alert = _last_push_alert(out_q)
    assert isinstance(alert, Alert)
    assert alert.source_cfar_event_id == e.event_id
    assert alert.center_freq_hz == float(e.center_freq_hz)


def test_execute_dedup_ignores_same_bucket_within_window() -> None:
    in_q = MagicMock()
    out_q = MagicMock()
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    e1 = _mk_event(freq_hz=433_000_000.0, snr_db=10.0)
    e2 = _mk_event(freq_hz=433_005_000.0, snr_db=10.0)  # same 10kHz bucket
    in_q.pop.side_effect = [e1, e2]

    with patch("time.monotonic", side_effect=[100.0, 101.0]):
        svc.execute()
        svc.execute()

    assert out_q.push.call_count == 1


def test_execute_dedup_allows_same_bucket_after_window() -> None:
    in_q = MagicMock()
    out_q = MagicMock()
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    e1 = _mk_event(freq_hz=433_000_000.0, snr_db=10.0)
    e2 = _mk_event(freq_hz=433_005_000.0, snr_db=10.0)  # same 10kHz bucket
    in_q.pop.side_effect = [e1, e2]

    with patch("time.monotonic", side_effect=[100.0, 103.0]):  # > window
        svc.execute()
        svc.execute()

    assert out_q.push.call_count == 2


def test_execute_dedup_allows_different_buckets_same_time() -> None:
    in_q = MagicMock()
    out_q = MagicMock()
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    e1 = _mk_event(freq_hz=433_000_000.0, snr_db=10.0)
    e2 = _mk_event(freq_hz=433_120_000.0, snr_db=10.0)  # different bucket
    in_q.pop.side_effect = [e1, e2]

    with patch("time.monotonic", side_effect=[100.0, 100.0]):
        svc.execute()
        svc.execute()

    assert out_q.push.call_count == 2


@pytest.mark.parametrize(
    "snr_db, expected",
    [
        (16.0, EventSeverity.CRITICAL),
        (15.1, EventSeverity.CRITICAL),
        (10.0, EventSeverity.WARNING),
        (8.1, EventSeverity.WARNING),
        (8.0, EventSeverity.INFO),
        (0.0, EventSeverity.INFO),
        (-5.0, EventSeverity.INFO),
    ],
)
def test_execute_severity_mapping_observable_via_push(snr_db: float, expected: EventSeverity) -> None:
    in_q = MagicMock()
    out_q = MagicMock()
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    e = _mk_event(freq_hz=433e6, snr_db=snr_db)
    in_q.pop.return_value = e

    with patch("time.monotonic", return_value=100.0):
        svc.execute()

    alert = _last_push_alert(out_q)
    assert alert.severity == expected


def test_execute_confidence_clamped_observable_via_push() -> None:
    in_q = MagicMock()
    out_q = MagicMock()
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    # low -> 0.0
    in_q.pop.return_value = _mk_event(freq_hz=433e6, snr_db=-100.0)
    with patch("time.monotonic", return_value=100.0):
        svc.execute()
    low = _last_push_alert(out_q)
    assert low.confidence_score == 0.0

    # high -> 1.0
    out_q.push.reset_mock()
    in_q.pop.return_value = _mk_event(freq_hz=433e6, snr_db=1_000.0)
    with patch("time.monotonic", return_value=200.0):
        svc.execute()
    high = _last_push_alert(out_q)
    assert high.confidence_score == 1.0

    # mid -> (0,1)
    out_q.push.reset_mock()
    in_q.pop.return_value = _mk_event(freq_hz=433e6, snr_db=10.0)
    with patch("time.monotonic", return_value=300.0):
        svc.execute()
    mid = _last_push_alert(out_q)
    assert 0.0 < mid.confidence_score < 1.0


def test_execute_logs_error_on_push_timeout(caplog: pytest.LogCaptureFixture) -> None:
    in_q = MagicMock()
    out_q = MagicMock()

    in_q.pop.return_value = _mk_event(freq_hz=433e6, snr_db=10.0)
    out_q.push.return_value = False

    svc = LogicService(in_q, out_q)

    caplog.set_level(logging.ERROR, logger=svc.logger.name)
    with patch("time.monotonic", return_value=100.0):
        svc.execute()

    assert any("Lost alert: DETECTED:" in rec.message for rec in caplog.records)


def test_execute_gc_prunes_old_keys_without_touching_privates() -> None:
    """
    We don't poke _last_alert_ts directly to keep Pylance happy.
    Instead, we:
      - create one entry at t=0 by executing an event (sets dedup key)
      - advance time far enough that this key becomes "old"
      - run enough ticks with no events to trigger GC
      - ensure a *new* event at same bucket is NOT ignored (meaning old key was pruned)
    """
    in_q = MagicMock()
    out_q = MagicMock()
    out_q.push.return_value = True

    svc = LogicService(in_q, out_q)

    e = _mk_event(freq_hz=433_000_000.0, snr_db=10.0)  # bucket B

    # 1) First execute at t=0 -> creates dedup entry and pushes
    in_q.pop.return_value = e
    with patch("time.monotonic", return_value=0.0):
        svc.execute()
    assert out_q.push.call_count == 1

    # 2) Move time far so entry becomes old for GC (cutoff = now - 6.0)
    # Use no-event executes to tick until GC triggers
    out_q.push.reset_mock()
    in_q.pop.return_value = None

    # Run GC tick at t=100.0
    with patch("time.monotonic", return_value=100.0):
        # Ensure we cross GC interval even if class constant changes later
        # We'll just run "a lot" of ticks without depending on private constants.
        for _ in range(1100):
            svc.execute()

    # 3) Now a new event in the same bucket at t=100.0 should NOT be ignored
    in_q.pop.return_value = e
    with patch("time.monotonic", return_value=100.0):
        svc.execute()

    assert out_q.push.call_count == 1