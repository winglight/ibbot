from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import src.ibbot.influx as influx


@pytest.fixture(autouse=True)
def reset_influx_state() -> None:
    influx.reset_influx_state()
    yield
    influx.reset_influx_state()


def test_client_initialisation_and_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []

    class DummyClient:
        def __init__(self, *, url: str, token: str | None, org: str | None, timeout: int) -> None:
            self.url = url
            self.token = token
            self.org = org
            self.timeout = timeout
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def write_api(self, *, write_options: object | None = None) -> Mock:
            return Mock(name="write_api")

        def query_api(self) -> Mock:
            return Mock(name="query_api")

        def buckets_api(self) -> Mock:
            return Mock(name="buckets_api")

    def factory(*, url: str, token: str | None, org: str | None, timeout: int) -> DummyClient:
        client = DummyClient(url=url, token=token, org=org, timeout=timeout)
        created.append(client)
        return client

    monkeypatch.setattr(influx, "InfluxDBClient", factory)
    monkeypatch.setenv("IBBOT_INFLUX_URL", "http://example:8086")
    monkeypatch.setenv("IBBOT_INFLUX_TOKEN", "token-123")
    monkeypatch.setenv("IBBOT_INFLUX_ORG", "org-1")
    monkeypatch.setenv("IBBOT_INFLUX_BUCKET", "bucket-1")

    client_a = influx.get_influx_client()
    client_b = influx.get_influx_client()
    assert client_a is client_b
    assert created and created[0].url == "http://example:8086"
    assert created[0].token == "token-123"

    influx.close_influx_client()
    assert created[0].closed is True


def test_point_builders_produce_expected_structure() -> None:
    now = datetime(2024, 1, 1, 12, 30, 15)

    order_point = influx.build_order_point(
        order_id="ord-1",
        symbol="AAPL",
        side="BUY",
        quantity=100,
        price=150.5,
        status="filled",
        exchange="NASDAQ",
        timestamp=now,
        extra_fields={"fee": 1.25},
        extra_tags={"strategy": "alpha"},
    )
    assert order_point.measurement == "orders"
    assert order_point.tags["strategy"] == "alpha"
    assert order_point.fields["fee"] == 1.25

    quote_point = influx.build_quote_point(
        symbol="AAPL",
        bid=150.2,
        ask=150.4,
        bid_size=50,
        ask_size=60,
        source="feed-x",
        timestamp=now,
        extra_fields={"mid": 150.3},
    )
    assert quote_point.measurement == "quotes"
    assert quote_point.tags["source"] == "feed-x"
    assert quote_point.fields["mid"] == 150.3

    dom_point = influx.build_dom_point(
        symbol="AAPL",
        level=1,
        side="bid",
        price=150.2,
        size=75,
        timestamp=now,
        extra_tags={"venue": "ARCA"},
    )
    assert dom_point.measurement == "dom"
    assert dom_point.tags["level"] == "1"
    assert dom_point.fields["price"] == 150.2

    metric_point = influx.build_service_metric_point(
        service="order-router",
        metric="latency",
        value=42.5,
        host="worker-1",
        status="ok",
        timestamp=now,
        extra_fields={"p95": 55.0},
    )
    assert metric_point.measurement == "service_metrics"
    assert metric_point.tags["host"] == "worker-1"
    assert metric_point.fields["p95"] == 55.0

    for point in (order_point, quote_point, dom_point, metric_point):
        payload = point.as_dict()
        assert payload["measurement"]
        assert isinstance(payload["fields"], dict)
        assert payload.get("time") == now


def test_write_points_uses_client_and_updates_statistics() -> None:
    influx.reset_write_statistics()
    write_api = Mock()
    client = Mock()
    client.write_api.return_value = write_api

    order_point = influx.build_order_point(
        order_id="ord-1",
        symbol="AAPL",
        side="BUY",
        quantity=10,
        price=150.0,
        status="submitted",
    )
    raw_point = {"measurement": "custom", "fields": {"value": 1}}

    count = influx.write_points(
        [order_point, raw_point],
        bucket="bucket-x",
        org="org-x",
        client=client,
        write_precision="ms",
    )

    assert count == 2
    client.write_api.assert_called_once()
    write_api.write.assert_called_once()
    kwargs = write_api.write.call_args.kwargs
    assert kwargs["bucket"] == "bucket-x"
    assert kwargs["org"] == "org-x"
    assert kwargs["write_precision"] == "ms"
    records = kwargs["record"]
    assert [record["measurement"] for record in records] == ["orders", "custom"]

    stats = influx.get_write_statistics()
    assert stats.batches == 1
    assert stats.points == 2
    assert stats.points_per_second >= 0.0
    assert stats.last_error is None


def test_query_and_retention_policy_management(monkeypatch: pytest.MonkeyPatch) -> None:
    influx.configure_influx(org="default-org", bucket="default-bucket")

    client = Mock()
    query_api = Mock()
    query_api.query.side_effect = [["custom"], ["default"]]
    buckets_api = Mock()

    client.query_api.return_value = query_api
    client.buckets_api.return_value = buckets_api

    existing_bucket = SimpleNamespace(retention_rules=[SimpleNamespace(every_seconds=3600)])
    buckets_api.find_bucket_by_name.side_effect = [
        None,
        existing_bucket,
        existing_bucket,
        existing_bucket,
    ]

    result_custom = influx.query_data("from(bucket: \"x\")", org="custom-org", client=client)
    assert result_custom == ["custom"]
    query_api.query.assert_called_once_with(org="custom-org", query="from(bucket: \"x\")")

    query_api.query.reset_mock()
    result_default = influx.query_data("buckets()", client=client)
    assert result_default == ["default"]
    query_api.query.assert_called_once_with(org="default-org", query="buckets()")

    influx.ensure_retention_policy("bucket-a", retention_hours=24, org="custom-org", client=client)
    assert buckets_api.create_bucket.called
    create_kwargs = buckets_api.create_bucket.call_args.kwargs
    assert create_kwargs["bucket_name"] == "bucket-a"
    assert create_kwargs["org"] == "custom-org"
    assert create_kwargs["retention_rules"] == [{"every_seconds": 24 * 3600}]

    influx.ensure_retention_policy("bucket-a", retention_hours=2, client=client)
    buckets_api.update_bucket.assert_called_once_with(existing_bucket)
    assert existing_bucket.retention_rules == [{"every_seconds": 2 * 3600}]

    buckets_api.update_bucket.reset_mock()
    influx.ensure_retention_policy("bucket-a", retention_hours=2, client=client)
    buckets_api.update_bucket.assert_not_called()

    influx.ensure_retention_policy("bucket-a", retention_hours=None, client=client)
    buckets_api.update_bucket.assert_not_called()
