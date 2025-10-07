"""Shared DOM-related singletons used across the application.

This module provides a single location where the DOM configuration,
processor and subscriber instances are created.  Keeping these
initialisations in their own module allows other modules (notably
``core`` and ``src.dom_service``) to import the shared instances without
triggering circular import issues.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from loguru import logger

from src.dom_processor import DOMProcessor
from src.redis_depth_subscriber import RedisDomSubscriber

try:  # depth subscriber is optional when using Redis backend
    from src.depth_subscriber import DepthSubscriber
except Exception:  # pragma: no cover - fallback when module unavailable
    DepthSubscriber = None  # type: ignore[misc, assignment]


@dataclass
class DOMConfig:
    """Configuration used by the DOM processor and related services."""

    window: int = 10000
    max_levels: int = 30
    trend_thresholds: List[float] = field(
        default_factory=lambda: [0.5, 0.7, 0.85, 0.95]
    )


dom_config = DOMConfig()

# Shared instances that other modules rely on
dom_processor = DOMProcessor(window=dom_config.window, max_levels=dom_config.max_levels)


def _build_depth_subscriber():
    backend = (os.getenv("DOM_SUBSCRIBER_BACKEND") or "auto").strip().lower()
    prefer_redis = backend in {"auto", "redis", ""}
    if os.getenv("PYTEST_CURRENT_TEST") and backend in {"auto", ""}:
        prefer_redis = False

    if prefer_redis:
        try:
            subscriber = RedisDomSubscriber()
        except Exception as exc:
            logger.warning("Failed to initialise RedisDomSubscriber: {}", exc)
            if backend == "redis":
                raise
        else:
            logger.debug("Using RedisDomSubscriber for DOM depth updates")
            return subscriber

    if DepthSubscriber is None:
        raise RuntimeError("DepthSubscriber unavailable and Redis backend disabled")

    from src.connection_manager import connection_manager  # local import to avoid eager dependency

    logger.debug("Falling back to legacy DepthSubscriber with connection manager")
    return DepthSubscriber(connection_manager)


depth_subscriber = _build_depth_subscriber()


def get_dom_processor() -> DOMProcessor:
    """Return the shared DOM processor instance."""

    return dom_processor


def set_dom_processor(processor: DOMProcessor) -> None:
    """Update the shared DOM processor instance."""

    global dom_processor
    dom_processor = processor


__all__ = [
    "DOMConfig",
    "dom_config",
    "dom_processor",
    "depth_subscriber",
    "get_dom_processor",
    "set_dom_processor",
]

