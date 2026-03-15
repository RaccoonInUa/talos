# tests/test_bus.py 
import pytest
from src.core.bus import TalosQueue
from src.core.types import CfarEvent, Alert, EventSeverity, SignalClassification, utc_now
from uuid import uuid4

# Mock Data
def make_cfar():
    return CfarEvent(
        event_id=uuid4(),
        timestamp=utc_now(),
        center_freq_hz=433e6,
        bandwidth_hz=1e6,
        power_db=-50.0,
        snr_db=15.0,
        noise_floor_db=-65.0
    ) # type: ignore

def make_alert():
    return Alert(
        id=uuid4(),
        timestamp=utc_now(),
        severity=EventSeverity.CRITICAL,
        # FIXED: Використовуємо UNKNOWN згідно MVP 1.3
        classification=SignalClassification.UNKNOWN, 
        center_freq_hz=900e6,
        description="Test",
        confidence_score=0.9
    )

def test_queue_strict_typing():
    """Перевірка: черга CfarEvent має відхиляти Alert."""
    if not __debug__:
        pytest.skip("Type checks disabled in optimized mode")

    # Створюємо чергу СУВОРО для CfarEvent
    q = TalosQueue(CfarEvent, maxsize=10, name="strict_test")
    
    # 1. Happy path
    assert q.push_nowait(make_cfar()) is True
    
    # 2. Wrong Model Type
    alert = make_alert()
    with pytest.raises(TypeError) as exc:
        q.push_nowait(alert) # type: ignore
    
    assert "Expected CfarEvent, got Alert" in str(exc.value)

def test_queue_backpressure():
    """Перевірка лімітів черги."""
    q = TalosQueue(CfarEvent, maxsize=1, name="limit_test")
    
    assert q.push_nowait(make_cfar()) is True
    
    # Переповнення
    assert q.push_nowait(make_cfar()) is False
    assert q.dropped_count == 1