from typing import Optional

from .notification_manager import NotificationManager


class Notifier:
    """Simple wrapper around NotificationManager for DOM alerts."""

    def __init__(self, manager: Optional[NotificationManager] = None):
        self.manager = manager

    def set_manager(self, manager: NotificationManager) -> None:
        self.manager = manager

    async def send(self, message: str, event_type: str = "general", **kwargs) -> None:
        """Send notification asynchronously via underlying manager."""
        if not self.manager:
            return
        await self.manager.send_notification(message=message, event_type=event_type, **kwargs)
