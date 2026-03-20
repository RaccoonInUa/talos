# tests/test_emulator_smoke_pipeline.py

from __future__ import annotations

from typing import Any, cast

from src.core.types import ProcessingConfig, SdrConfig, SimulationIntensity, TalosConfig
from src.services.logic_core import LogicService
from src.services.sdr_monitor import SdrMonitor


class _InMemoryQueue:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def push_nowait(self, item: Any) -> bool:
        self.items.append(item)
        return True

    def push(self, item: Any, timeout: float = 0.0) -> bool:
        self.items.append(item)
        return True

    def pop(self, timeout: float = 0.0):
        if not self.items:
            return None
        return self.items.pop(0)


def test_emulator_smoke_pipeline_min_frames(monkeypatch) -> None:
    import src.hal.sdr as sdr_mod

    monkeypatch.setattr(sdr_mod, "_HAS_SOAPY", False)
    monkeypatch.setattr(sdr_mod, "SoapySDR", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_RX", None)
    monkeypatch.setattr(sdr_mod, "SOAPY_SDR_CF32", None)
    monkeypatch.setattr(sdr_mod.time, "sleep", lambda *_args, **_kwargs: None)

    cfg = TalosConfig(
        simulation_intensity=SimulationIntensity.HIGH,
        sdr=SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0),
        processing=ProcessingConfig(
            fft_size=1024,
            cfar_threshold_db=15.0,
            ai_anomaly_threshold=0.85,
        ),
    )

    cfar_q = _InMemoryQueue()
    alert_q = _InMemoryQueue()

    monitor = SdrMonitor(
        sdr_config=cfg.sdr,
        dsp_config=cfg.processing,
        output_queue=cast(Any, cfar_q),
        global_config=cfg,
        waterfall_queue=None,
    )
    logic = LogicService(input_queue=cast(Any, cfar_q), output_queue=cast(Any, alert_q))

    monitor.setup()

    for _ in range(20):
        monitor.execute()
        logic.execute()

    assert len(alert_q.items) >= 1
