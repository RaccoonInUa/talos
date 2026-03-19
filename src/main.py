# src/main.py

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from fastapi import FastAPI
from contextlib import asynccontextmanager
from src.api.websocket import router as ws_router

from src.core.config import load_config
from src.core.orchestrator import ServiceOrchestrator


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler (replaces deprecated on_event).
    """
    setup_logging(debug=False)

    config = load_config()

    orchestrator = ServiceOrchestrator(config)
    orchestrator.setup()
    orchestrator.start()

    app.state.orchestrator = orchestrator

    try:
        yield
    finally:
        orch = getattr(app.state, "orchestrator", None)
        if orch:
            orch.request_stop()
            orch.shutdown()


# -----------------------------
# ASGI App (FastAPI)
# -----------------------------

app = FastAPI(title="Talos API", lifespan=lifespan)

# Register routers
app.include_router(ws_router)


def setup_logging(*, debug: bool = False) -> None:
    """
    Production-grade logging setup:
      - Console handler
      - Rotating file handler
      - No duplicate handlers
    """
    level = logging.DEBUG if debug else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    # Prevent double configuration (important for tests)
    if getattr(root, "_talos_configured", False):
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # -----------------------
    # Console
    # -----------------------
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)

    # -----------------------
    # File (rotating)
    # -----------------------
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "talos.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)

    root._talos_configured = True  # type: ignore[attr-defined]

    logging.getLogger("talos.main").info(
        "Logging initialized (level=%s, file=%s)",
        logging.getLevelName(level),
        log_dir / "talos.log",
    )


def main() -> int:
    setup_logging(debug=False)

    try:
        config = load_config()

        orchestrator = ServiceOrchestrator(config)
        orchestrator.setup()
        orchestrator.start()

        return orchestrator.run_forever()

    except KeyboardInterrupt:
        logging.getLogger("talos.main").info("Interrupted by user.")
        return 0
    except Exception:
        logging.getLogger("talos.main").exception("FATAL STARTUP ERROR")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())