#src/core/service.py
import logging
import signal
import time
import threading
import multiprocessing as mp
from abc import ABC, abstractmethod
from typing import Optional, final
from types import FrameType

class BaseService(mp.Process, ABC):
    """
    Абстрактний базовий клас для всіх Worker-процесів TALOS.
    
    ARCHITECTURAL GUARANTEES:
    1. Graceful Shutdown: Гарантує виклик teardown() при SIGTERM/SIGINT.
    2. Daemon=False: Процес не вмирає миттєво при виході Parent.
    3. Isolation: Помилки в loop() не валять процес, а викликають backoff.
    """

    def __init__(
        self, 
        name: str, 
        enabled: bool = True,
        loop_sleep_s: float = 0.0,
        error_sleep_s: float = 1.0
    ):
        super().__init__(name=name, daemon=False)
        self.service_name = name
        self._enabled = enabled
        self._loop_sleep_s = loop_sleep_s
        self._error_sleep_s = error_sleep_s
        
        self._stop_event = mp.Event()
        self._logger: Optional[logging.Logger] = None

    @final
    def run(self) -> None:
        """
        SEALED: Точка входу в процес.
        Керує життєвим циклом: Setup -> Loop -> Teardown.
        """
        self._setup_logging_safe()
        self._setup_signals()

        if not self._enabled:
            self.logger.info("Service disabled by config. Exiting.")
            return

        self.logger.info("Starting service lifecycle...")

        try:
            self.setup()
            
            # Main Loop
            while not self._stop_event.is_set():
                try:
                    self.execute()
                    if self._loop_sleep_s > 0:
                        time.sleep(self._loop_sleep_s)
                except Exception as e:
                    self.logger.error("Error in main loop: %s", e, exc_info=True)
                    time.sleep(self._error_sleep_s)
                    
        except Exception as e:
            self.logger.critical("Fatal service error: %s", e, exc_info=True)
        finally:
            self.logger.info("Stopping service...")
            try:
                self.teardown()
            except Exception as e:
                self.logger.error("Error during teardown: %s", e, exc_info=True)
            self.logger.info("Service stopped.")

    def request_stop(self) -> None:
        """Thread-safe метод для запиту зупинки ззовні."""
        self._stop_event.set()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            return logging.getLogger(f"talos.{self.service_name}")
        return self._logger

    # --- ABSTRACT INTERFACE ---

    @abstractmethod
    def setup(self) -> None:
        pass

    @abstractmethod
    def execute(self) -> None:
        pass

    @abstractmethod
    def teardown(self) -> None:
        pass

    # --- INTERNAL ---

    def _setup_signals(self) -> None:
        """
        Налаштування сигналів. Працює тільки в Main Thread.
        """
        if threading.current_thread() is not threading.main_thread():
            self.logger.warning("Signal handlers not installed (not in main thread).")
            return

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Optional[FrameType]) -> None:
        self.logger.info("Received signal %s. Initiating graceful shutdown.", signum)
        self.request_stop()

    def _setup_logging_safe(self) -> None:
        root = logging.getLogger()
        if not root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] [%(processName)s] %(message)s"
            )
        self._logger = logging.getLogger(f"talos.{self.service_name}")