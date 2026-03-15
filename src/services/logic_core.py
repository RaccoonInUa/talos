# src/services/logic_core.py

from __future__ import annotations

import time
from typing import Dict

from src.core.bus import TalosQueue
from src.core.service import BaseService
from src.core.types import Alert, CfarEvent, EventSeverity, SignalClassification


class LogicService(BaseService):
    """
    Logic Core Service (Signal Analysis & Filtering).
    Consumes CfarEvent -> Produces Alert (Level 3 DTO).

    Production notes:
    - Dedup per ~10kHz bucket (cheap and effective for MVP).
    - Blocking push with timeout for Alerts (critical path).
    - Periodic pruning prevents unbounded dedup dict growth.
    """

    _DEDUP_WINDOW_S = 2.0
    _GC_INTERVAL_TICKS = 1000
    _ALERT_PUSH_TIMEOUT_S = 0.5
    _CONFIDENCE_SNR_NORM_DB = 20.0  # heuristic normalization for confidence_score

    def __init__(
        self,
        input_queue: TalosQueue[CfarEvent],
        output_queue: TalosQueue[Alert],
    ):
        super().__init__(name="logic_core", loop_sleep_s=0.01)
        self.input = input_queue
        self.output = output_queue

        # {freq_bucket_10khz: last_seen_monotonic_ts}
        self._last_alert_ts: Dict[int, float] = {}
        self._tick_counter: int = 0

    def setup(self) -> None:
        self.logger.info(
            "Logic Core initialized. Dedup window: %.1fs", self._DEDUP_WINDOW_S
        )

    def execute(self) -> None:
        # 1) Housekeeping (Garbage Collection)
        self._tick_counter += 1
        if self._tick_counter >= self._GC_INTERVAL_TICKS:
            self._prune_dedup_cache()
            self._tick_counter = 0

        # 2) Batch-drain input queue
        MAX_BATCH = 256
        processed_this_tick = 0

        while processed_this_tick < MAX_BATCH:
            # Block only for the first event to avoid busy spin
            timeout = 0.1 if processed_this_tick == 0 else 0.0
            event = self.input.pop(timeout=timeout)

            if event is None:
                break

            processed_this_tick += 1

            # 3) Filter
            if self._should_ignore(event):
                continue

            # 4) Create Alert
            alert = self._create_alert(event)

            # 5) Push (critical path)
            if not self.output.push(alert, timeout=self._ALERT_PUSH_TIMEOUT_S):
                self.logger.error(
                    "Alert queue timeout (%.2fs). Lost alert: %s",
                    self._ALERT_PUSH_TIMEOUT_S,
                    alert.description,
                )

    def teardown(self) -> None:
        self.logger.info("Logic Core stopping...")

    def _should_ignore(self, event: CfarEvent) -> bool:
        now = time.monotonic()
        freq_key = int(float(event.center_freq_hz) / 10_000)

        # Production-correct dedup:
        # - First ever event in a bucket must pass (no fake "last_seen=0.0" baseline).
        last_seen = self._last_alert_ts.get(freq_key)
        if last_seen is not None and (now - last_seen) < self._DEDUP_WINDOW_S:
            return True

        self._last_alert_ts[freq_key] = now
        return False

    def _create_alert(self, event: CfarEvent) -> Alert:
        snr = float(event.snr_db)
        freq_hz = float(event.center_freq_hz)

        if snr > 15.0:
            severity = EventSeverity.CRITICAL
        elif snr > 8.0:
            severity = EventSeverity.WARNING
        else:
            severity = EventSeverity.INFO

        description = "DETECTED: %.3f MHz | SNR: %.1f dB" % (freq_hz / 1e6, snr)

        confidence = snr / self._CONFIDENCE_SNR_NORM_DB
        if confidence < 0.0:
            confidence = 0.0
        elif confidence > 1.0:
            confidence = 1.0

        return Alert(
            source_cfar_event_id=event.event_id,
            severity=severity,
            classification=SignalClassification.UNKNOWN,
            center_freq_hz=freq_hz,
            description=description,
            confidence_score=float(confidence),
        )

    def _prune_dedup_cache(self) -> None:
        now = time.monotonic()
        cutoff = now - (self._DEDUP_WINDOW_S * 3.0)

        before = len(self._last_alert_ts)
        self._last_alert_ts = {
            k: ts for k, ts in self._last_alert_ts.items() if ts >= cutoff
        }
        removed = before - len(self._last_alert_ts)

        if removed > 0:
            self.logger.debug("GC: Pruned %d old keys from dedup cache", removed)