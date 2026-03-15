# Основні транспортні механізми для Talos SDR Monitoring System.

from __future__ import annotations

import logging
import multiprocessing as mp
from multiprocessing.queues import Queue as MPQueue
from multiprocessing.sharedctypes import Synchronized
from queue import Empty, Full
from typing import Generic, Optional, Type, TypeVar, Union, cast

from pydantic import BaseModel

# Імпорт ТІЛЬКИ канонічних подій пайплайну (MVP 1.3 scope)
from src.core.types import Alert, AiAnomalyResult, CfarEvent

# --- CONTRACT DEFINITION ---

# Строгий Union для Type Hinting у споживачів (pipeline events only)
TalosEvent = Union[CfarEvent, AiAnomalyResult, Alert]

T = TypeVar("T", bound=BaseModel)


class TalosQueue(Generic[T]):
    """
    Типізована обгортка над multiprocessing.Queue з backpressure політикою.

    ARCHITECTURAL NOTE:
    Це IPC транспорт (Inter-Process Communication).
    multiprocessing.Queue передає об'єкти між процесами через pickle.
    Отже, payload має бути picklable; локальні in-process тести цього НЕ гарантують.

    SPAWN / PICKLE SAFETY:
    - Не зберігаємо logging.Logger як поле екземпляра, щоб уникнути проблем з pickle/spawn.
      Logger ініціалізується ліниво через property в контексті поточного процесу.

    OBSERVABILITY:
    - Локальний лічильник dropped_count (для періодичного логування).
    - Опційний shared dropped counter (mp.Value) для метрик/дашборду між процесами.
    
    HARDENING:
    - Strictly forbids Union/Tuple types in constructor. 
    - Enforces "One Queue = One Data Type" architecture.
    """

    def __init__(
        self,
        expected_type: Type[T],
        maxsize: int = 1000,
        name: str = "unknown",
        dropped_counter: Optional["Synchronized[int]"] = None,
    ):
        """
        Args:
            expected_type: Клас Pydantic-моделі для runtime-перевірки.
            maxsize: Ліміт черги для backpressure (RAM safety).
            name: Ім'я для логування.
            dropped_counter: Shared counter (mp.Value).
        """
        # --- ARCHITECTURAL GUARD (ADDED) ---
        # Забороняємо "Union-bus" (наприклад, expected_type=(EventA, EventB)).
        # Це змушує використовувати Explicit Channels.
        if isinstance(expected_type, (tuple, list)):
            raise TypeError(
                f"TalosQueue '{name}': Tuples/lists of types are FORBIDDEN. "
                "Use explicit channels (one queue per data type)."
            )
        # -----------------------------------

        # ВАЖЛИВО: використовуємо context (spawn/fork consistency).
        ctx = mp.get_context()

        # Pylance FIX: cast до MPQueue[T]
        self._queue: MPQueue[T] = cast(MPQueue[T], ctx.Queue(maxsize=maxsize))

        self._expected_type = expected_type
        self.name = name

        # Local stats (for periodic logging, per-process)
        self.dropped_count: int = 0

        # Shared stats (for metrics/dashboard, cross-process)
        self._shared_dropped = dropped_counter

    @property
    def _logger(self) -> logging.Logger:
        # Lazy initialization ensures logger is created in the correct process context
        return logging.getLogger(f"talos.core.bus.{self.name}")

    def push_nowait(self, item: T) -> bool:
        """
        Fast Path (SDR/DSP): non-blocking drop if full.
        """
        if __debug__:
            self._validate_type(item)

        try:
            self._queue.put_nowait(item)
            return True
        except Full:
            self._handle_drop()
            return False

    def push(self, item: T, timeout: float = 1.0) -> bool:
        """
        Critical Path (Logic/Alerts): blocking put with timeout.
        """
        if __debug__:
            self._validate_type(item)

        try:
            self._queue.put(item, block=True, timeout=timeout)
            return True
        except Full:
            self._logger.error("Critical queue full. Lost item after %.2fs", timeout)
            self._handle_drop()
            return False

    def pop(self, timeout: float = 0.1) -> Optional[T]:
        """
        Consumer side.
        """
        try:
            return self._queue.get(block=True, timeout=timeout)
        except Empty:
            return None

    def _handle_drop(self) -> None:
        """
        Increments local and shared counters, logs periodically.
        """
        self.dropped_count += 1

        # Update shared metric if provided
        if self._shared_dropped is not None:
            with self._shared_dropped.get_lock():
                self._shared_dropped.value += 1

        # Log rarely to avoid spamming and CPU overhead
        if self.dropped_count % 100 == 1:
            self._logger.warning("Queue full. Dropped=%d", self.dropped_count)

    def _validate_type(self, item: T) -> None:
        """
        Debug-only guard.
        """
        if not isinstance(item, self._expected_type):
            raise TypeError(
                f"Contract Violation in {self.name}: "
                f"Expected {self._expected_type.__name__}, got {type(item).__name__}."
            )