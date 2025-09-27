"""Persistence backends for the unified data layer."""

from __future__ import annotations

from typing import Any, Dict

from .event_bus import EventEnvelope

__all__ = [
    "PersistenceBackend",
    "NoopPersistenceBackend",
]


class PersistenceBackend:
    """Abstract interface for persisting market data events."""

    def write_event(self, envelope: EventEnvelope) -> None:
        raise NotImplementedError


class NoopPersistenceBackend(PersistenceBackend):
    """Persistence backend that simply drops events (default for tests)."""

    def write_event(self, envelope: EventEnvelope) -> None:  # pragma: no cover - trivial
        return None

