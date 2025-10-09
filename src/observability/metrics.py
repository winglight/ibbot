from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, Response

try:  # pragma: no cover - exercised when prometheus_client is available
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        generate_latest,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback for offline environments
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class CollectorRegistry:
        def __init__(self) -> None:
            self._metrics: list["_BaseMetric"] = []

        def register(self, metric: "_BaseMetric") -> None:
            self._metrics.append(metric)

        def collect(self) -> list["_BaseMetric"]:
            return list(self._metrics)

    class _BaseMetric:
        _type = "gauge"

        def __init__(
            self,
            name: str,
            documentation: str,
            *,
            labelnames: tuple[str, ...] = (),
            registry: CollectorRegistry | None = None,
        ) -> None:
            self._name = name
            self._documentation = documentation
            self._labelnames = labelnames
            self._metrics: dict[tuple[str, ...], float] = {}
            if registry is not None:
                registry.register(self)

        def labels(self, **labels: str) -> "_MetricHandle":
            key = tuple(labels.get(name, "") for name in self._labelnames)
            return _MetricHandle(self, key)

    class _MetricHandle:
        def __init__(self, metric: "_BaseMetric", key: tuple[str, ...]) -> None:
            self._metric = metric
            self._key = key

        def inc(self, amount: float = 1.0) -> None:
            current = self._metric._metrics.get(self._key, 0.0)
            self._metric._metrics[self._key] = current + amount

        def set(self, value: float) -> None:
            self._metric._metrics[self._key] = float(value)

    class Counter(_BaseMetric):
        _type = "counter"

    class Gauge(_BaseMetric):
        _type = "gauge"

    def generate_latest(registry: CollectorRegistry) -> bytes:
        lines: list[str] = []
        for metric in registry.collect():
            lines.append(f"# HELP {metric._name} {metric._documentation}")
            lines.append(f"# TYPE {metric._name} {metric._type}")
            for labels, value in metric._metrics.items():
                if metric._labelnames:
                    serialized = ",".join(
                        f'{name}="{label}"'
                        for name, label in zip(metric._labelnames, labels, strict=False)
                    )
                    lines.append(f"{metric._name}{{{serialized}}} {value}")
                else:
                    lines.append(f"{metric._name} {value}")
        return "\n".join(lines).encode("utf-8")

_REGISTRY = CollectorRegistry()

_REDIS_PUBLISH_FAILURES = Counter(
    "ibbot_redis_publish_failures_total",
    "Number of Redis publish attempts that resulted in an error",
    labelnames=("service",),
    registry=_REGISTRY,
)

_IB_RECONNECTS = Counter(
    "ibbot_ib_client_reconnects_total",
    "Interactive Brokers client connect/reconnect invocations",
    labelnames=("service",),
    registry=_REGISTRY,
)

_ACTIVE_SUBSCRIPTIONS = Gauge(
    "ibbot_active_subscriptions",
    "Current number of active subscriptions exposed by a service",
    labelnames=("service", "channel"),
    registry=_REGISTRY,
)


def get_metrics_registry() -> CollectorRegistry:
    """Return the process-wide Prometheus registry."""

    return _REGISTRY


def record_redis_publish_failure(*, service: str) -> None:
    """Increment the Redis failure counter for ``service``."""

    _REDIS_PUBLISH_FAILURES.labels(service=service).inc()


def record_ib_reconnect(*, service: str) -> None:
    """Record a successful IB client connection for ``service``."""

    _IB_RECONNECTS.labels(service=service).inc()


def set_subscription_gauge(*, service: str, channel: str, count: int) -> None:
    """Update the subscription gauge for ``service``/``channel``."""

    _ACTIVE_SUBSCRIPTIONS.labels(service=service, channel=channel).set(max(count, 0))


def reset_metrics() -> None:
    """Reset all registered metric series.

    This helper is primarily used by unit tests to provide deterministic
    assertions across multiple runs.
    """

    for metric in (_REDIS_PUBLISH_FAILURES, _IB_RECONNECTS, _ACTIVE_SUBSCRIPTIONS):
        metric._metrics.clear()  # type: ignore[attr-defined]


def build_metrics_router(*, prefixes: Iterable[str] | None = None) -> APIRouter:
    """Return a router exposing one or more ``/metrics`` endpoints."""

    router = APIRouter()
    paths = tuple(prefixes) if prefixes else ("/metrics",)

    async def _metrics_endpoint() -> Response:  # type: ignore[func-returns-value]
        payload = generate_latest(_REGISTRY)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    for path in paths:
        normalized = path if path.startswith("/") else f"/{path}"
        router.add_api_route(
            normalized,
            _metrics_endpoint,
            methods=["GET"],
            include_in_schema=False,
        )

    return router


__all__ = [
    "build_metrics_router",
    "get_metrics_registry",
    "record_ib_reconnect",
    "record_redis_publish_failure",
    "reset_metrics",
    "set_subscription_gauge",
]
