"""FastAPI dependency helpers for accessing the core service container."""

from __future__ import annotations

from fastapi import Depends, Request

import core
from .container import CoreContainer


def get_core_container(request: Request) -> CoreContainer:
    """Return the application core container from FastAPI state or globals."""

    container = getattr(request.app.state, "core", None)
    if container is None:
        container = core.get_container()
    if container is None:
        raise RuntimeError("Core container has not been initialised")
    return container


def get_websocket_manager(
    container: CoreContainer = Depends(get_core_container),
):
    return container.websocket_manager

