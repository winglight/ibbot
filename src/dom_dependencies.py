"""Shared DOM-related singletons used across the application.

This module provides a single location where the DOM configuration,
processor and subscriber instances are created.  Keeping these
initialisations in their own module allows other modules (notably
``core`` and ``src.dom_service``) to import the shared instances without
triggering circular import issues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.connection_manager import connection_manager
from src.depth_subscriber import DepthSubscriber
from src.dom_processor import DOMProcessor


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
depth_subscriber = DepthSubscriber(connection_manager)


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

