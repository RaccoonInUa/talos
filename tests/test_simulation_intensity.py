# tests/test_simulation_intensity.py

from __future__ import annotations

from typing import Any

import numpy as np

from src.core.types import ProcessingConfig, SdrConfig, SimulationIntensity, TalosConfig
from src.hal.sdr import SdrDriver, VirtualSDRDevice
from src.sim.rf_environment import RFEnvironmentSimulator


def _mk_global_cfg(*, intensity: SimulationIntensity) -> TalosConfig:
    return TalosConfig(
        simulation_intensity=intensity,
        sdr=SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0),
        processing=ProcessingConfig(
            fft_size=1024,
            cfar_threshold_db=15.0,
            ai_anomaly_threshold=0.85,
        ),
    )


def test_rf_environment_intensity_profiles_change_params() -> None:
    base = RFEnvironmentSimulator(intensity=None)
    low = RFEnvironmentSimulator(intensity="low")
    high = RFEnvironmentSimulator(intensity="high")

    assert low.config.noise.std < base.config.noise.std < high.config.noise.std
    assert low.config.telemetry.amplitude < base.config.telemetry.amplitude < high.config.telemetry.amplitude

    # Lower intensity should hop less often (larger interval), higher intensity more often.
    assert low.config.hopper.hop_interval_s > base.config.hopper.hop_interval_s > high.config.hopper.hop_interval_s

    # Interference should be rarer at low intensity and more frequent at high intensity.
    assert (
        low.config.interference.probability_per_frame
        < base.config.interference.probability_per_frame
        < high.config.interference.probability_per_frame
    )


def test_virtual_sdr_device_passes_intensity_to_simulator(monkeypatch) -> None:
    import src.hal.sdr as sdr_mod

    class _FakeSim:
        last_intensity: Any = None

        def __init__(self, *args: Any, intensity: Any = None, **kwargs: Any) -> None:
            _FakeSim.last_intensity = intensity

    monkeypatch.setattr(sdr_mod, "RFEnvironmentSimulator", _FakeSim)

    _ = VirtualSDRDevice(intensity=SimulationIntensity.HIGH)

    assert _FakeSim.last_intensity == SimulationIntensity.HIGH


def test_sdrdriver_emulator_passes_global_simulation_intensity(monkeypatch) -> None:
    import src.hal.sdr as sdr_mod

    class _FakeSim:
        last_intensity: Any = None

        def __init__(self, *args: Any, intensity: Any = None, **kwargs: Any) -> None:
            _FakeSim.last_intensity = intensity

        def next_iq_frame(
            self,
            num_samples: int,
            sample_rate_hz: float,
            center_freq_hz: float,
        ) -> np.ndarray:
            return np.zeros(num_samples, dtype=np.complex64)

    monkeypatch.setattr(sdr_mod, "RFEnvironmentSimulator", _FakeSim)

    cfg = SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0)
    global_cfg = _mk_global_cfg(intensity=SimulationIntensity.LOW)
    d = SdrDriver(cfg, global_config=global_cfg)

    d._setup_emulator("test")

    assert _FakeSim.last_intensity == SimulationIntensity.LOW


def test_sdrdriver_generate_fake_samples_uses_global_intensity(monkeypatch) -> None:
    import src.hal.sdr as sdr_mod

    class _FakeSim:
        last_intensity: Any = None

        def __init__(self, *args: Any, intensity: Any = None, **kwargs: Any) -> None:
            _FakeSim.last_intensity = intensity

        def next_iq_frame(
            self,
            num_samples: int,
            sample_rate_hz: float,
            center_freq_hz: float,
        ) -> np.ndarray:
            return np.zeros(num_samples, dtype=np.complex64)

    monkeypatch.setattr(sdr_mod, "RFEnvironmentSimulator", _FakeSim)
    monkeypatch.setattr(sdr_mod.time, "sleep", lambda *_args, **_kwargs: None)

    cfg = SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0)
    global_cfg = _mk_global_cfg(intensity=SimulationIntensity.HIGH)
    d = SdrDriver(cfg, global_config=global_cfg)

    out = d._generate_fake_samples(16)

    assert out.shape == (16,)
    assert out.dtype == np.complex64
    assert _FakeSim.last_intensity == SimulationIntensity.HIGH
