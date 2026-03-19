# tests/test_integration_logic_smoke.py
from __future__ import annotations

from unittest.mock import MagicMock, patch
import itertools

import pytest
import uuid

from src.services.logic_core import LogicService


def _mk_event(*, freq_hz: float, snr_db: float, event_id: str) -> MagicMock:
    """Minimal CFAR-like event without depending on CfarEvent ctor signature."""
    e = MagicMock()
    e.center_freq_hz = freq_hz
    e.snr_db = snr_db
    e.event_id = event_id
    return e


def test_logic_service_smoke_dedup_and_backpressure(caplog: pytest.LogCaptureFixture) -> None:
    """
    Smoke test (no SDR):
      - реально ганяє LogicService.execute()
      - доводить dedup на одному freq bucket (10 kHz)
      - доводить backpressure: output.push -> False і лог пише error
      - без private-інспекції

    Чому тут MagicMock-queues, а не TalosQueue?
      - Це найбільш стабільно: ми тестуємо поведінку LogicService (pop/push + time),
        а не реалізацію черги.
      - Для “реальної” інтеграції з TalosQueue краще робити окремий тест TalosQueue.
    """
    in_q = MagicMock()
    out_q = MagicMock()

    svc = LogicService(in_q, out_q)

    # Same 10kHz bucket (433_000_000 / 10_000 == 43300)
    e1 = _mk_event(freq_hz=433_000_000.0, snr_db=12.0, event_id=str(uuid.uuid4()))
    e2 = _mk_event(freq_hz=433_000_000.0, snr_db=12.0, event_id=str(uuid.uuid4()))
    e3 = _mk_event(freq_hz=433_000_000.0, snr_db=12.0, event_id=str(uuid.uuid4()))

    in_q.pop.side_effect = itertools.chain(
    [e1, e2, e3, None],          # твій список подій
    itertools.repeat(None)
)

    # First push succeeds; third push fails. Second execute must be dedup-skipped.
    out_q.push.side_effect = [True, False]

    caplog.set_level("ERROR", logger=svc.logger.name)

    # time.monotonic is called once per execute() via _should_ignore().
    with patch("time.monotonic", side_effect=[0.0, 1.0, 3.0]):
        svc.execute()  # allowed -> push(True)
        svc.execute()  # within dedup window -> ignored (no push)
        svc.execute()  # outside window -> push(False) + error log

    # Dedup proof: only 2 push attempts (first + third).
    assert out_q.push.call_count == 2

    # Backpressure proof: error log emitted when push() returns False.
    assert any(
        "Alert queue timeout" in rec.message and "Lost alert" in rec.message
        for rec in caplog.records
    )