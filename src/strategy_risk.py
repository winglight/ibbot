"""Helpers for strategy components to consume risk service data."""

from __future__ import annotations

from typing import Any, Mapping

import json

from src.routes.service_clients import RiskServiceClient

from src.risk_manager import RiskManager

__all__ = ["StrategyRiskManager"]


class StrategyRiskManager(RiskManager):
    """Lightweight facade tailored for strategy components."""

    def __init__(
        self,
        *,
        client: RiskServiceClient | None = None,
        redis_client: Any | None = None,
        settings_prefix: str = "json:strategy",
    ) -> None:
        super().__init__(client=client)
        self._redis = redis_client
        self._settings_prefix = settings_prefix.rstrip(":")

    async def load_rules(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of symbol to rule payloads."""

        payload = await self.list_rules()
        return self._normalise_rules_payload(payload)

    async def load_rule(self, symbol: str) -> dict[str, Any] | None:
        """Return a single rule payload for *symbol* if available."""

        key = (symbol or "").strip().upper()
        if not key:
            return None
        rules = await self.load_rules()
        return rules.get(key)

    async def load_config(self) -> dict[str, Any]:
        """Return the flattened risk configuration payload."""

        payload = await self.get_config()
        if isinstance(payload, Mapping):
            config = payload.get("config")
            if isinstance(config, Mapping):
                return {str(k): v for k, v in config.items()}
            return {str(k): v for k, v in payload.items() if k != "success"}
        return {}

    async def load_rules_history(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return the rule change history for the provided filters."""

        payload = await self.fetch_rules_history(symbol=symbol, limit=limit)
        if isinstance(payload, Mapping):
            history = (
                payload.get("history")
                or payload.get("items")
                or payload.get("data")
            )
            if isinstance(history, list):
                return [dict(item) if isinstance(item, Mapping) else item for item in history]
        return []

    # ------------------------------------------------------------------
    def _settings_key(self, strategy: str) -> str:
        name = (strategy or "").strip()
        return f"{self._settings_prefix}:{name}:risk-settings"

    async def load_strategy_settings(self, strategy: str) -> dict[str, Any]:
        """Return persisted risk settings for *strategy* if available."""

        if not self._redis:
            return {}
        key = self._settings_key(strategy)
        try:
            raw = await self._redis.get(key)
        except Exception:  # pragma: no cover - defensive network guard
            return {}
        payload: Mapping[str, Any] | None = None
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, Mapping):
                payload = decoded
        elif isinstance(raw, Mapping):
            payload = raw
        if not payload:
            return {}
        return {str(k): v for k, v in payload.items()}

    async def persist_strategy_settings(
        self, strategy: str, payload: Mapping[str, Any]
    ) -> None:
        """Persist risk settings for *strategy* to the backing store."""

        if not self._redis:
            return
        key = self._settings_key(strategy)
        try:
            await self._redis.set(key, json.dumps(dict(payload), ensure_ascii=False))
        except Exception:  # pragma: no cover - defensive network guard
            logger.warning("Failed to persist strategy risk settings", strategy=strategy)

    async def delete_strategy_settings(self, strategy: str) -> None:
        """Remove risk settings associated with *strategy*."""

        if not self._redis:
            return
        key = self._settings_key(strategy)
        try:
            await self._redis.delete(key)
        except Exception:  # pragma: no cover - defensive network guard
            logger.debug("Failed to delete strategy risk settings", strategy=strategy)

    async def rename_strategy_settings(self, source: str, target: str) -> None:
        """Move persisted settings from *source* to *target*."""

        if not self._redis:
            return
        source_key = self._settings_key(source)
        target_key = self._settings_key(target)
        try:
            payload = await self._redis.get(source_key)
        except Exception:  # pragma: no cover - defensive guard
            payload = None
        if payload is None:
            return
        try:
            await self._redis.set(target_key, payload)
            await self._redis.delete(source_key)
        except Exception:  # pragma: no cover - defensive guard
            logger.debug(
                "Failed to rename strategy risk settings",
                source=source,
                target=target,
            )

    def _normalise_rules_payload(self, payload: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, Mapping):
            return {}
        rules = payload.get("rules")
        if not isinstance(rules, Mapping):
            return {}
        normalised: dict[str, dict[str, Any]] = {}
        for symbol, value in rules.items():
            if not isinstance(symbol, str) or not symbol.strip():
                continue
            if isinstance(value, Mapping):
                normalised[symbol.upper()] = {str(k): v for k, v in value.items()}
        return normalised
