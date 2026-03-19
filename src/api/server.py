# src/api/server.py
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from src.api.websocket import router as ws_router
from typing import Any, Dict, List
# pyright: reportUnusedFunction=false

def create_app(orchestrator: Any) -> FastAPI:
    app = FastAPI(
        title="TALOS API", 
        description="Autonomous RF Monitoring System API"
    )

    app.state.orchestrator = orchestrator

    # 1. Виправлено: Монтування директорії для JS/CSS/WASM
    app.mount("/static", StaticFiles(directory="static"), name="static")

    app.include_router(ws_router)

    @app.get("/")
    async def read_root() -> FileResponse:
        # Тепер браузер коректно підтягне ресурси відносно /static/
        return FileResponse("static/index.html")

    @app.get("/status")
    async def get_status(request: Request) -> Dict[str, Any]:
        orch = request.app.state.orchestrator
        metrics = orch.metrics
        
        return {
            "frames_ok": metrics.get("frames_ok", 0),
            "frames_skipped": metrics.get("frames_skipped", 0),
            "cfar_detections": metrics.get("cfar_detections", 0),
            "clusters_emitted": metrics.get("clusters_emitted", 0),
            "events_emitted": metrics.get("events_emitted", 0),
            "waterfall_frames_emitted": metrics.get("waterfall_frames_emitted", 0),
            "waterfall_frames_dropped": metrics.get("waterfall_frames_dropped", 0),
            "queue_sizes": {
                "cfar": metrics.get("queue_cfar", [0, 0]),
                "alert": metrics.get("queue_alert", [0, 0]),
                "waterfall": metrics.get("queue_waterfall", [0, 0])
            },
            # 7. Виправлено: Безпечна серіалізація Enum/Об'єкта
            "hal_state": str(orch.hal_state) 
        }

    @app.get("/alerts")
    async def get_alerts(request: Request) -> List[Dict[str, Any]]:
        orch = request.app.state.orchestrator
        alerts = orch.get_recent_alerts()
        return [alert.model_dump(mode='json') for alert in alerts]

    @app.get("/config")
    async def get_config(request: Request) -> Dict[str, Any]:
        orch = request.app.state.orchestrator
        return orch.config.model_dump(mode='json')

    return app