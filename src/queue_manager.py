"""Lightweight Redis-backed queue proxy used for compatibility."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Dict, Mapping

from loguru import logger

from src.ibbot.redis import get_redis_client

queue_logger = logger.bind(module="queue_manager", request_id="-")


class QueueManager:
    """Simplified queue manager that pushes tasks to Redis lists."""

    def __init__(self, *, queue_key: str | None = None) -> None:
        self.queue_key = queue_key or os.getenv("STRATEGY_QUEUE_KEY", "list:queue:strategy")
        self.is_running: bool = False

    # ------------------------------------------------------------------
    def set_dependencies(self, **_: Any) -> None:  # pragma: no cover - compatibility hook
        return None

    def start(self) -> None:  # pragma: no cover - compatibility hook
        self.is_running = True

    def stop(self) -> None:  # pragma: no cover - compatibility hook
        self.is_running = False

    async def start_worker(self) -> None:
        self.is_running = True

    async def stop_worker(self) -> None:
        self.is_running = False

    # ------------------------------------------------------------------
    def add_task(self, task_type: str, payload: Mapping[str, Any], max_retries: int = 3) -> str:
        task_id = uuid.uuid4().hex
        task = {
            "task_id": task_id,
            "task_type": task_type,
            "payload": dict(payload),
            "max_retries": int(max_retries),
        }
        queue_logger.info("Queueing task", task_type=task_type, task_id=task_id)
        self._schedule_enqueue(task)
        return task_id

    def _schedule_enqueue(self, task: Dict[str, Any]) -> None:
        async def _runner() -> None:
            try:
                client = await get_redis_client()
                await client.rpush(self.queue_key, json.dumps(task))
            except Exception as exc:  # pragma: no cover - defensive guard
                queue_logger.warning("Failed to enqueue task", task_id=task["task_id"], error=str(exc))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_runner())
        else:
            loop.create_task(_runner())

    async def fetch_queue_status(self) -> Dict[str, Any]:
        try:
            client = await get_redis_client()
            pending = await client.llen(self.queue_key)
        except Exception as exc:  # pragma: no cover - defensive guard
            queue_logger.debug("Failed to fetch queue length", error=str(exc))
            pending = None
        return {
            "is_running": self.is_running,
            "queue_key": self.queue_key,
            "pending_count": pending,
        }

    def get_queue_status(self) -> Dict[str, Any]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.fetch_queue_status())
        queue_logger.debug("get_queue_status called within running loop; returning cached state")
        return {
            "is_running": self.is_running,
            "queue_key": self.queue_key,
            "pending_count": None,
        }
