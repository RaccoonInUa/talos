# src/core/types.py
# Основні типи та моделі Pydantic для Talos SDR Monitoring System.

from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, ConfigDict, field_validator

# --- HELPERS ---

def utc_now() -> datetime:
    """Генератор поточного часу в UTC."""
    return datetime.now(timezone.utc)

def ensure_utc(v: datetime) -> datetime:
    """
    Нормалізатор часу.
    1. Відхиляє naive datetime (щоб уникнути плутанини).
    2. Конвертує будь-який aware datetime в UTC.
    """
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError("Timestamp must be timezone-aware (naive datetimes are forbidden)")
    return v.astimezone(timezone.utc)

# --- BASE MODELS ---

class TalosBaseModel(BaseModel):
    """
    Базова мутабельна модель.
    Забезпечує сувору валідацію: extra='forbid', enum values.
    """
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        populate_by_name=True
    )

class TalosFrozenModel(TalosBaseModel):
    """
    Базова імутабельна модель для конфігурацій.
    Явно дублюємо model_config, оскільки frozen=True у Pydantic v2
    перезаписує успадковані налаштування.
    """
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        populate_by_name=True,
        frozen=True
    )

# --- ENUMS (Semantic Layer) ---

class HalState(str, Enum):
    """
    Технічні стани драйвера SDR (Hardware Layer).
    УВАГА: Не плутати зі станами бізнес-логіки (FSM: IDLE/TRACKING/ALERT).
    """
    DISCONNECTED = "disconnected"
    READY = "ready"       # Пристрій ініціалізовано, буфер пустий
    SCANNING = "scanning" # Активний стрімінг I/Q семплів
    ERROR = "error"

class EventSeverity(str, Enum):
    """Рівень пріоритету події для Logic Engine"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SignalClassification(str, Enum):
    """Класифікатор сигналів (MVP 1.3 Scope)"""
    UNKNOWN = "unknown"
    STATIC = "static"     # Whitelisted signal

class SimulationIntensity(str, Enum):
    """Simulation load profile for RF environment"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

# --- CONFIGURATIONS (Immutable) ---

class SdrConfig(TalosFrozenModel):
    """Параметри SDR приймача"""
    center_freq_hz: float = Field(..., gt=0, description="Центральна частота")
    sample_rate_hz: float = Field(..., gt=0, description="Швидкість потоку")
    gain_db: float = Field(..., ge=0, le=60, description="Посилення")

class ProcessingConfig(TalosFrozenModel):
    """Параметри DSP та AI пайплайну"""
    fft_size: int = Field(1024, ge=256, le=16384)
    cfar_threshold_db: float = Field(15.0, ge=0)
    ai_anomaly_threshold: float = Field(0.85, ge=0, description="Поріг MSE")
    signal_separation_hz: float = 50_000

# --- PIPELINE DTOs (High Frequency Traffic) ---

class WaterfallHeader(TalosBaseModel):
    """
    Метадані для бінарного потоку спектру (WebSocket).
    """
    timestamp: datetime
    center_freq_hz: float = Field(..., gt=0)
    fft_size: int = Field(..., ge=256, le=16384)
    num_bins: int = Field(..., gt=0, description="Кількість точок (зазвичай = fft_size)")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

class WaterfallFrame(TalosBaseModel):
    """
    Мінімалістичний кадр телеметрії для водоспаду.
    Генерується в DSP (SdrMonitor) після downsampling та квантизації.
    """
    frame_seq: int = Field(..., description="Монотонний лічильник FFT кадрів")
    timestamp: datetime

    center_freq_hz: float = Field(..., gt=0)
    bin_hz: float = Field(..., description="Ширина одного пікселя (біна) у Гц")

    # Дані стиснуті у байти (uint8) для миттєвої передачі через WebSocket.
    # Значення 0‑255 мапляться на кольорову палітру у браузері.
    line_uint8: bytes

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

class CfarEvent(TalosBaseModel):
    """
    Level 1: Подія енергетичної детекції (CFAR).
    Producer: DSP Engine
    """
    event_id: UUID
    timestamp: datetime

    # Синхронізація з водоспадом (UI overlay)
    source_frame_seq: int = 0  # optional for backward compatibility
    
    center_freq_hz: float = Field(..., gt=0)
    bandwidth_hz: float = Field(..., gt=0)
    power_db: float
    snr_db: float
    
    # Optional field для дебагу, не блокує інтеграцію
    noise_floor_db: Optional[float] = None 

    # Level 2 Readiness
    duration_s: Optional[float] = Field(None, ge=0)
    duty_cycle: Optional[float] = Field(None, ge=0, le=1.0)

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

class AiAnomalyResult(TalosBaseModel):
    """
    Level 1: Результат роботи автоенкодера.
    Producer: AI Worker
    """
    result_id: UUID = Field(default_factory=uuid4)
    source_event_id: UUID # ID події CFAR або вікна
    timestamp: datetime
    
    mse_score: float = Field(..., ge=0)
    is_anomaly: bool
    processing_time_ms: float = Field(..., ge=0)

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

class SystemStatus(TalosBaseModel):
    """Health Check телеметрія"""
    timestamp: datetime = Field(default_factory=utc_now)
    
    cpu_temp_c: float
    cpu_usage_pct: float = Field(..., ge=0, le=100)
    memory_usage_bytes: int = Field(..., ge=0)
    hal_state: HalState

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

# --- LOGIC & API LAYER ---

class WhitelistEntry(TalosBaseModel):
    """Запис у базі відомих частот"""
    center_freq_hz: float = Field(..., gt=0)
    bandwidth_hz: float = Field(..., gt=0)
    description: Optional[str] = "Static Signal"
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

class Alert(TalosBaseModel):
    """
    Level 3: Фінальна агрегована подія.
    Producer: Decision Logic
    Consumer: API / Database
    """
    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=utc_now)
    
    # Traceability
    source_cfar_event_id: Optional[UUID] = None
    source_ai_result_id: Optional[UUID] = None

    # Дозволяє прив'язати Alert до конкретного рядка водоспаду
    source_frame_seq: Optional[int] = 0
    
    severity: EventSeverity
    classification: SignalClassification
    
    center_freq_hz: float = Field(..., gt=0)
    description: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, v: datetime) -> datetime:
        return ensure_utc(v)

class TalosConfig(TalosFrozenModel):
    """
    Головний об'єкт конфігурації системи.
    Агрегує налаштування всіх підсистем.
    """
    simulation_intensity: SimulationIntensity = SimulationIntensity.MEDIUM
    sdr: SdrConfig
    processing: ProcessingConfig
    # У майбутньому сюди додадуться:
    # api: ApiConfig
    # storage: StorageConfig