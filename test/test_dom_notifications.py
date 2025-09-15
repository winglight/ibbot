import asyncio
import threading
import time
import pytest

from src.dom_service import get_dom_service


class DummyNotifier:
    def __init__(self):
        self.calls = []

    async def send(self, message: str, event_type: str = "general", **kwargs):
        self.calls.append((message, event_type))


class FakeProcessor:
    def __init__(self):
        self.last_signals = {"long": True, "short": False}
        self.last_events = ["buy_absorb"]
        self.last_metrics = {}

    def on_depth_update(self, data):
        pass


def test_dom_notifier_and_alerts():
    dom_service = get_dom_service()
    dom_service.processor = FakeProcessor()
    dom_service.notifier = DummyNotifier()
    dom_service._alerts.clear()
    dom_service._last_signal = {"long": False, "short": False}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dom_service._loop = loop
    async def run_update():
        dom_service._on_depth_update({})
        await asyncio.sleep(0)

    loop.run_until_complete(run_update())

    assert dom_service.notifier.calls
    msg, ev_type = dom_service.notifier.calls[0]
    assert ev_type == "dom_long"
    assert "DOM signal" in msg

    alerts = dom_service.list_alerts()
    assert alerts and alerts[0]["event"] == "buy_absorb"
    aid = alerts[0]["id"]
    assert dom_service.ack_alert(aid)
    assert dom_service.list_alerts() == []

    dom_service._alerts.append({"id": 99, "event": "x", "timestamp": time.time() - 400})
    dom_service._perform_cleanup()
    assert not dom_service._alerts


def test_dom_notifier_threadsafe():
    dom_service = get_dom_service()
    dom_service.processor = FakeProcessor()
    dom_service.notifier = DummyNotifier()
    dom_service._alerts.clear()
    dom_service._last_signal = {"long": False, "short": False}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    dom_service._loop = loop

    def call_update():
        dom_service._on_depth_update({})

    t = threading.Thread(target=call_update)
    t.start()
    t.join()
    loop.run_until_complete(asyncio.sleep(0))

    assert dom_service.notifier.calls
