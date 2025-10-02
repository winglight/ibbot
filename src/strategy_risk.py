"""Helpers for strategy components to consume risk service data."""

from __future__ import annotations

from typing import Any, Mapping

from src.risk_manager import RiskManager

__all__ = ["StrategyRiskManager"]


class StrategyRiskManager(RiskManager):
    """Lightweight facade tailored for strategy components."""

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
            history = payload.get("history") or payload.get("data")
            if isinstance(history, list):
                return [dict(item) if isinstance(item, Mapping) else item for item in history]
        return []

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
