# tests/test_emulation_intensity_integration.py

from __future__ import annotations

from typing import Any, cast

import pytest

from src.core.types import ProcessingConfig, SdrConfig, SimulationIntensity, TalosConfig
from src.services.sdr_monitor import SdrMonitor


class _RecordingQueue:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def push_nowait(self, item: Any) -> bool:
        self.items.append(item)
        return True


def _mk_global_cfg(intensity: SimulationIntensity) -> TalosConfig:
    sdr = SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0)
    processing = ProcessingConfig(
        fft_size=1024,
        cfar_threshold_db=15.0,
        ai_anomaly_threshold=0.85,
    )
    return TalosConfig(simulation_intensity=intensity, sdr=sdr, processing=processing)


def _run_cfar_events_for_intensity(
    monkeypatch: pytest.MonkeyPatch,
    intensity: SimulationIntensity,
    frames: int,
) -> int:
    import src.hal.sdr as sdr_mod

    def _noop_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sdr_mod, "_HAS_SOAPY", False)
    monkeypatch.setattr(sdr_mod, "SoapySDR", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_RX", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_CF32", None)
    monkeypatch.setattr(sdr_mod.time, "sleep", _noop_sleep)

    global_cfg = _mk_global_cfg(intensity)
    out_q = _RecordingQueue()

    monitor = SdrMonitor(
        sdr_config=global_cfg.sdr,
        dsp_config=global_cfg.processing,
        output_queue=cast(Any, out_q),
        global_config=global_cfg,
        waterfall_queue=None,
    )

    monitor.setup()

    for _ in range(frames):
        monitor.execute()

    return len(out_q.items)


def test_emulation_intensity_monotonic_cfar_events(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = 60

    low = _run_cfar_events_for_intensity(monkeypatch, SimulationIntensity.LOW, frames)
    medium = _run_cfar_events_for_intensity(monkeypatch, SimulationIntensity.MEDIUM, frames)
    high = _run_cfar_events_for_intensity(monkeypatch, SimulationIntensity.HIGH, frames)

    assert low <= medium <= high
    assert high > 0
