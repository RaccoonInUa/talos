# src/core/orchestrator.py

from __future__ import annotations

import logging
import signal
import threading
import time
import asyncio

from collections import deque
from dataclasses import dataclass
from typing import Deque, Set, List, Optional, Any

from src.core.bus import TalosQueue
from src.core.service import BaseService
from src.core.types import Alert, AiAnomalyResult, CfarEvent, EventSeverity, TalosConfig, WaterfallFrame
from src.services.logic_core import LogicService
from src.services.sdr_monitor import SdrMonitor



@dataclass(frozen=True)
class EventSubscriber:
    queue: asyncio.Queue[Alert]
    loop: asyncio.AbstractEventLoop




class ServiceOrchestrator:
    """
    Supervisor Process (Main Thread).
    Manages services lifecycle and explicit IPC channels.
    """

    _SHUTDOWN_TIMEOUT_S = 2.0
    _MONITOR_INTERVAL_S = 0.5

    # Batch limits to prevent loop starvation
    _ALERTS_BATCH_SIZE = 50
    _AI_BATCH_SIZE = 20

    # Icons are explicitly mapped (no DTO coupling)
    _ICON_BY_SEVERITY: dict[EventSeverity, str] = {
        EventSeverity.CRITICAL: "🔴",
        EventSeverity.WARNING: "🟠",
        EventSeverity.INFO: "🔵",
    }

    def __init__(self, config: TalosConfig):
        self.config = config
        self.logger = logging.getLogger("talos.core.orchestrator")
        self.services: List[BaseService] = []

        # Explicit Channels
        self.cfar_queue: Optional[TalosQueue[CfarEvent]] = None
        self.ai_queue: Optional[TalosQueue[AiAnomalyResult]] = None
        self.alert_queue: Optional[TalosQueue[Alert]] = None
        self.waterfall_queue: Optional[TalosQueue[WaterfallFrame]] = None

        # Lifecycle flags
        self._stop_requested = False
        self._shutdown_started = False
        self._shutdown_exit_code = 0

        # Signals are bound in run_forever() in main thread only
        self._signals_bound = False

        # UI state snapshots
        self._recent_alerts: Deque[Alert] = deque(maxlen=100)
        self._latest_waterfall_frame: Optional[WaterfallFrame] = None

        # Thread-safe pub/sub for WS clients
        self._event_subscribers: Set[EventSubscriber] = set()
        self._subscribers_lock = threading.Lock()

        # Metrics snapshot (API)
        self.metrics: dict[str, object] = {
            "frames_ok": 0,
            "frames_skipped": 0,
            "cfar_detections": 0,
            "clusters_emitted": 0,
            "events_emitted": 0,
            "waterfall_frames_emitted": 0,
            "waterfall_frames_dropped": 0,
            "queue_cfar": [0, 0],
            "queue_alert": [0, 0],
            "queue_waterfall": [0, 0],
        }

    # ---------------------------------------------------------------------
    # Public hooks (production-friendly, test-friendly)
    # ---------------------------------------------------------------------

    def bind_signals_if_main_thread(self) -> None:
        """
        Public hook.
        Installs SIGINT/SIGTERM handlers only when running in the main thread.

        Production rule:
          - install signal handlers ONLY in the main thread
          - do it at runtime, not in __init__
        """
        self._bind_signals_if_main_thread()

    def request_stop(self) -> None:
        """
        Public hook.
        Allows external code/tests to stop the supervisor loop without signals.
        """
        self._stop_requested = True

    def process_alerts_once(self) -> int:
        """
        Public hook.
        Processes up to _ALERTS_BATCH_SIZE alerts once and returns how many were consumed.
        Useful for tests and for embedding the orchestrator into other runtimes.
        """
        return self._process_alerts()

    @property
    def hal_state(self) -> str:
        for s in self.services:
            if isinstance(s, SdrMonitor):
                return "SCANNING" if s.is_alive() else "DISCONNECTED"
        return "UNKNOWN"

    def get_recent_alerts(self) -> list[Alert]:
        return list(self._recent_alerts)

    def get_latest_waterfall_frame(self) -> Optional[WaterfallFrame]:
        self._drain_waterfall_queue()
        return self._latest_waterfall_frame

    def subscribe_events(self) -> asyncio.Queue[Alert]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[Alert] = asyncio.Queue(maxsize=100)
        sub = EventSubscriber(queue=q, loop=loop)
        with self._subscribers_lock:
            self._event_subscribers.add(sub)
        return q

    def unsubscribe_events(self, q: asyncio.Queue[Alert]) -> None:
        with self._subscribers_lock:
            doomed = [sub for sub in self._event_subscribers if sub.queue is q]
            for sub in doomed:
                self._event_subscribers.discard(sub)

    def _prune_dead_subscribers(self) -> None:
        with self._subscribers_lock:
            self._event_subscribers = {
                sub for sub in self._event_subscribers if not sub.loop.is_closed()
            }

    # ---------------------------------------------------------------------
    # Signal handling (production-correct)
    # ---------------------------------------------------------------------

    def _bind_signals_if_main_thread(self) -> None:
        if self._signals_bound:
            return

        if threading.current_thread() is not threading.main_thread():
            self.logger.warning(
                "Signal handlers not installed (not in main thread). "
                "Shutdown must be triggered via request_stop() / external control."
            )
            return

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        self._signals_bound = True
        self.logger.debug("Signal handlers bound (SIGINT/SIGTERM).")

    def _handle_signal(self, signum: int, frame: object) -> None:
        # Never run shutdown inside signal handler. Only request stop.
        try:
            sig_name = signal.Signals(signum).name
        except Exception:
            sig_name = str(signum)

        self.logger.info("Received signal %s. Requesting stop...", sig_name)
        self._stop_requested = True

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------

    def setup(self) -> None:
        self.logger.info("Setting up Talos services...")

        # Explicit typed queues
        self.cfar_queue = TalosQueue(CfarEvent, maxsize=1000, name="cfar_events")
        self.ai_queue = TalosQueue(AiAnomalyResult, maxsize=100, name="ai_results")
        self.alert_queue = TalosQueue(Alert, maxsize=50, name="alerts")
        # Waterfall telemetry queue (lossy, UI-only path)
        self.waterfall_queue = TalosQueue(WaterfallFrame, maxsize=5, name="waterfall_frames")

        # Stage 1: SDR Monitor (Producer)
        sdr_service = SdrMonitor(
            sdr_config=self.config.sdr,
            dsp_config=self.config.processing,
            output_queue=self.cfar_queue,
            global_config=self.config,
            waterfall_queue=self.waterfall_queue,
        )
        self.services.append(sdr_service)

        # Stage 2: Logic Core (Consumer/Producer)
        logic_service = LogicService(
            input_queue=self.cfar_queue,
            output_queue=self.alert_queue,
        )
        self.services.append(logic_service)

        self.logger.info("Pipeline wired: SDR -> Logic -> Alerts (+Waterfall Telemetry)")

    def start(self) -> None:
        if not self.services:
            raise RuntimeError("Setup not called")

        for s in self.services:
            s.start()

        self.logger.info("All services started.")

    def run_forever(self) -> int:
        """
        Main Loop. Returns exit code:
          0 = OK / Clean shutdown
          1 = Error / Force-kill required / Unexpected exception
        """
        self._bind_signals_if_main_thread()
        self.logger.info("System operational. Press Ctrl+C to stop.")

        exit_code = 0
        try:
            while not self._stop_requested:
                if not self._check_health():
                    exit_code = 1
                    self._stop_requested = True
                    break

                self._refresh_metrics()
                self._drain_waterfall_queue()
                self._process_alerts()
                self._drain_ai_stub()
                time.sleep(self._MONITOR_INTERVAL_S)

        except KeyboardInterrupt:
            self.logger.info("KeyboardInterrupt.")
        except Exception:
            self.logger.exception("Supervisor error")
            exit_code = 1

        shutdown_code = self.shutdown()
        return max(exit_code, shutdown_code)

    def _check_health(self) -> bool:
        for s in self.services:
            if not s.is_alive():
                self.logger.critical("Service '%s' died (exitcode=%s).", s.name, s.exitcode)
                return False
        return True

    def _process_alerts(self) -> int:
        if not self.alert_queue:
            return 0

        count = 0
        while count < self._ALERTS_BATCH_SIZE:
            alert = self.alert_queue.pop(timeout=0.0)
            if not alert:
                break

            self._recent_alerts.append(alert)
            self._publish_alert(alert)

            icon = self._ICON_BY_SEVERITY.get(alert.severity, "⚪")
            self.logger.info(
                "%s ALERT: %s (%.3f MHz, conf=%.2f, class=%s)",
                icon,
                alert.description,
                float(alert.center_freq_hz) / 1e6,
                float(alert.confidence_score),
                alert.classification,
            )
            count += 1

        return count

    def _publish_alert(self, alert: Alert) -> None:
        self._prune_dead_subscribers()
        with self._subscribers_lock:
            subscribers = tuple(self._event_subscribers)

        for sub in subscribers:
            try:
                sub.loop.call_soon_threadsafe(self._offer_alert_to_subscriber, sub, alert)
            except RuntimeError:
                with self._subscribers_lock:
                    self._event_subscribers.discard(sub)

    @staticmethod
    def _offer_alert_to_subscriber(sub: EventSubscriber, alert: Alert) -> None:
        try:
            sub.queue.put_nowait(alert)
        except asyncio.QueueFull:
            pass

    def _drain_waterfall_queue(self) -> None:
        if not self.waterfall_queue:
            return

        while True:
            frame = self.waterfall_queue.pop(timeout=0.0)
            if not frame:
                break
            self._latest_waterfall_frame = frame

    def _refresh_metrics(self) -> None:
        if self.cfar_queue:
            q: Any = self.cfar_queue
            self.metrics["queue_cfar"] = [int(q.qsize()), int(q.maxsize)]
        if self.alert_queue:
            q: Any = self.alert_queue
            self.metrics["queue_alert"] = [int(q.qsize()), int(q.maxsize)]
        if self.waterfall_queue:
            q: Any = self.waterfall_queue
            self.metrics["queue_waterfall"] = [int(q.qsize()), int(q.maxsize)]

    def _drain_ai_stub(self) -> None:
        """
        Future-proofing: keep AI queue from blocking if something produces into it.
        Batch limited to prevent starvation.
        """
        if not self.ai_queue:
            return

        count = 0
        while count < self._AI_BATCH_SIZE:
            res = self.ai_queue.pop(timeout=0.0)
            if not res:
                break
            count += 1

    def shutdown(self) -> int:
        """
        Stops services. Returns:
          0 on clean exit
          1 if force kill was required

        Idempotent: safe to call multiple times.
        """
        if self._shutdown_started:
            return self._shutdown_exit_code

        self._shutdown_started = True
        self.logger.info("Stopping services...")

        # 1) Request stop
        for s in self.services:
            if s.is_alive():
                s.request_stop()

        # 2) Join (graceful) with monotonic deadline
        deadline = time.monotonic() + self._SHUTDOWN_TIMEOUT_S
        for s in self.services:
            if s.is_alive():
                remaining = deadline - time.monotonic()
                s.join(timeout=max(0.1, remaining))

        # 3) Terminate (force kill) + join to avoid zombies
        stuck: List[BaseService] = []
        for s in self.services:
            if s.is_alive():
                self.logger.warning("Service %s stuck. Sending SIGKILL.", s.name)
                s.terminate()
                s.join(timeout=1.0)
                if s.is_alive():
                    stuck.append(s)

        if stuck:
            self.logger.error("Shutdown complete with ERRORS (stuck services=%d).", len(stuck))
            self._shutdown_exit_code = 1
            return 1

        self.logger.info("Shutdown complete (Clean).")
        self._shutdown_exit_code = 0
        return 0