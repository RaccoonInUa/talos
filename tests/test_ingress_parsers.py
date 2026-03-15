#tests/test_ingress_parsers.py

import pytest
from datetime import datetime, timezone, timedelta
from typing import Any

from pydantic import ValidationError
from uuid import uuid4

from src.core.errors import ContractViolationError
from src.core.ingress import (
    parse_alert,
    parse_cfar_event,
    parse_ai_anomaly_result,
    parse_whitelist_entry,
    parse_sdr_config,
    parse_processing_config,
)

from src.core.types import (
    Alert,
    CfarEvent,
    AiAnomalyResult,
    WhitelistEntry,
    SdrConfig,
    ProcessingConfig,
)

Payload = dict[str, Any]


def test_parse_alert_valid_returns_model():
    payload: Payload = {
        "severity": "info",
        "classification": "unknown",
        "center_freq_hz": 433e6,
        "description": "ok",
        "confidence_score": 0.5,
    }
    result = parse_alert(payload)
    assert isinstance(result, Alert)


def test_parse_cfar_event_valid_returns_model():
    payload: Payload = {
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "center_freq_hz": 100e6,
        "bandwidth_hz": 5000,
        "power_db": -50.0,
        "snr_db": 10.0,
    }
    result = parse_cfar_event(payload)
    assert isinstance(result, CfarEvent)


def test_parse_ai_anomaly_result_valid_returns_model():
    payload: Payload = {
        "source_event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "mse_score": 0.1,
        "is_anomaly": False,
        "processing_time_ms": 12.3,
    }
    result = parse_ai_anomaly_result(payload)
    assert isinstance(result, AiAnomalyResult)


def test_parse_whitelist_entry_valid_returns_model():
    payload: Payload = {
        "center_freq_hz": 100e6,
        "bandwidth_hz": 5000,
        "description": "Static Signal",
    }
    result = parse_whitelist_entry(payload)
    assert isinstance(result, WhitelistEntry)


def test_parse_sdr_config_valid_returns_model():
    payload: Payload = {
        "center_freq_hz": 100e6,
        "sample_rate_hz": 2e6,
        "gain_db": 30.0,
    }
    result = parse_sdr_config(payload)
    assert isinstance(result, SdrConfig)


def test_parse_processing_config_valid_returns_model():
    # Передаємо всі поля, щоб Pylance/типізація не “вигадували” required-параметри
    payload: Payload = {
        "fft_size": 1024,
        "cfar_threshold_db": 15.0,
        "ai_anomaly_threshold": 0.85,
    }
    result = parse_processing_config(payload)
    assert isinstance(result, ProcessingConfig)


def test_ingress_rejects_extra_fields():
    payload: Payload = {
        "severity": "info",
        "classification": "unknown",
        "center_freq_hz": 433e6,
        "description": "ok",
        "confidence_score": 0.5,
        "random_junk_field": "nope",
    }
    with pytest.raises(ContractViolationError) as exc:
        parse_alert(payload)
    assert exc.value.__cause__ is not None
    assert isinstance(exc.value.__cause__, ValidationError)


def test_ingress_rejects_naive_datetime():
    payload: Payload = {
        "severity": "info",
        "classification": "unknown",
        "center_freq_hz": 433e6,
        "description": "ok",
        "confidence_score": 0.5,
        "timestamp": datetime.now(),  # naive -> має впасти
    }
    with pytest.raises(ContractViolationError) as exc:
        parse_alert(payload)
    assert exc.value.__cause__ is not None
    assert isinstance(exc.value.__cause__, ValidationError)


def test_ingress_normalizes_aware_datetime_to_utc():
    tz_plus_2 = timezone(timedelta(hours=2))
    local_dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=tz_plus_2)  # це 10:00 UTC

    payload: Payload = {
        "severity": "info",
        "classification": "unknown",
        "center_freq_hz": 433e6,
        "description": "ok",
        "confidence_score": 0.5,
        "timestamp": local_dt,
    }

    alert = parse_alert(payload)

    expected = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert alert.timestamp.tzinfo == timezone.utc
    assert alert.timestamp == expected