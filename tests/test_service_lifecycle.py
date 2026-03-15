import time
import multiprocessing as mp
import pytest
from typing import Any, cast

from src.core.service import BaseService

mp_ctx = mp.get_context("spawn")  # stable across platforms

class StrictMockWorker(BaseService):
    def __init__(self, name: str, shared_list: Any):
        super().__init__(name, loop_sleep_s=0.01)
        self.shared_list = shared_list

    def setup(self) -> None:
        self.shared_list.append("setup")

    def execute(self) -> None:
        self.shared_list.append("loop")

    def teardown(self) -> None:
        self.shared_list.append("teardown")


class FailingWorker(BaseService):
    def __init__(self, name: str, counter: Any):
        super().__init__(name, error_sleep_s=0.05)
        self.counter = counter

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def execute(self) -> None:
        lock = self.counter.get_lock()
        assert lock is not None
        with lock:
            self.counter.value += 1
            cur_value = self.counter.value

        if cur_value < 3:
            raise ValueError("Boom!")

        self.request_stop()


@pytest.mark.timeout(10)
def test_service_lifecycle_graceful_stop() -> None:
        manager = mp.Manager()
        shared_list = manager.list()
        worker = StrictMockWorker("strict_worker", shared_list)

        worker.start()
        time.sleep(0.1)

        assert "setup" in shared_list
        assert "loop" in shared_list

        worker.request_stop()
        worker.join(timeout=2.0)

        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=1.0)
            pytest.fail("Worker did not stop gracefully after request_stop()")

        assert "teardown" in shared_list
        assert worker.exitcode == 0


@pytest.mark.timeout(10)
def test_service_error_resilience() -> None:
    counter = cast(Any, mp_ctx.Value("i", 0))
    worker = FailingWorker("crashy", counter)

    worker.start()
    worker.join(timeout=3.0)

    if worker.is_alive():
        worker.terminate()
        worker.join(timeout=1.0)
        pytest.fail("Worker stuck in loop/error state")

    assert counter.value >= 3
    assert worker.exitcode == 0