# tests/test_logic_fuzz.py
# Property-based fuzz test for LogicService:
# - No SDR, no threads, no sleeps.
# - Generates many RF-like events and checks core invariants stay true.
# - Goal: catch edge-cases early (weird SNR ranges, frequency extremes, long sequences).

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from hypothesis import given, settings, strategies as st

from src.services.logic_core import LogicService
from src.core.types import Alert, CfarEvent


# -----------------------------
# Minimal test doubles for queues
# -----------------------------
# We intentionally keep them tiny and deterministic:
# - pop() returns events sequentially, then None
# - push() always accepts and records produced alerts


class _FakeInputQueue:
    """A minimal queue-like input for LogicService: sequential pop()."""

    def __init__(self, events: List[CfarEvent]):
        self.events = events
        self.idx = 0

    def pop(self, timeout: float = 0.0) -> Optional[CfarEvent]:
        # timeout is accepted for signature compatibility with real queue interfaces
        if self.idx >= len(self.events):
            return None
        ev = self.events[self.idx]
        self.idx += 1
        return ev


class _RecordingOutputQueue:
    """Records all alerts that LogicService pushes."""

    def __init__(self):
        self.items: list[Alert] = []

    def push(self, item: Alert, timeout: float = 0.0) -> bool:
        # Always accept (no backpressure in this fuzz test).
        # Backpressure behavior is tested separately in the integration smoke/sim tests.
        self.items.append(item)
        return True


# -----------------------------
# Event factory (real DTO, not MagicMock)
# -----------------------------
# Using real CfarEvent ensures pydantic validation matches production expectations.


def _mk_event(freq: float, snr: float) -> CfarEvent:
    return CfarEvent(
        event_id=uuid.uuid4(),
        center_freq_hz=float(freq),
        snr_db=float(snr),
        power_db=-30.0,
        noise_floor_db=-45.0,
        timestamp=datetime.now(timezone.utc),
        # These must satisfy pydantic constraints (e.g., > 0 where required).
        bandwidth_hz=1.0,
        duration_s=0.01,
        duty_cycle=0.1,
    )


# -----------------------------
# Property-based test
# -----------------------------
# Notes:
# - Keep CI fast and stable: limit examples and list sizes.
# - Fuzzing for “thousands per second” belongs to a separate soak script,
#   or a dedicated Hypothesis profile invoked manually.


@settings(max_examples=10000)
@given(
    # RF frequencies: from 1 kHz to 10 GHz (broad enough to hit scaling issues)
    freqs=st.lists(
        st.floats(
            min_value=1e3,
            max_value=10e9,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=5000,
    ),
    # SNR range: allow negative SNR too (real-world can be below noise floor)
    snrs=st.lists(
        st.floats(
            min_value=-20,
            max_value=100,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=500,
    ),
)
def test_logic_fuzz_basic_invariants(freqs: List[float], snrs: List[float]) -> None:
    # zip() automatically truncates to min(len(freqs), len(snrs)),
    # so we don't need a separate "size" variable.
    events: List[CfarEvent] = [_mk_event(f, s) for f, s in zip(freqs, snrs)]

    # Drive LogicService end-to-end via the public queue interface.
    in_q = _FakeInputQueue(events)
    out_q = _RecordingOutputQueue()

    svc = LogicService(in_q, out_q)  # type: ignore[arg-type]

    # Run one execute() per input event.
    for _ in events:
        svc.execute()
    
    #print(len(events), "events ->", len(out_q.items), "alerts")
    #print("alerts ratio:", len(out_q.items) / len(events))

    alerts_total = len(out_q.items)
    events_total = len(events)

    print(
        f"{events_total} events -> {alerts_total} alerts "
        f"(ratio={alerts_total/events_total:.4f})"
    )

    # -----------------------------
    # Invariants we expect to always hold
    # -----------------------------

    # The service must never produce more alerts than it consumed events.
    assert len(out_q.items) <= len(events)

    for alert in out_q.items:
        # numeric sanity checks
        assert math.isfinite(alert.center_freq_hz)
        assert math.isfinite(alert.confidence_score)

        # confidence_score must always be a finite probability-like value.
        assert 0.0 <= alert.confidence_score <= 1.0
        assert not math.isnan(alert.confidence_score)

        # Center frequency must stay within expected range.
        assert alert.center_freq_hz > 0
        assert alert.center_freq_hz <= 10e9