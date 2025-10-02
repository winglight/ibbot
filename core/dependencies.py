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


def get_notification_manager(
    container: CoreContainer = Depends(get_core_container),
):
    return container.notification_manager


def get_telegram_notifier(
    container: CoreContainer = Depends(get_core_container),
):
    return container.telegram_notifier


def get_risk_manager(
    container: CoreContainer = Depends(get_core_container),
):
    return container.risk_manager


def get_strategy_manager(
    container: CoreContainer = Depends(get_core_container),
):
    return container.strategy_manager


def get_queue_manager(
    container: CoreContainer = Depends(get_core_container),
):
    return container.queue_manager


def get_websocket_manager(
    container: CoreContainer = Depends(get_core_container),
):
    return container.websocket_manager


def get_account_snapshot_service(
    container: CoreContainer = Depends(get_core_container),
):
    return container.account_snapshot_service

