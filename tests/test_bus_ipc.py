# tests/test_bus_ipc.py
import multiprocessing as mp
from multiprocessing.connection import Connection
from typing import Any

import pytest
from uuid import uuid4

from src.core.bus import TalosQueue
from src.core.types import CfarEvent, utc_now


Payload = dict[str, Any]


def _make_cfar_payload() -> Payload:
    # IMPORTANT: робимо payload і парсимо через model_validate (Pylance-friendly + true-to-life)
    return {
        "event_id": str(uuid4()),
        "timestamp": utc_now(),
        "center_freq_hz": 433e6,
        "bandwidth_hz": 1e6,
        "power_db": -50.0,
        "snr_db": 15.0,
        "noise_floor_db": -65.0,
        # optional поля можна не давати взагалі (якщо в types.py вони Optional[...] = None)
        # "duration_s": None,
        # "duty_cycle": None,
    }


def _child_put_event(q: TalosQueue[CfarEvent], payload: Payload) -> None:
    event = CfarEvent.model_validate(payload)  # forces pydantic path
    ok = q.push(event, timeout=1.0)
    if not ok:
        raise RuntimeError("Failed to push event from child process")


def _child_get_event_and_ack(q: TalosQueue[CfarEvent], conn: Connection) -> None:
    item = q.pop(timeout=2.0)
    if item is None:
        conn.send(("error", "timeout"))
        return
    conn.send(("ok", str(item.event_id), float(item.center_freq_hz)))


@pytest.mark.timeout(10)
def test_ipc_child_to_parent_pickling_roundtrip():
    """
    Child -> Parent через multiprocessing.Queue (реальний pickle/IPC шлях).
    """
    ctx = mp.get_context("spawn")

    q: TalosQueue[CfarEvent] = TalosQueue(CfarEvent, maxsize=10, name="ipc_child_to_parent")
    payload = _make_cfar_payload()

    p = ctx.Process(target=_child_put_event, args=(q, payload), daemon=True)
    p.start()

    got = q.pop(timeout=3.0)

    p.join(timeout=3.0)
    assert p.exitcode == 0

    assert got is not None
    assert isinstance(got, CfarEvent)
    assert str(got.event_id) == payload["event_id"]
    assert got.center_freq_hz == payload["center_freq_hz"]


@pytest.mark.timeout(10)
def test_ipc_parent_to_child_pickling_roundtrip():
    """
    Parent -> Child через multiprocessing.Queue + ack назад через Pipe.
    """
    ctx = mp.get_context("spawn")

    q: TalosQueue[CfarEvent] = TalosQueue(CfarEvent, maxsize=10, name="ipc_parent_to_child")
    parent_conn, child_conn = ctx.Pipe(duplex=False)

    p = ctx.Process(target=_child_get_event_and_ack, args=(q, child_conn), daemon=True)
    p.start()

    payload = _make_cfar_payload()
    event = CfarEvent.model_validate(payload)
    assert q.push(event, timeout=1.0) is True

    status, *ack = parent_conn.recv()

    p.join(timeout=3.0)
    assert p.exitcode == 0

    assert status == "ok"
    received_event_id, received_freq = ack
    assert received_event_id == payload["event_id"]
    assert received_freq == float(payload["center_freq_hz"])