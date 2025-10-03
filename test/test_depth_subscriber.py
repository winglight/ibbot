import pytest

from src.depth_subscriber import DepthSubscriber


class _StubConnectionManager:
    def __init__(self) -> None:
        self._connected = True
        self.timeouts: list[float | None] = []

    def is_connected(self) -> bool:
        return self._connected

    def run_in_connection_thread(self, func, *args, timeout=None, **kwargs):
        # Legacy pathway retained for completeness.
        return self.run_in_connection_thread_blocking(func, *args, timeout=timeout, **kwargs)

    def run_in_connection_thread_blocking(self, func, *args, timeout=None, **kwargs):
        # Record the timeout value that callers provide. Simulate a failure so
        # ``DepthSubscriber`` is forced to handle the timeout path.
        self.timeouts.append(timeout)
        return None

    def get_ib_instance(self):  # pragma: no cover - not exercised in this stub
        return None


def test_depth_subscriber_uses_connection_timeout():
    manager = _StubConnectionManager()
    subscriber = DepthSubscriber(manager)

    result = subscriber.start_depth_stream("MNQ", lambda data: None)

    assert result is False
    assert manager.timeouts, "run_in_connection_thread should be invoked"
    assert manager.timeouts[0] == pytest.approx(15.0)
