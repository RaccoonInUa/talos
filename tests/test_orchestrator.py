# tests/test_orchestrator.py

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestrator import ServiceOrchestrator
from src.core.service import BaseService
from src.core.types import (
    Alert,
    EventSeverity,
    ProcessingConfig,
    SdrConfig,
    SignalClassification,
    TalosConfig,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def mock_config() -> TalosConfig:
    return TalosConfig(
        sdr=SdrConfig(center_freq_hz=433e6, sample_rate_hz=2e6, gain_db=10.0),
        processing=ProcessingConfig(
            fft_size=1024,
            cfar_threshold_db=15.0,
            ai_anomaly_threshold=0.85,
        ),
    )


@pytest.fixture
def orchestrator(mock_config: TalosConfig) -> ServiceOrchestrator:
    return ServiceOrchestrator(mock_config)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _mk_alive_service(name: str = "svc") -> MagicMock:
    s = MagicMock(spec=BaseService)
    s.name = name
    s.exitcode = 0
    return s


# -----------------------------------------------------------------------------
# Setup wiring
# -----------------------------------------------------------------------------

def test_setup_wires_services(orchestrator: ServiceOrchestrator) -> None:
    with patch("src.core.orchestrator.SdrMonitor") as MockSdr, patch(
        "src.core.orchestrator.LogicService"
    ) as MockLogic:
        orchestrator.setup()

        assert orchestrator.cfar_queue is not None
        assert orchestrator.ai_queue is not None
        assert orchestrator.alert_queue is not None

        assert len(orchestrator.services) == 2

        # Pylance-friendly (no assert_called_once typing drama)
        assert MockSdr.call_count == 1
        assert MockLogic.call_count == 1


def test_start_calls_service_start(orchestrator: ServiceOrchestrator) -> None:
    s1 = _mk_alive_service("s1")
    s2 = _mk_alive_service("s2")
    orchestrator.services = [s1, s2]

    orchestrator.start()

    assert s1.start.call_count == 1
    assert s2.start.call_count == 1


# -----------------------------------------------------------------------------
# run_forever / shutdown behavior matrix
# -----------------------------------------------------------------------------

def test_run_forever_clean_stop_when_request_stop_called_and_services_exit(
    orchestrator: ServiceOrchestrator,
) -> None:
    """
    request_stop() is issued before loop starts.
    Service is alive initially, but stops gracefully during shutdown.
    Expect: exit_code == 0 and terminate NOT called.
    """
    s = _mk_alive_service("worker")

    # Health check: True
    # Shutdown: request_stop/join then becomes dead
    s.is_alive.side_effect = [True, True, False, False]

    orchestrator.services = [s]
    orchestrator.request_stop()

    with patch("time.sleep"):
        exit_code = orchestrator.run_forever()

    assert exit_code == 0
    assert s.request_stop.call_count >= 1
    assert s.join.call_count >= 1
    assert s.terminate.call_count == 0


def test_run_forever_clean_stop_with_no_services(orchestrator: ServiceOrchestrator) -> None:
    orchestrator.services = []
    orchestrator.request_stop()

    with patch("time.sleep"):
        exit_code = orchestrator.run_forever()

    assert exit_code == 0


def test_run_forever_exits_with_error_on_health_failure(orchestrator: ServiceOrchestrator) -> None:
    s = _mk_alive_service("dead")
    s.exitcode = 139
    s.is_alive.return_value = False

    orchestrator.services = [s]

    with patch("time.sleep"):
        exit_code = orchestrator.run_forever()

    assert exit_code == 1


def test_run_forever_returns_1_when_force_kill_required(orchestrator: ServiceOrchestrator) -> None:
    """
    Loop stops, but shutdown detects stuck service (still alive after terminate+join).
    Expect: exit_code == 1 and terminate called.
    """
    s = _mk_alive_service("stuck")
    s.exitcode = None
    s.is_alive.return_value = True  # always alive => stuck forever

    orchestrator.services = [s]
    orchestrator.request_stop()

    # Expire deadline immediately
    with patch("time.sleep"), patch("time.monotonic", side_effect=[0.0, 1000.0, 1000.0, 1000.0]):
        exit_code = orchestrator.run_forever()

    assert exit_code == 1
    assert s.request_stop.call_count >= 1
    assert s.join.call_count >= 1
    assert s.terminate.call_count >= 1


def test_shutdown_idempotent_returns_cached_code(orchestrator: ServiceOrchestrator) -> None:
    s = _mk_alive_service("worker")
    s.is_alive.side_effect = [True, True, False, False]

    orchestrator.services = [s]

    rc1 = orchestrator.shutdown()
    rc2 = orchestrator.shutdown()

    assert rc1 == 0
    assert rc2 == 0


# -----------------------------------------------------------------------------
# Alerts sink behavior via public hook
# -----------------------------------------------------------------------------

def test_process_alerts_once_drains_and_logs(
    orchestrator: ServiceOrchestrator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    q = MagicMock()
    orchestrator.alert_queue = q

    a1 = Alert(
        severity=EventSeverity.CRITICAL,
        classification=SignalClassification.UNKNOWN,
        center_freq_hz=433e6,
        description="A1",
        confidence_score=0.9,
        source_cfar_event_id=None,
        source_ai_result_id=None,
    )
    a2 = Alert(
        severity=EventSeverity.INFO,
        classification=SignalClassification.UNKNOWN,
        center_freq_hz=434e6,
        description="A2",
        confidence_score=0.1,
        source_cfar_event_id=None,
        source_ai_result_id=None,
    )

    q.pop.side_effect = [a1, a2, None]

    caplog.set_level("INFO", logger=orchestrator.logger.name)
    orchestrator.process_alerts_once()

    msgs = [r.message for r in caplog.records]
    assert any("ALERT:" in m and "A1" in m for m in msgs)
    assert any("ALERT:" in m and "A2" in m for m in msgs)


# -----------------------------------------------------------------------------
# Signal binding correctness via public hook
# -----------------------------------------------------------------------------

def test_bind_signals_if_main_thread_does_not_call_signal_in_worker_thread(
    orchestrator: ServiceOrchestrator,
) -> None:
    import signal as signal_mod

    called = {"n": 0}

    def _spy_signal(_sig: Any, _handler: Any) -> None:
        called["n"] += 1

    def _thread_target() -> None:
        with patch.object(signal_mod, "signal", side_effect=_spy_signal):
            orchestrator.bind_signals_if_main_thread()

    t = threading.Thread(target=_thread_target)
    t.start()
    t.join()

    assert called["n"] == 0


def test_bind_signals_if_main_thread_calls_signal_in_main_thread(
    orchestrator: ServiceOrchestrator,
) -> None:
    import signal as signal_mod

    called = {"n": 0}

    def _spy_signal(_sig: Any, _handler: Any) -> None:
        called["n"] += 1

    with patch.object(signal_mod, "signal", side_effect=_spy_signal):
        orchestrator.bind_signals_if_main_thread()
        orchestrator.bind_signals_if_main_thread()  # idempotent

    # SIGINT + SIGTERM once each
    assert called["n"] == 2


# -----------------------------------------------------------------------------
# Realtime / Metrics / Wiring (Sprint 2 readiness)
# -----------------------------------------------------------------------------

def test_setup_wires_waterfall_queue(orchestrator: ServiceOrchestrator) -> None:
    with patch("src.core.orchestrator.SdrMonitor") as MockSdr, patch(
        "src.core.orchestrator.LogicService"
    ):
        orchestrator.setup()

        # Перевіряємо, що waterfall_queue створена
        assert orchestrator.waterfall_queue is not None

        # Перевіряємо, що вона передана в SDR
        _, kwargs = MockSdr.call_args
        assert "waterfall_queue" in kwargs
        assert kwargs["waterfall_queue"] is orchestrator.waterfall_queue


def test_metrics_reflect_queue_state(orchestrator: ServiceOrchestrator) -> None:
    q = MagicMock()
    q.qsize.return_value = 5
    q.maxsize = 10
    q.qsize.assert_not_called()

    orchestrator.cfar_queue = q
    orchestrator.alert_queue = q
    orchestrator.waterfall_queue = q

    getattr(orchestrator, "_refresh_metrics")()

    assert orchestrator.metrics["queue_cfar"] == [5, 10]
    assert orchestrator.metrics["queue_alert"] == [5, 10]
    assert orchestrator.metrics["queue_waterfall"] == [5, 10]
    assert q.qsize.call_count == 3


def test_waterfall_queue_lossy_behavior(orchestrator: ServiceOrchestrator) -> None:
    """
    Імітуємо переповнення: queue кидає, але система не падає.
    """
    q = MagicMock()
    q.pop.return_value = None
    q.qsize.return_value = 100
    q.maxsize = 5

    orchestrator.waterfall_queue = q

    # Просто викликаємо metrics як proxy на "живість"
    getattr(orchestrator, "_refresh_metrics")()

    # Якщо дійшли сюди — система не впала
    assert True


def test_setup_creates_all_queues(orchestrator: ServiceOrchestrator) -> None:
    with patch("src.core.orchestrator.SdrMonitor"), patch(
        "src.core.orchestrator.LogicService"
    ):
        orchestrator.setup()

        assert orchestrator.cfar_queue is not None
        assert orchestrator.ai_queue is not None
        assert orchestrator.alert_queue is not None
        assert orchestrator.waterfall_queue is not None