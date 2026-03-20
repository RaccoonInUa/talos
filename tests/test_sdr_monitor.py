# tests/test_sdr_monitor.py

from __future__ import annotations

from typing import Any, cast

import numpy as np

from src.core.bus import TalosQueue
from src.core.types import CfarEvent, ProcessingConfig, SdrConfig, SimulationIntensity, TalosConfig
from src.services.sdr_monitor import SdrMonitor


def _tone_samples(*, num_samples: int, sample_rate_hz: float, offset_hz: float, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(num_samples, dtype=np.float64) / float(sample_rate_hz)
    samples = amplitude * np.exp(1j * 2.0 * np.pi * float(offset_hz) * t)
    return samples.astype(np.complex64, copy=False)


class _RecordingQueue:
    def __init__(self) -> None:
        self.items: list[CfarEvent] = []

    def push_nowait(self, item: CfarEvent) -> bool:
        self.items.append(item)
        return True


class _StubDriver:
    def __init__(self, samples: np.ndarray) -> None:
        self._samples = samples

    def read_samples(self, num_samples: int) -> np.ndarray:
        if self._samples.size != num_samples:
            return np.empty(0, dtype=np.complex64)
        return self._samples

    def close(self) -> None:
        return None


def _mk_configs(*, cfar_threshold_db: float = 8.0) -> tuple[TalosConfig, SdrConfig, ProcessingConfig]:
    sdr = SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0)
    processing = ProcessingConfig(
        fft_size=1024,
        cfar_threshold_db=cfar_threshold_db,
        ai_anomaly_threshold=0.85,
    )
    global_cfg = TalosConfig(
        simulation_intensity=SimulationIntensity.MEDIUM,
        sdr=sdr,
        processing=processing,
    )
    return global_cfg, sdr, processing


def test_sdr_monitor_happy_path_emits_cfar_event(monkeypatch) -> None:
    import src.hal.sdr as sdr_mod

    monkeypatch.setattr(sdr_mod, "_HAS_SOAPY", False)
    monkeypatch.setattr(sdr_mod, "SoapySDR", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_RX", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_CF32", None)

    global_cfg, sdr_cfg, dsp_cfg = _mk_configs(cfar_threshold_db=6.0)

    out_q = _RecordingQueue()
    monitor = SdrMonitor(
        sdr_config=sdr_cfg,
        dsp_config=dsp_cfg,
        output_queue=cast(Any, out_q),
        global_config=global_cfg,
        waterfall_queue=None,
    )

    monitor.setup()

    samples = _tone_samples(
        num_samples=dsp_cfg.fft_size,
        sample_rate_hz=sdr_cfg.sample_rate_hz,
        offset_hz=200_000.0,
        amplitude=1.0,
    )

    monitor.driver = _StubDriver(samples)
    monitor.execute()

    assert len(out_q.items) >= 1


def test_sdr_monitor_backpressure_drops_do_not_crash(monkeypatch) -> None:
    import src.hal.sdr as sdr_mod

    monkeypatch.setattr(sdr_mod, "_HAS_SOAPY", False)
    monkeypatch.setattr(sdr_mod, "SoapySDR", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_RX", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_CF32", None)

    global_cfg, sdr_cfg, dsp_cfg = _mk_configs(cfar_threshold_db=6.0)

    out_q = TalosQueue(CfarEvent, maxsize=1, name="cfar_backpressure_test")
    monitor = SdrMonitor(
        sdr_config=sdr_cfg,
        dsp_config=dsp_cfg,
        output_queue=out_q,
        global_config=global_cfg,
        waterfall_queue=None,
    )

    monitor.setup()

    samples = _tone_samples(
        num_samples=dsp_cfg.fft_size,
        sample_rate_hz=sdr_cfg.sample_rate_hz,
        offset_hz=200_000.0,
        amplitude=1.0,
    )

    monitor.driver = _StubDriver(samples)

    monitor.execute()
    monitor.execute()

    assert out_q.dropped_count >= 1
