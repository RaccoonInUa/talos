# src/sim/rf_environment.py
#
# Virtual RF environment simulator for TALOS.
#
# Goals:
# - Generate realistic-ish IQ frames for the existing SDR -> DSP -> CFAR pipeline
# - Deterministic enough for testing, rich enough for demos/manual runs
# - Model:
#     * thermal/noise floor
#     * telemetry bursts
#     * frequency hopping source
#     * random interference
#
# Notes:
# - This is a signal-environment generator, not a full RF physics simulator.
# - It intentionally favors operational usefulness over RF-purity.
# - All frequencies here are expressed as OFFSETS from center_freq_hz in Hz.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from src.core.types import SimulationIntensity
from enum import Enum

# Simulation intensity enum
class SimulationIntensity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

import numpy as np
from numpy.typing import NDArray


@dataclass(slots=True)
class TelemetryBurstConfig:
    """Short duty-cycled burst source near a fixed carrier offset."""
    enabled: bool = True
    carrier_offset_hz: float = 220_000.0
    amplitude: float = 0.035
    burst_on_s: float = 0.18
    burst_off_s: float = 0.65
    snr_wobble_db: float = 3.0
    drift_hz: float = 1_500.0
    burst_rise_fall_fraction: float = 0.08


@dataclass(slots=True)
class HopperConfig:
    """Frequency-hopping narrowband source."""
    enabled: bool = True
    offsets_hz: tuple[float, ...] = (
        -380_000.0,
        -260_000.0,
        -120_000.0,
        80_000.0,
        240_000.0,
        360_000.0,
    )
    hop_interval_s: float = 0.075
    amplitude: float = 0.028
    dwell_jitter_s: float = 0.01
    per_hop_amplitude_jitter_db: float = 2.5
    fine_offset_jitter_hz: float = 8_000.0


@dataclass(slots=True)
class InterferenceConfig:
    """Random short-lived interferers."""
    enabled: bool = True
    probability_per_frame: float = 0.035
    min_duration_s: float = 0.03
    max_duration_s: float = 0.25
    min_amplitude: float = 0.015
    max_amplitude: float = 0.09
    max_abs_offset_hz: float = 900_000.0
    wideband_probability: float = 0.20
    max_concurrent: int = 3
    chirp_probability: float = 0.18


@dataclass(slots=True)
class NoiseConfig:
    """Approximate noise floor."""
    std: float = 0.0012


@dataclass(slots=True)
class RFEnvironmentConfig:
    noise: NoiseConfig = field(default_factory=NoiseConfig)
    telemetry: TelemetryBurstConfig = field(default_factory=TelemetryBurstConfig)
    hopper: HopperConfig = field(default_factory=HopperConfig)
    interference: InterferenceConfig = field(default_factory=InterferenceConfig)
    seed: Optional[int] = 1337


@dataclass(slots=True)
class _ActiveInterferer:
    start_time_s: float
    end_time_s: float
    amplitude: float
    offset_hz: float
    is_wideband: bool
    is_chirp: bool
    chirp_rate_hz_per_s: float


class RFEnvironmentSimulator:
    """
    Generates complex64 IQ frames for a virtual SDR.

    Public contract:
      next_iq_frame(num_samples, sample_rate_hz, center_freq_hz) -> complex64[N]

    Internal time:
      - advances by frame duration per call
      - independent from wall-clock time
      - deterministic given the same seed/config/call pattern
    """

    def __init__(
        self,
        config: RFEnvironmentConfig | None = None,
        *,
        intensity: SimulationIntensity | str | None = None,
    ):
        self.config = config or RFEnvironmentConfig()
        self._apply_intensity_profile(intensity)
        self._rng = np.random.default_rng(self.config.seed)

        self._sim_time_s: float = 0.0
        self._hopper_next_hop_s: float = 0.0
        self._hopper_index: int = 0
        self._active_interferers: list[_ActiveInterferer] = []

    def _apply_intensity_profile(self, intensity: str) -> None:
        """
        Adjust environment parameters based on desired simulation intensity.
        """

        if intensity == "low":
            self.config.noise.std = 0.0008

            self.config.telemetry.amplitude = 0.015

            self.config.hopper.amplitude = 0.012
            self.config.hopper.hop_interval_s = 0.12

            self.config.interference.probability_per_frame = 0.01
            self.config.interference.max_concurrent = 1
            self.config.interference.max_amplitude = 0.03
            self.config.interference.wideband_probability = 0.05

        elif intensity == "high":
            self.config.noise.std = 0.002

            self.config.telemetry.amplitude = 0.05

            self.config.hopper.amplitude = 0.04
            self.config.hopper.hop_interval_s = 0.05

            self.config.interference.probability_per_frame = 0.08
            self.config.interference.max_concurrent = 5
            self.config.interference.max_amplitude = 0.12
            self.config.interference.wideband_probability = 0.35

        else:
            # medium (default)
            pass

    def next_iq_frame(
        self,
        num_samples: int,
        sample_rate_hz: float,
        center_freq_hz: float,  # kept for future scenario-awareness
    ) -> NDArray[np.complex64]:
        if num_samples <= 0:
            return np.array([], dtype=np.complex64)

        frame_duration_s = num_samples / float(sample_rate_hz)
        t0 = self._sim_time_s
        t1 = t0 + frame_duration_s

        n = num_samples
        t = np.arange(n, dtype=np.float64)
        frame_t_s = t / float(sample_rate_hz)

        # Base thermal-ish noise
        iq = self._noise(n)

        # Deterministic telemetry bursts
        if self.config.telemetry.enabled:
            iq += self._telemetry_component(
                t=t,
                frame_t_s=frame_t_s,
                t0=t0,
                sample_rate_hz=sample_rate_hz,
            )

        # Frequency-hopping source
        if self.config.hopper.enabled:
            iq += self._hopper_component(
                t=t,
                t0=t0,
                t1=t1,
                sample_rate_hz=sample_rate_hz,
            )

        # Random interference
        if self.config.interference.enabled:
            iq += self._interference_component(
                t=t,
                frame_t_s=frame_t_s,
                t0=t0,
                t1=t1,
                sample_rate_hz=sample_rate_hz,
            )

        self._sim_time_s = t1
        return iq.astype(np.complex64, copy=False)

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------

    def _noise(self, n: int) -> NDArray[np.complex128]:
        std = float(self.config.noise.std)
        return (
            self._rng.normal(0.0, std, n) +
            1j * self._rng.normal(0.0, std, n)
        ).astype(np.complex128)

    def _telemetry_component(
        self,
        *,
        t: NDArray[np.float64],
        frame_t_s: NDArray[np.float64],
        t0: float,
        sample_rate_hz: float,
    ) -> NDArray[np.complex128]:
        cfg = self.config.telemetry

        cycle_s = cfg.burst_on_s + cfg.burst_off_s
        phase_in_cycle = t0 % cycle_s
        if phase_in_cycle >= cfg.burst_on_s:
            return np.zeros_like(t, dtype=np.complex128)

        # Slight amplitude wobble makes it less toy-like
        wobble_db = self._rng.uniform(-cfg.snr_wobble_db, cfg.snr_wobble_db)
        amp = cfg.amplitude * (10.0 ** (wobble_db / 20.0))

        # Mild carrier drift inside the burst makes the spectrum less perfectly stationary.
        drift_phase = 2.0 * np.pi * float(cfg.drift_hz) * frame_t_s
        offset_inst_hz = cfg.carrier_offset_hz + (cfg.drift_hz * np.sin(drift_phase))

        # Apply a short rise/fall envelope so bursts do not look like perfectly hard on/off gates.
        envelope = np.ones_like(frame_t_s, dtype=np.float64)
        ramp_s = max(0.0, float(cfg.burst_on_s) * float(cfg.burst_rise_fall_fraction))
        if ramp_s > 0.0:
            remaining_on_s = max(0.0, cfg.burst_on_s - phase_in_cycle)
            burst_time_s = np.minimum(frame_t_s, remaining_on_s)

            rise_mask = burst_time_s < ramp_s
            if np.any(rise_mask):
                envelope[rise_mask] = 0.5 - 0.5 * np.cos(np.pi * burst_time_s[rise_mask] / ramp_s)

            fall_mask = burst_time_s > max(0.0, remaining_on_s - ramp_s)
            if np.any(fall_mask) and remaining_on_s > 0.0:
                tail_s = np.maximum(0.0, remaining_on_s - burst_time_s[fall_mask])
                envelope[fall_mask] = np.minimum(
                    envelope[fall_mask],
                    0.5 - 0.5 * np.cos(np.pi * tail_s / ramp_s),
                )

        return amp * envelope * self._tone_sweep(
            t=t,
            offset_hz=offset_inst_hz,
            sample_rate_hz=sample_rate_hz,
        )

    def _hopper_component(
        self,
        *,
        t: NDArray[np.float64],
        t0: float,
        t1: float,
        sample_rate_hz: float,
    ) -> NDArray[np.complex128]:
        cfg = self.config.hopper
        if not cfg.offsets_hz:
            return np.zeros_like(t, dtype=np.complex128)

        if t0 >= self._hopper_next_hop_s:
            self._hopper_index = (self._hopper_index + 1) % len(cfg.offsets_hz)
            jitter = self._rng.uniform(-cfg.dwell_jitter_s, cfg.dwell_jitter_s)
            self._hopper_next_hop_s = t0 + max(0.01, cfg.hop_interval_s + jitter)

        offset = float(cfg.offsets_hz[self._hopper_index])
        offset += float(self._rng.uniform(-cfg.fine_offset_jitter_hz, cfg.fine_offset_jitter_hz))

        amp_jitter_db = self._rng.uniform(-cfg.per_hop_amplitude_jitter_db, cfg.per_hop_amplitude_jitter_db)
        amplitude = cfg.amplitude * (10.0 ** (amp_jitter_db / 20.0))

        return self._tone(
            t=t,
            offset_hz=offset,
            amplitude=amplitude,
            sample_rate_hz=sample_rate_hz,
        )

    def _interference_component(
        self,
        *,
        t: NDArray[np.float64],
        frame_t_s: NDArray[np.float64],
        t0: float,
        t1: float,
        sample_rate_hz: float,
    ) -> NDArray[np.complex128]:
        cfg = self.config.interference

        # Spawn new interferers while there is capacity.
        while (
            len(self._active_interferers) < cfg.max_concurrent
            and self._rng.random() < cfg.probability_per_frame
        ):
            duration_s = self._rng.uniform(cfg.min_duration_s, cfg.max_duration_s)
            amplitude = self._rng.uniform(cfg.min_amplitude, cfg.max_amplitude)
            offset_hz = self._rng.uniform(-cfg.max_abs_offset_hz, cfg.max_abs_offset_hz)
            is_wideband = bool(self._rng.random() < cfg.wideband_probability)
            is_chirp = bool((not is_wideband) and (self._rng.random() < cfg.chirp_probability))
            chirp_rate_hz_per_s = 0.0
            if is_chirp:
                chirp_rate_hz_per_s = float(self._rng.uniform(-1_500_000.0, 1_500_000.0))

            self._active_interferers.append(
                _ActiveInterferer(
                    start_time_s=t0,
                    end_time_s=t0 + duration_s,
                    amplitude=amplitude,
                    offset_hz=offset_hz,
                    is_wideband=is_wideband,
                    is_chirp=is_chirp,
                    chirp_rate_hz_per_s=chirp_rate_hz_per_s,
                )
            )

        # Remove expired interferers.
        self._active_interferers = [
            active for active in self._active_interferers if active.end_time_s > t0
        ]

        if not self._active_interferers:
            return np.zeros_like(t, dtype=np.complex128)

        out = np.zeros_like(t, dtype=np.complex128)

        for active in self._active_interferers:
            if active.is_wideband:
                # Mildly colored wideband energy: wideband noise plus a weak low-rate amplitude flutter.
                flutter = 1.0 + 0.18 * np.sin(2.0 * np.pi * 7.0 * frame_t_s)
                real = self._rng.normal(0.0, active.amplitude, t.size)
                imag = self._rng.normal(0.0, active.amplitude, t.size)
                wb = np.asarray(real + 1j * imag, dtype=np.complex128)
                out += flutter * wb
                continue

            if active.is_chirp:
                local_t_s = frame_t_s + max(0.0, t0 - active.start_time_s)
                inst_offset_hz = active.offset_hz + (active.chirp_rate_hz_per_s * local_t_s)
                out += active.amplitude * self._tone_sweep(
                    t=t,
                    offset_hz=inst_offset_hz,
                    sample_rate_hz=sample_rate_hz,
                )
                continue

            out += self._tone(
                t=t,
                offset_hz=active.offset_hz,
                amplitude=active.amplitude,
                sample_rate_hz=sample_rate_hz,
            )

        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tone_sweep(
        *,
        t: NDArray[np.float64],
        offset_hz: NDArray[np.float64] | float,
        sample_rate_hz: float,
    ) -> NDArray[np.complex128]:
        offset_arr = np.asarray(offset_hz, dtype=np.float64)
        phase_step = (2.0 * np.pi * offset_arr) / float(sample_rate_hz)
        phase = np.cumsum(phase_step, dtype=np.float64)
        tone = np.exp(1j * phase).astype(np.complex128, copy=False)
        return tone

    @staticmethod
    def _tone(
        *,
        t: NDArray[np.float64],
        offset_hz: float,
        amplitude: float,
        sample_rate_hz: float,
    ) -> NDArray[np.complex128]:
        phase = 2.0 * np.pi * float(offset_hz) * t / float(sample_rate_hz)
        tone = np.exp(1j * phase).astype(np.complex128, copy=False)
        return float(amplitude) * tone