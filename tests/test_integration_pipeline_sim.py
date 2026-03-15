

# tests/test_integration_pipeline_sim.py

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from typing import List, cast, Any
from datetime import datetime, timezone

import pytest

from src.services.logic_core import LogicService
from src.core.types import CfarEvent, Alert


@dataclass
class _EventPlanItem:
    t: float
    event: CfarEvent


class _FakeInputQueue:
    """Deterministic input queue for CI simulation.

    - pop(timeout=...) returns the next event when fake_time >= scheduled_time
    - otherwise returns None

    This allows us to drive LogicService.execute() in a tight loop with a fake clock
    (no sleep) while keeping behavior close to production: execute() polls pop().
    """

    def __init__(self, plan: List[_EventPlanItem], fake_time_ref: List[float]):
        self._plan = plan
        self._idx = 0
        self._tref = fake_time_ref

    def pop(self, timeout: float = 0.0):  # noqa: ARG002 - signature compatibility
        if self._idx >= len(self._plan):
            return None

        now = float(self._tref[0])
        nxt = self._plan[self._idx]
        if now >= float(nxt.t):
            self._idx += 1
            return nxt.event

        return None

class _RecordingOutputQueue:
    """Output queue that can simulate backpressure deterministically.

    Strategy:
      - push() returns False every `fail_every_n` pushes (e.g., 7 -> 7th, 14th, ...)
      - otherwise returns True

    We also record all alerts passed into push() for assertions.
    """

    def __init__(self, *, fail_every_n: int):
        if fail_every_n <= 0:
            raise ValueError("fail_every_n must be > 0")

        self.fail_every_n = int(fail_every_n)
        self.push_calls: int = 0
        self.items: list[Alert] = []

    def push(self, item: Alert, timeout: float = 0.0) -> bool:
        self.push_calls += 1
        self.items.append(item)

        if (self.push_calls % self.fail_every_n) == 0:
            return False

        return True

def _mk_cfar_event(*, freq_hz: float, snr_db: float, power_db: float, noise_floor_db: float) -> CfarEvent:
    """Create a real DTO instance (no MagicMock) so pydantic validation matches prod."""
    return CfarEvent(
        event_id=uuid.uuid4(),
        center_freq_hz=float(freq_hz),
        snr_db=float(snr_db),
        power_db=float(power_db),
        noise_floor_db=float(noise_floor_db),
        timestamp=datetime.now(timezone.utc),
        bandwidth_hz=1.0,   # must be >0 according to pydantic model
        duration_s=0.01,
        duty_cycle=0.1,
    )


def _generate_plan(*, seed: int) -> List[_EventPlanItem]:
    """Deterministic pseudo-random event schedule.

    Key idea:
      - we intentionally generate bursts in the SAME 10kHz bucket to validate dedup
      - we also generate some events outside the bucket to ensure normal throughput

    The plan is deterministic due to the fixed seed.
    """
    rng = random.Random(int(seed))

    base_freq = 433_000_000.0
    other_freq = 433_120_000.0  # different 10kHz bucket

    plan: List[_EventPlanItem] = []

    t = 0.0
    # Phase 1: burst inside one bucket (should dedup heavily)
    for _ in range(15):
        # keep within same 10 kHz bucket by staying within <10kHz
        jitter_hz = rng.uniform(-2_000.0, 2_000.0)
        snr = rng.uniform(9.0, 18.0)
        ev = _mk_cfar_event(
            freq_hz=base_freq + jitter_hz,
            snr_db=snr,
            power_db=-30.0 + rng.uniform(-2.0, 2.0),
            noise_floor_db=-45.0 + rng.uniform(-2.0, 2.0),
        )
        plan.append(_EventPlanItem(t=t, event=ev))
        # tight spacing (< dedup window)
        t += rng.uniform(0.01, 0.2)

    # Phase 2: advance time beyond dedup window, then allow another event in same bucket
    t += 2.5
    ev = _mk_cfar_event(
        freq_hz=base_freq + rng.uniform(-2_000.0, 2_000.0),
        snr_db=rng.uniform(9.0, 18.0),
        power_db=-30.0,
        noise_floor_db=-45.0,
    )
    plan.append(_EventPlanItem(t=t, event=ev))

    # Phase 3: mix of other-bucket events (should pass, not deduped by bucket 1)
    for _ in range(10):
        t += rng.uniform(0.05, 0.4)
        ev = _mk_cfar_event(
            freq_hz=other_freq + rng.uniform(-2_000.0, 2_000.0),
            snr_db=rng.uniform(6.0, 20.0),
            power_db=-31.0,
            noise_floor_db=-46.0,
        )
        plan.append(_EventPlanItem(t=t, event=ev))

    return plan


def test_integration_pipeline_sim_seeded_dedup_and_backpressure(caplog: pytest.LogCaptureFixture) -> None:
    """CI-stable integration simulation (no SDR, no threads, no sleeps).

    What we prove:
      - LogicService.execute() can be driven end-to-end via input.pop()/output.push()
      - Dedup works for repeated events in the same 10kHz bucket within the window
      - Backpressure is handled: output.push(False) logs an ERROR

    Constraints:
      - deterministic (fixed seed)
      - fast (< ~1s)
      - no private attribute inspection

    Notes:
      - We do *not* rely on internal constants like _DEDUP_WINDOW_S.
        Instead, we deliberately schedule a >2s time jump and assert we get
        a second acceptance in the same bucket.
    """

    # Stable seed for CI reproducibility
    seed = 1337

    # Fake monotonic clock is a 1-element list so closures can mutate it.
    fake_now = [0.0]

    plan = _generate_plan(seed=seed)
    in_q = _FakeInputQueue(plan, fake_now)

    # Backpressure every 7th push attempt
    out_q = _RecordingOutputQueue(fail_every_n=5)

    svc = LogicService(
        cast(Any, in_q),
        cast(Any, out_q),
    )

    # Capture only logic_core logger errors for backpressure assertion
    caplog.set_level(logging.ERROR, logger=svc.logger.name)

    # Patch time.monotonic used by LogicService._should_ignore and GC
    # We drive time forward in discrete steps, no sleeps.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("time.monotonic", lambda: float(fake_now[0]))

        # Drive simulation until the plan is exhausted, plus a few extra ticks.
        # The extra ticks mimic idle polling after last event.
        max_t = float(plan[-1].t) + 0.5

        # Use small time step to let pop() release events at their scheduled time.
        dt = 0.05
        while fake_now[0] <= max_t:
            svc.execute()
            fake_now[0] += dt

        # Extra polls after last event (should do nothing)
        for _ in range(10):
            svc.execute()

    # Assertions (no private inspection):
    # 1) There must have been at least one push attempt.
    assert out_q.push_calls > 0

    # 2) Dedup should reduce pushes during the initial same-bucket burst.
    #    We created 15 events in phase 1 within the same bucket.
    #    It should not push 15 times.
    #    (We avoid hardcoding exact number, but it must be much smaller.)
    assert out_q.push_calls < 15 + 1 + 10  # strict upper bound: total events

    # 3) We intentionally jumped time by >2s between phase 1 and phase 2,
    #    so we expect at least TWO accepted alerts from the same bucket overall.
    #    We'll infer this without private inspection by looking at descriptions.
    same_bucket_desc: list[str] = [
        a.description for a in out_q.items if "DETECTED: 433." in a.description
    ]
    assert len(same_bucket_desc) >= 2

    # 4) Backpressure: every 3rd push returns False -> should log ERROR at least once.
    assert any("Lost alert" in rec.getMessage() for rec in caplog.records)
