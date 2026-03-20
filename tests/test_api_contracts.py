# tests/test_api_contracts.py

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.core.types import Alert, EventSeverity, SignalClassification, TalosConfig, SdrConfig, ProcessingConfig, WaterfallFrame


class _DummyStatic:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def __call__(self, scope, receive, send) -> None:  # pragma: no cover - ASGI stub
        return None


class _FakeOrchestrator:
    def __init__(self, *, alerts: list[Alert], frame: WaterfallFrame | None) -> None:
        self._alerts = alerts
        self._frame = frame
        self.metrics = {
            "frames_ok": 1,
            "frames_skipped": 2,
            "cfar_detections": 3,
            "clusters_emitted": 4,
            "events_emitted": 5,
            "waterfall_frames_emitted": 6,
            "waterfall_frames_dropped": 7,
            "queue_cfar": [1, 10],
            "queue_alert": [2, 50],
            "queue_waterfall": [0, 5],
        }
        self.hal_state = "SCANNING"
        self.config = TalosConfig(
            sdr=SdrConfig(center_freq_hz=433e6, sample_rate_hz=2_000_000, gain_db=10.0),
            processing=ProcessingConfig(
                fft_size=1024,
                cfar_threshold_db=15.0,
                ai_anomaly_threshold=0.85,
            ),
        )

    def get_recent_alerts(self) -> list[Alert]:
        return list(self._alerts)

    def get_latest_waterfall_frame(self):
        if self._frame is None:
            return None
        frame = self._frame
        self._frame = None
        return frame


def _mk_app(monkeypatch, orchestrator: _FakeOrchestrator):
    import src.api.server as server_mod

    monkeypatch.setattr(server_mod, "StaticFiles", _DummyStatic)
    return server_mod.create_app(orchestrator)


def test_status_and_alerts_contract(monkeypatch) -> None:
    alerts = [
        Alert(
            severity=EventSeverity.CRITICAL,
            classification=SignalClassification.UNKNOWN,
            center_freq_hz=433e6,
            description="A1",
            confidence_score=0.9,
        )
    ]
    orch = _FakeOrchestrator(alerts=alerts, frame=None)
    app = _mk_app(monkeypatch, orch)

    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()

    expected_keys = {
        "frames_ok",
        "frames_skipped",
        "cfar_detections",
        "clusters_emitted",
        "events_emitted",
        "waterfall_frames_emitted",
        "waterfall_frames_dropped",
        "queue_sizes",
        "hal_state",
    }
    assert expected_keys.issubset(payload.keys())
    assert set(payload["queue_sizes"].keys()) == {"cfar", "alert", "waterfall"}

    alerts_resp = client.get("/alerts")
    assert alerts_resp.status_code == 200
    alerts_payload = alerts_resp.json()
    assert isinstance(alerts_payload, list)
    assert len(alerts_payload) == 1
    assert alerts_payload[0]["severity"] == "critical"
    assert isinstance(alerts_payload[0]["timestamp"], str)


def test_websocket_waterfall_packet_size(monkeypatch) -> None:
    frame = WaterfallFrame(
        frame_seq=1,
        timestamp=datetime.now(timezone.utc),
        center_freq_hz=433e6,
        bin_hz=1_000.0,
        line_uint8=b"\x01\x02\x03",
    )
    orch = _FakeOrchestrator(alerts=[], frame=frame)
    app = _mk_app(monkeypatch, orch)

    client = TestClient(app)

    with client.websocket_connect("/ws/waterfall") as ws:
        data = ws.receive_bytes()
        assert len(data) == 288
        assert len(data[32:]) == 256
        ws.close()
