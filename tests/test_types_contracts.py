# tests/test_types_contracts.py
import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from pydantic import ValidationError
from typing import Any

Payload = dict[str, Any]
PC_BASE: Payload = {
    "cfar_threshold_db": 15.0,
    "ai_anomaly_threshold": 0.85,
}

from src.core.types import (
    Alert,
    CfarEvent,
    AiAnomalyResult,
    WhitelistEntry,
    ProcessingConfig,
    SdrConfig,
    EventSeverity,
    SimulationIntensity,
    TalosConfig,
)

# --- 1. TIMEZONE CONTRACTS ---

def test_naive_datetime_forbidden():
    """
    Verifies that passing a naive datetime payload raises a ValidationError.
    Uses model_validate to simulate raw data ingress.
    """
    naive_dt = datetime.now()
    
    # Payload simulates incoming dict (e.g. from DB or API)
    payload: Payload = {
        "center_freq_hz": 100e6,
        "bandwidth_hz": 5000,
        "created_at": naive_dt
    }

    with pytest.raises(ValidationError) as exc_info:
        WhitelistEntry.model_validate(payload)
    
    errors = exc_info.value.errors()
    assert any("created_at" in e["loc"] for e in errors), \
        f"Expected error on 'created_at', got: {errors}"

def test_aware_datetime_normalized_to_utc():
    """
    Verifies that a timezone-aware datetime in payload is normalized to UTC.
    """
    # 12:00 UTC+2
    tz_plus_2 = timezone(timedelta(hours=2))
    local_dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=tz_plus_2)

    payload: Payload = {
        "severity": "info",  # simulating raw string input
        "classification": "unknown",
        "center_freq_hz": 433e6,
        "description": "Test Event",
        "confidence_score": 0.9,
        "timestamp": local_dt
    }

    alert = Alert.model_validate(payload)

    # Contract: Must be converted to 10:00 UTC
    expected_dt = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    
    assert alert.timestamp.tzinfo == timezone.utc
    assert alert.timestamp == expected_dt

def test_ai_anomaly_result_timestamp_normalization():
    """
    Verifies that AiAnomalyResult normalizes timestamp from raw payload.
    """
    tz_minus_5 = timezone(timedelta(hours=-5))
    local_dt = datetime(2025, 1, 1, 10, 0, 0, tzinfo=tz_minus_5) # 15:00 UTC

    payload: Payload = {
        "source_event_id": str(uuid4()), # simulating UUID as string
        "timestamp": local_dt,
        "mse_score": 0.1,
        "is_anomaly": False,
        "processing_time_ms": 10.5
    }

    anomaly = AiAnomalyResult.model_validate(payload)

    expected_dt = datetime(2025, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    assert anomaly.timestamp.tzinfo == timezone.utc
    assert anomaly.timestamp == expected_dt

# --- 2. CONFIGURATION & STRICTNESS CONTRACTS ---

def test_extra_fields_forbidden_config():
    """
    Verifies that SdrConfig forbids extra fields in the raw payload.
    """
    payload: Payload = {
        "center_freq_hz": 100e6,
        "sample_rate_hz": 2e6,
        "gain_db": 30,
        "unexpected_field": "Should fail" # <--- Extra field
    }

    with pytest.raises(ValidationError) as exc_info:
        SdrConfig.model_validate(payload)
    
    errors = exc_info.value.errors()
    assert any(e.get("type") == "extra_forbidden" for e in errors)

def test_alert_extra_fields_forbidden():
    """
    Verifies that Alert model forbids extra fields (Strict API Contract).
    """
    payload: Payload = {
        "severity": "critical",
        "classification": "unknown",
        "center_freq_hz": 900e6,
        "description": "Intruder",
        "confidence_score": 0.99,
        "random_junk_field": "should fail" # <--- Extra field
    }

    with pytest.raises(ValidationError) as exc_info:
        Alert.model_validate(payload)
    
    errors = exc_info.value.errors()
    assert any(e.get("type") == "extra_forbidden" for e in errors)

def test_frozen_config_immutability():
    """
    Verifies that Configuration models are immutable (frozen).
    Here we validate creation works, then modification fails.
    """
    payload: Payload = {"fft_size": 1024, "cfar_threshold_db": 15.0, "ai_anomaly_threshold": 0.85}
    config = ProcessingConfig.model_validate(payload)
    
    with pytest.raises((TypeError, ValidationError)):
        config.fft_size = 2048

# --- 3. MANDATORY FIELD CONTRACTS (MISSING) ---

def test_ai_anomaly_result_source_event_id_mandatory():
    """
    Verifies that source_event_id is mandatory.
    We test this by validating a payload missing that key.
    """
    payload: Payload = {
        # "source_event_id": MISSING, 
        "timestamp": datetime.now(timezone.utc),
        "mse_score": 0.5,
        "is_anomaly": True,
        "processing_time_ms": 12.0
    }

    with pytest.raises(ValidationError) as exc_info:
        AiAnomalyResult.model_validate(payload)
    
    errors = exc_info.value.errors()
    # Pydantic reports missing fields with type 'missing'
    assert any("source_event_id" in e["loc"] for e in errors)

# --- 4. NUMERIC BOUNDARY & ENUM CONTRACTS ---

def test_alert_confidence_score_bounds_and_enums():
    """
    Verifies confidence_score limits [0.0, 1.0] via model_validate.
    Also validates that raw strings map correctly to Enums.
    """
    # 1. Valid Case (String -> Enum coercion)
    valid_payload: Payload = {
        "severity": "info", 
        "classification": "static", 
        "center_freq_hz": 1e6, 
        "description": "ok", 
        "confidence_score": 0.5
    }
    alert = Alert.model_validate(valid_payload)
    
    assert alert.severity == EventSeverity.INFO.value
    assert isinstance(alert.severity, str) # Because use_enum_values=True

    # 2. Upper bound violation (> 1.0)
    bad_payload_upper = valid_payload.copy()
    bad_payload_upper["confidence_score"] = 1.1

    with pytest.raises(ValidationError) as exc:
        Alert.model_validate(bad_payload_upper)
    assert any(e['type'] == 'less_than_equal' for e in exc.value.errors())

    # 3. Lower bound violation (< 0.0)
    bad_payload_lower = valid_payload.copy()
    bad_payload_lower["confidence_score"] = -0.1

    with pytest.raises(ValidationError) as exc:
        Alert.model_validate(bad_payload_lower)
    assert any(e['type'] == 'greater_than_equal' for e in exc.value.errors())

def test_processing_config_fft_size_bounds():
    """
    Verifies FFT size strict limits via model_validate.
    """
    # Too small
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({**PC_BASE, "fft_size": 128})

    # Too large
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({**PC_BASE, "fft_size": 32768})

    # Valid
    ProcessingConfig.model_validate({**PC_BASE, "fft_size": 256})
    ProcessingConfig.model_validate({**PC_BASE, "fft_size": 16384})

def test_cfar_event_frequency_and_bandwidth_positive():
    """
    Verifies numeric constraints for CfarEvent via payload validation.
    """
    base_payload: Payload = {
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "power_db": -50,
        "snr_db": 10,
        "center_freq_hz": 100e6,
        "bandwidth_hz": 5000
    }

    # 1. Invalid Bandwidth (<= 0)
    bad_bw = base_payload.copy()
    bad_bw["bandwidth_hz"] = 0
    with pytest.raises(ValidationError) as exc_bw:
        CfarEvent.model_validate(bad_bw)
    assert any("bandwidth_hz" in e["loc"] for e in exc_bw.value.errors())

    # 2. Invalid Center Frequency (<= 0)
    bad_freq = base_payload.copy()
    bad_freq["center_freq_hz"] = 0
    with pytest.raises(ValidationError) as exc_freq:
        CfarEvent.model_validate(bad_freq)
    assert any("center_freq_hz" in e["loc"] for e in exc_freq.value.errors())

def test_sdr_gain_limits():
    """
    Verifies gain limits via model_validate.
    """
    # Negative gain
    with pytest.raises(ValidationError):
        SdrConfig.model_validate({"center_freq_hz": 1e6, "sample_rate_hz": 2e6, "gain_db": -1})
    
    # Excessive gain
    with pytest.raises(ValidationError):
        SdrConfig.model_validate({"center_freq_hz": 1e6, "sample_rate_hz": 2e6, "gain_db": 61})


def test_talos_config_accepts_simulation_intensity_values():
    payload: Payload = {
        "simulation_intensity": "low",
        "sdr": {"center_freq_hz": 1e6, "sample_rate_hz": 2e6, "gain_db": 10.0},
        "processing": {
            "fft_size": 1024,
            "cfar_threshold_db": 15.0,
            "ai_anomaly_threshold": 0.85,
        },
    }

    cfg = TalosConfig.model_validate(payload)
    assert cfg.simulation_intensity == SimulationIntensity.LOW


def test_talos_config_rejects_invalid_simulation_intensity():
    payload: Payload = {
        "simulation_intensity": "ultra",
        "sdr": {"center_freq_hz": 1e6, "sample_rate_hz": 2e6, "gain_db": 10.0},
        "processing": {
            "fft_size": 1024,
            "cfar_threshold_db": 15.0,
            "ai_anomaly_threshold": 0.85,
        },
    }

    with pytest.raises(ValidationError):
        TalosConfig.model_validate(payload)
