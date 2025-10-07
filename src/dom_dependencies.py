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

# Note: connection_manager has been replaced by the new service architecture
# Note: DepthSubscriber import removed - deprecated in favor of new service architecture
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
# Note: depth_subscriber is deprecated in favor of the new data layer architecture
depth_subscriber = None  # Will be lazily initialized if needed for backward compatibility


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

