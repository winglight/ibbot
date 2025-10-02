"""Thin proxy that forwards notifications to the notification service."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, MutableMapping, Optional

from loguru import logger

from src.routes.service_clients import (
    NotificationServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)

notification_logger = logger.bind(module="notification_manager", request_id="-")


class NotificationManager:
    """Compatibility facade that delegates to ``NotificationServiceClient``."""

    def __init__(
        self,
        *,
        client: NotificationServiceClient | None = None,
        default_channel: str = "general",
    ) -> None:
        self._client = client or NotificationServiceClient()
        self.default_channel = default_channel
        self._websocket_manager: Any | None = None

    # ------------------------------------------------------------------
    def set_dependencies(self, *, websocket_manager: Any | None = None, **_: Any) -> None:
        """Retain compatibility with the legacy signature."""

        if websocket_manager is not None:
            self._websocket_manager = websocket_manager

    # ------------------------------------------------------------------
    async def _dispatch(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        try:
            return await self._client.dispatch(payload)
        except ServiceUnavailableError as exc:
            notification_logger.warning("Notification service unavailable", error=str(exc))
        except ServiceResponseError as exc:
            detail = exc.detail or "notification service rejected payload"
            notification_logger.warning("Notification service error", detail=detail)
        except ServiceClientError as exc:
            notification_logger.warning("Notification request failed", error=str(exc))
        return None

    async def send_notification(
        self,
        *,
        message: str,
        channel: str | None = None,
        event_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        payload: MutableMapping[str, Any] = {
            "message": message,
            "channel": channel or self.default_channel,
        }
        if event_type:
            payload["event_type"] = event_type
        if metadata:
            payload["metadata"] = dict(metadata)
        payload.update(extra)
        await self._dispatch(payload)

    async def send_risk_notification(
        self,
        symbol: str,
        message: str,
        *,
        risk_type: str = "general",
        **extra: Any,
    ) -> None:
        await self.send_notification(
            message=message,
            channel="risk",
            event_type=risk_type,
            metadata={"symbol": symbol, **extra},
        )

    async def send_stop_loss_notification(
        self,
        symbol: str,
        message: str,
        *,
        order_id: str | None = None,
        **extra: Any,
    ) -> None:
        metadata: MutableMapping[str, Any] = {"symbol": symbol}
        if order_id:
            metadata["order_id"] = order_id
        metadata.update(extra)
        await self.send_notification(
            message=message,
            channel="risk",
            event_type="stop_loss",
            metadata=metadata,
        )

    async def send_auto_close_notification(
        self,
        symbol: str,
        message: str,
        *,
        reason: str | None = None,
        **extra: Any,
    ) -> None:
        metadata: MutableMapping[str, Any] = {"symbol": symbol}
        if reason:
            metadata["reason"] = reason
        metadata.update(extra)
        await self.send_notification(
            message=message,
            channel="risk",
            event_type="auto_close",
            metadata=metadata,
        )

    async def send_daily_summary_notification(
        self,
        account_info: Mapping[str, Any],
        positions: list[Mapping[str, Any]] | None = None,
    ) -> None:
        payload = {
            "message": "Daily risk summary",
            "channel": "risk",
            "event_type": "daily_summary",
            "metadata": {
                "account": dict(account_info),
                "positions": list(positions or []),
            },
        }
        await self._dispatch(payload)

    # ------------------------------------------------------------------
    def notify(self, message: str, *, channel: str | None = None, **extra: Any) -> None:
        """Compatibility helper that schedules an asynchronous send."""

        async def _runner() -> None:
            await self.send_notification(message=message, channel=channel, **extra)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_runner())
        else:
            loop.create_task(_runner())
