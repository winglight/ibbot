"""Helpers for strategy components to consume risk service data."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from loguru import logger

from src.routes.service_clients import (
    RiskServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)

__all__ = ["StrategyRiskManager"]


_risk_logger = logger.bind(module="strategy_risk_manager", request_id="-")


class StrategyRiskManager:
    """Lightweight facade tailored for strategy components.

    The manager now prefers Redis snapshots (``hash:risk:rules``) for active
    rule payloads so strategies see the same stop/limit levels as the risk
    engine. When the cache is unavailable it transparently falls back to the
    risk service HTTP client, preserving existing behaviour.
    """

    def __init__(
        self,
        *,
        client: RiskServiceClient | None = None,
        redis_client: Any | None = None,
        settings_prefix: str = "json:strategy",
        rules_hash_key: str = "hash:risk:rules",
        *,
        settings_ttl_seconds: int | None = None,
    ) -> None:
        self._client = client or RiskServiceClient()
        self._redis = redis_client
        self._settings_prefix = settings_prefix.rstrip(":")
        self._rules_hash_key = rules_hash_key
        default_ttl = int(os.getenv("STRATEGY_RISK_SETTINGS_TTL_SECONDS", "3600"))
        self._settings_ttl_seconds = settings_ttl_seconds or default_ttl

    async def _call(self, action: str, **kwargs: Any) -> Any:
        try:
            method = getattr(self._client, action)
        except AttributeError:
            raise AttributeError(f"RiskServiceClient has no action '{action}'")
        try:
            return await method(**kwargs)
        except ServiceUnavailableError as exc:
            _risk_logger.warning("Risk service unavailable", action=action, error=str(exc))
            raise
        except ServiceResponseError as exc:
            detail = exc.detail or "risk service error"
            _risk_logger.warning("Risk service error", action=action, detail=detail)
            raise
        except ServiceClientError as exc:
            _risk_logger.warning("Risk service request failed", action=action, error=str(exc))
            raise

    async def list_rules(self) -> Any:
        return await self._call("list_rules")

    async def create_rule(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("create_rule", payload=payload)

    async def update_rule(
        self,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        *,
        enabled: bool | None = None,
    ) -> Any:
        return await self._call(
            "update_rule", symbol=symbol, payload=payload, enabled=enabled
        )

    async def delete_rule(self, symbol: str) -> Any:
        return await self._call("delete_rule", symbol=symbol)

    async def get_config(self) -> Any:
        return await self._call("get_config")

    async def update_config(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("update_config", payload=payload)

    async def position_sizing(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("position_sizing", payload=payload)

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> Any:
        return await self._call(
            "fetch_sizing_history",
            symbol=symbol,
            since=since,
            limit=int(limit),
        )

    async def fetch_order_queue_status(self) -> Any:
        return await self._call("fetch_order_queue_status")

    async def fetch_config_rules(self) -> Any:
        return await self._call("fetch_config_rules")

    async def update_config_rules(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("update_config_rules", payload=payload)

    async def fetch_rules_history(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> Any:
        return await self._call(
            "fetch_rules_history",
            symbol=symbol,
            limit=int(limit),
        )

    async def load_rules(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of symbol to rule payloads."""

        cached = await self._load_rules_from_cache()
        if cached:
            return cached

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
            await self._redis.set(
                key,
                json.dumps(dict(payload), ensure_ascii=False),
                ex=self._settings_ttl_seconds,
            )
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
            await self._redis.set(target_key, payload, ex=self._settings_ttl_seconds)
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

    async def _load_rules_from_cache(self) -> dict[str, dict[str, Any]]:
        """Return rules stored in Redis without querying the risk service."""

        if not self._redis:
            return {}
        try:
            payload = await self._redis.hgetall(self._rules_hash_key)
        except Exception:  # pragma: no cover - defensive guard
            return {}
        if not payload:
            return {}

        rules: dict[str, dict[str, Any]] = {}
        for raw_symbol, raw_value in payload.items():
            symbol = self._decode_symbol(raw_symbol)
            if not symbol:
                continue
            decoded = self._decode_cached_rule(raw_value)
            if not isinstance(decoded, Mapping):
                continue
            rule_payload = {str(k): v for k, v in decoded.items()}
            symbol_key = str(rule_payload.get("symbol") or symbol).strip().upper()
            if not symbol_key:
                continue
            rule_payload["symbol"] = symbol_key
            rules[symbol_key] = rule_payload
        return rules

    @staticmethod
    def _decode_symbol(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore").strip().upper()
        return str(value).strip().upper()

    @staticmethod
    def _decode_cached_rule(raw: Any) -> Any:
        if raw is None:
            return None
        if isinstance(raw, Mapping):
            return {str(k): v for k, v in raw.items()}
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="ignore")
        else:
            text = str(raw)
        text = text.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        if isinstance(data, Mapping):
            return {str(k): v for k, v in data.items()}
        return data
