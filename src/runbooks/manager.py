"""Runbook manager keeping track of available operational workflows."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from .websocket_remediation import WebsocketRemediationRunbook, serialize_runbook_state


class RunbookManager:
    """Registry for runbooks and convenience helpers for API usage."""

    def __init__(self) -> None:
        self._runbooks: Dict[str, WebsocketRemediationRunbook] = {
            WebsocketRemediationRunbook.identifier: WebsocketRemediationRunbook()
        }
        self._lock = asyncio.Lock()

    async def list_runbooks(self) -> List[Dict[str, Any]]:
        """Return serialized state for all registered runbooks."""

        runbooks: List[Dict[str, Any]] = []
        async with self._lock:
            for runbook in self._runbooks.values():
                state = await runbook.get_state()
                runbooks.append(serialize_runbook_state(runbook, state))
        return runbooks

    async def get_runbook(self, runbook_id: str) -> Dict[str, Any]:
        """Return serialized state for the requested runbook."""

        async with self._lock:
            runbook = self._runbooks.get(runbook_id)
        if runbook is None:
            raise KeyError(runbook_id)
        state = await runbook.get_state()
        return serialize_runbook_state(runbook, state)

    async def start_runbook(self, runbook_id: str) -> Dict[str, Any]:
        """Start the specified runbook and return its updated state."""

        async with self._lock:
            runbook = self._runbooks.get(runbook_id)
        if runbook is None:
            raise KeyError(runbook_id)
        state = await runbook.start()
        return serialize_runbook_state(runbook, state)


runbook_manager = RunbookManager()

__all__ = ["RunbookManager", "runbook_manager"]

