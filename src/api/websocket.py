# /src/api/websocket.py

import asyncio
import logging
import struct
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("websocket_logger")
router = APIRouter()

@router.websocket("/ws/waterfall")
async def waterfall_stream(websocket: WebSocket):
    await websocket.accept()
    orch = websocket.app.state.orchestrator
    
    target_fps = 30
    frame_interval = 1.0 / target_fps
    
    # 3. Виправлено: Fixed cadence (absolute ticks) для зменшення jitter
    next_tick = time.monotonic()

    try:
        while True:
            # 4. Вимоги до Orchestrator: метод МАЄ бути lock-free (atomic swap)
            frame = orch.get_latest_waterfall_frame()

            if frame:
                header = struct.pack(
                    "<QddQ",
                    frame.frame_seq,
                    frame.center_freq_hz,
                    frame.bin_hz,
                    0,
                )

                payload = frame.line_uint8[:256]

                # Hard transport contract: payload must always be exactly 256 bytes
                # so the browser receives a fixed 288-byte packet.
                if len(payload) < 256:
                    payload = payload.ljust(256, b"\x00")

                # ASGI-safe send: header + payload already produces immutable bytes.
                await websocket.send_bytes(header + payload)

            now = time.monotonic()
            sleep_time = next_tick - now
            
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                # Відстаємо від графіка (CPU bottleneck). 
                # Скидаємо tick, щоб не було "catch-up" сплесків кадрів
                await asyncio.sleep(0)
                next_tick = now 
                
            next_tick += frame_interval

    except WebSocketDisconnect:
        pass


@router.websocket("/ws/events")
async def events_stream(websocket: WebSocket):
    await websocket.accept()
    orch = websocket.app.state.orchestrator
    
    client_queue = orch.subscribe_events()

    MAX_EVENTS_PER_SEC = 50
    min_interval = 1.0 / MAX_EVENTS_PER_SEC
    last_send_time = 0.0
    last_log_time = 0.0

    try:
        while True:
            try:
                event = await asyncio.wait_for(client_queue.get(), timeout=10.0)

                if event:
                    now = time.monotonic()
                    is_alert = event.__class__.__name__ == "Alert"

                    if is_alert or (now - last_send_time >= min_interval):
                        await websocket.send_json({
                            "type": event.__class__.__name__,
                            "payload": event.model_dump(mode="json"),
                        })
                        last_send_time = now
                    else:
                        if now - last_log_time > 1.0:
                            logger.warning("Event storm: dropping UI broadcast for a client")
                            last_log_time = now
                        # Adaptive sleep tied to throttle interval to avoid CPU spin and preserve latency
                        sleep_time = min_interval - (now - last_send_time)
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        else:
                            await asyncio.sleep(0)
                        continue

            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({
                        "type": "Heartbeat",
                        "payload": {"timestamp": time.time()}
                    })
                except Exception:
                    break

            await asyncio.sleep(0)

    except WebSocketDisconnect:
        logger.debug("Event client disconnected via protocol.")
    except Exception as e:
        logger.error(f"Event stream error: {e}")
    finally:
        if client_queue is not None:
            orch.unsubscribe_events(client_queue)
        logger.debug("Event client queue destroyed and unsubscribed.")