# src/core/logging_config.py
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path


def setup_logging(
    *,
    log_level: str = "INFO",
    log_dir: str = "logs",
    log_file: str = "talos.log",
) -> None:
    """
    Central logging setup:
      - Console handler
      - Rotating file handler
    Call once at program start (src/main.py).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_path = os.path.join(log_dir, log_file)

    root = logging.getLogger()
    root.setLevel(level)

    # Prevent duplicate handlers if setup_logging is called twice
    if getattr(root, "_talos_configured", False):
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)

    # File with rotation
    fh = logging.handlers.RotatingFileHandler(
        file_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)

    root.addHandler(ch)
    root.addHandler(fh)

    root._talos_configured = True  # type: ignore[attr-defined]
    logging.getLogger("talos").info("Logging initialized (level=%s, file=%s)", log_level, file_path)