# src/core/config.py

from __future__ import annotations
from src.core.types import TalosConfig, SdrConfig, ProcessingConfig


def load_config() -> TalosConfig:
    """
    Load system configuration.

    MVP:
      - hardcoded defaults
    Future:
      - env vars
      - YAML / JSON
      - CLI overrides
    """
    return TalosConfig(
        sdr=SdrConfig(
            center_freq_hz=433e6,
            sample_rate_hz=2_000_000,
            gain_db=10.0,
        ),
        processing=ProcessingConfig(
            fft_size=1024,
            cfar_threshold_db=15.0,
            ai_anomaly_threshold=0.85,
        ),
    )