# tests/test_hal_sdr.py

#!!!
# pyright: reportPrivateUsage=false
#!!!

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from src.core.types import HalState, SdrConfig
from src.hal.sdr import SdrDriver


# -----------------------------------------------------------------------------
# Fakes for "hardware" path (no SoapySDR needed)
# -----------------------------------------------------------------------------

@dataclass
class _FakeStreamResult:
    ret: int


class _FakeSDR:
    """
    Minimal fake of a SoapySDR-like device:
      readStream(stream, [buf], n, timeoutUs=...) -> object/int/tuple
    """

    def __init__(self, behavior: str, expected_n: int):
        self.behavior = behavior
        self.expected_n = expected_n

    def readStream(self, stream: Any, buffs: list[np.ndarray], n: int, **kwargs: Any) -> Any:
        assert stream is not None
        assert n == self.expected_n
        assert len(buffs) == 1

        buf = buffs[0]
        if buf.size:
            buf[:] = (1.0 + 1.0j)

        if self.behavior == "full_obj":
            return _FakeStreamResult(ret=n)
        if self.behavior == "full_int":
            return n
        if self.behavior == "full_tuple":
            return (n, 0, 0)
        if self.behavior == "partial":
            return _FakeStreamResult(ret=n - 1)
        if self.behavior == "error":
            return _FakeStreamResult(ret=-5)
        if self.behavior == "raises":
            raise RuntimeError("boom")
        raise AssertionError(f"Unknown behavior: {self.behavior}")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _mk_cfg() -> SdrConfig:
    """
    IMPORTANT: SdrConfig forbids extra fields, so keep it minimal.
    Adjust names if your DTO differs.
    """
    return SdrConfig(
        center_freq_hz=433e6,
        sample_rate_hz=2_000_000,
        gain_db=10.0,
    )


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

def test_read_samples_not_scanning_returns_empty() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.DISCONNECTED

    out = d.read_samples(1024)
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.complex64
    assert out.size == 0


def test_emulator_returns_full_frame_when_scanning() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.SCANNING
    d._is_emulated = True  # force emulator branch

    n = 2048
    out = d.read_samples(n)
    assert out.dtype == np.complex64
    assert out.size == n


def test_hardware_full_frame_object_ret() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.SCANNING
    d._is_emulated = False

    n = 1024
    d._stream = object()
    d._sdr = _FakeSDR("full_obj", expected_n=n)

    out = d.read_samples(n)
    assert out.size == n
    # returned buffer is the reusable internal buffer
    assert out is d._buf


def test_hardware_full_frame_tuple_ret() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.SCANNING
    d._is_emulated = False

    n = 512
    d._stream = object()
    d._sdr = _FakeSDR("full_tuple", expected_n=n)

    out = d.read_samples(n)
    assert out.size == n


def test_hardware_partial_frame_is_discarded_and_counts_drop() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.SCANNING
    d._is_emulated = False

    n = 256
    d._stream = object()
    d._sdr = _FakeSDR("partial", expected_n=n)

    before = d.stats()["partial_drops"]
    out = d.read_samples(n)

    assert out.size == 0
    after = d.stats()
    assert after["partial_drops"] == before + 1
    assert after["partial_reads"] >= 1


def test_hardware_error_code_returns_empty_and_fastfail_increments() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.SCANNING
    d._is_emulated = False

    n = 256
    d._stream = object()
    d._sdr = _FakeSDR("error", expected_n=n)

    before = d.stats()["fastfail_reads"]
    out = d.read_samples(n)

    assert out.size == 0
    assert d.stats()["fastfail_reads"] == before + 1


def test_hardware_exception_returns_empty_and_fastfail_increments() -> None:
    d = SdrDriver(_mk_cfg())
    d.state = HalState.SCANNING
    d._is_emulated = False

    n = 256
    d._stream = object()
    d._sdr = _FakeSDR("raises", expected_n=n)

    before = d.stats()["fastfail_reads"]
    out = d.read_samples(n)

    assert out.size == 0
    assert d.stats()["fastfail_reads"] == before + 1


def test_connect_throttle_prevents_connect_storm(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Перевіряємо throttling незалежно від SoapySDR:
    якщо connect() викликають часто, він не повинен виконувати "дорогу роботу"
    частіше ніж раз за _CONNECT_RETRY_MIN_INTERVAL_S.

    У нашому середовищі SoapySDR часто відсутній, тому connect() йде в emulator-path.
    Тестуємо, що _setup_emulator НЕ викликається вдруге всередині throttle window.
    """
    import src.hal.sdr as sdr_mod

    d = SdrDriver(_mk_cfg())

    # Гарантуємо шлях емулювання (типово так і є в CI/dev без SoapySDR)
    d._is_emulated = True

    # Control monotonic time
    t = {"now": 100.0}

    def _mono() -> float:
        return t["now"]

    monkeypatch.setattr(sdr_mod.time, "monotonic", _mono)

    # Spy on _setup_emulator
    calls = {"n": 0}

    def _spy_setup_emulator(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        # не викликаємо реальний метод — нам важлива тільки кількість викликів

    monkeypatch.setattr(d, "_setup_emulator", _spy_setup_emulator)

    # 1) First call -> should call emulator setup once
    d.connect()
    assert calls["n"] == 1

    # 2) Within throttle window -> should be throttled (no extra calls)
    t["now"] = 100.5
    d.connect()
    assert calls["n"] == 1

    # 3) After throttle window -> should call again
    t["now"] = 103.0
    d.connect()
    assert calls["n"] == 2