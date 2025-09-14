import types
from ib_insync import Event

from src.depth_subscriber import DepthSubscriber


class DummyTicker:
    def __init__(self):
        self.domBids = []
        self.domAsks = []
        self.updateEvent = Event()


class DummyIB:
    def __init__(self):
        self.req_calls = []
        self.cancel_calls = []

    def reqMktDepth(self, contract, numRows=5, isSmartDepth=False):
        self.req_calls.append((contract, numRows))
        return DummyTicker()

    def cancelMktDepth(self, contract):
        self.cancel_calls.append(contract)


class DummyConnectionManager:
    def __init__(self):
        self.connected = True
        self.ib = DummyIB()

    def is_connected(self):
        return self.connected

    def get_ib_instance(self):
        return self.ib

    def run_in_connection_thread(self, func, *args, **kwargs):
        return func(*args, **kwargs)


class DummyIBKRClient:
    def get_stock_contract(self, symbol, exchange="SMART", currency="USD"):
        return types.SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)

    def get_future_contract(self, symbol, exchange="CME", currency="USD"):
        return types.SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)


def test_start_and_stop_stream():
    cm = DummyConnectionManager()
    client = DummyIBKRClient()
    sub = DepthSubscriber(cm)
    sub.set_ibkr_client(client)

    received = []
    assert sub.start_depth_stream("AAPL", lambda data: received.append(data))
    assert "AAPL" in sub._streams

    ticker = sub._streams["AAPL"].ticker
    ticker.domBids = [types.SimpleNamespace(price=100.0, size=10)]
    ticker.domAsks = [types.SimpleNamespace(price=100.5, size=12)]
    ticker.updateEvent.emit()
    assert received and received[0]["bids"][0] == (100.0, 10)

    assert sub.stop_depth_stream("AAPL")
    assert "AAPL" not in sub._streams
    assert cm.ib.cancel_calls


def test_start_stream_requires_connection():
    cm = DummyConnectionManager()
    cm.connected = False
    sub = DepthSubscriber(cm)
    assert not sub.start_depth_stream("AAPL", lambda data: None)
