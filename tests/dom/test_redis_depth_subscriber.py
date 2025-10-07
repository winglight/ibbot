import json
import time
from typing import Any, Dict, List

try:  # pragma: no cover - exercised in test environments without fakeredis
    import fakeredis  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback for offline environments
    from tests.helpers.fakeredis_stub import fakeredis

from src.redis_depth_subscriber import RedisDomSubscriber


def _build_fake_factory(server: fakeredis.FakeServer):
    def _factory(_url: str):
        return fakeredis.FakeRedis(server=server, decode_responses=True)

    return _factory


def _publish_dom(server: fakeredis.FakeServer, symbol: str, bids: List[Dict[str, Any]], asks: List[Dict[str, Any]]):
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    payload = json.dumps({
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "ts_ns": time.time_ns(),
    })
    client.publish(f"pubsub:ib:dom:{symbol}", payload)


def test_subscriber_receives_dom_updates():
    server = fakeredis.FakeServer()
    factory = _build_fake_factory(server)
    subscriber = RedisDomSubscriber(redis_url="redis://fake/0", client_factory=factory)

    received: List[Dict[str, Any]] = []

    assert subscriber.start_depth_stream("ES", received.append)

    _publish_dom(
        server,
        "ES",
        bids=[{"price": 4300.25, "size": 5}, {"price": 4300.0, "size": 3}],
        asks=[{"price": 4300.5, "size": 4}],
    )

    deadline = time.time() + 2.0
    while time.time() < deadline and not received:
        time.sleep(0.01)

    assert received, "DOM update was not delivered"
    snapshot = received[-1]
    assert snapshot["bids"][0] == (4300.25, 5.0)
    assert snapshot["asks"][0] == (4300.5, 4.0)
    assert snapshot["timestamp"].endswith("Z")
    assert isinstance(snapshot["ts_ns"], int)

    assert subscriber.stop_depth_stream("ES")


def test_force_restart_replaces_existing_stream():
    server = fakeredis.FakeServer()
    created_clients: List[fakeredis.FakeRedis] = []

    def factory(_url: str):
        client = fakeredis.FakeRedis(server=server, decode_responses=True)
        created_clients.append(client)
        return client

    subscriber = RedisDomSubscriber(redis_url="redis://fake/0", client_factory=factory)

    events_first: List[Dict[str, Any]] = []
    events_second: List[Dict[str, Any]] = []

    assert subscriber.start_depth_stream("CL", events_first.append)
    assert subscriber.start_depth_stream("CL", events_second.append, force_restart=True)

    assert len(created_clients) == 2

    _publish_dom(server, "CL", bids=[{"price": 70.1, "size": 2}], asks=[])

    deadline = time.time() + 2.0
    while time.time() < deadline and not events_second:
        time.sleep(0.01)

    assert events_second
    assert not events_first, "Old callbacks should not receive updates after restart"

    subscriber.stop_depth_stream("CL")


def test_custom_factory_allows_init_without_redis(monkeypatch):
    import src.redis_depth_subscriber as module

    server = fakeredis.FakeServer()
    factory = _build_fake_factory(server)

    monkeypatch.setattr(module, "redis", None)

    subscriber = module.RedisDomSubscriber(
        redis_url="redis://fake/0", client_factory=factory
    )

    received: List[Dict[str, Any]] = []

    assert subscriber.start_depth_stream("GC", received.append)

    _publish_dom(
        server,
        "GC",
        bids=[{"price": 1950.5, "size": 1}],
        asks=[{"price": 1951.0, "size": 1}],
    )

    deadline = time.time() + 2.0
    while time.time() < deadline and not received:
        time.sleep(0.01)

    assert received
    subscriber.stop_depth_stream("GC")
