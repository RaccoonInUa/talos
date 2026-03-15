import logging
from typing import Any, Mapping, Type, TypeVar

from pydantic import BaseModel, ValidationError

from src.core.errors import ContractViolationError
from src.core.types import (
    Alert,
    CfarEvent,
    AiAnomalyResult,
    WhitelistEntry,
    SdrConfig,
    ProcessingConfig,
)

__all__ = [
    "parse_alert",
    "parse_cfar_event",
    "parse_ai_anomaly_result",
    "parse_whitelist_entry",
    "parse_sdr_config",
    "parse_processing_config",
]

# Standardized logger namespace
logger = logging.getLogger("talos.core.ingress")

# Generic TypeVar bound to Pydantic BaseModel
T = TypeVar("T", bound=BaseModel)

def _validate(model: Type[T], payload: Mapping[str, Any]) -> T:
    """
    Generic validator acting as the strict boundary enforcement.
    It relies on model.__name__ for dynamic error context.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as e:
        # Log stack trace for debugging (internal detail)
        logger.exception(
            "Ingress Protocol Violation: Failed to parse entity '%s'",
            model.__name__,
        )
        # Raise semantic error for caller (external contract)
        # Keeps the original traceback via 'from e'
        raise ContractViolationError(
            f"Contract violation while parsing {model.__name__}"
        ) from e

def parse_alert(payload: Mapping[str, Any]) -> Alert:
    return _validate(Alert, payload)

def parse_cfar_event(payload: Mapping[str, Any]) -> CfarEvent:
    return _validate(CfarEvent, payload)

def parse_ai_anomaly_result(payload: Mapping[str, Any]) -> AiAnomalyResult:
    return _validate(AiAnomalyResult, payload)

def parse_whitelist_entry(payload: Mapping[str, Any]) -> WhitelistEntry:
    return _validate(WhitelistEntry, payload)

def parse_sdr_config(payload: Mapping[str, Any]) -> SdrConfig:
    return _validate(SdrConfig, payload)

def parse_processing_config(payload: Mapping[str, Any]) -> ProcessingConfig:
    return _validate(ProcessingConfig, payload)