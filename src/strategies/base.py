"""Base classes for internal trading strategies."""

from __future__ import annotations

from typing import Optional, Any, Dict, Iterable, List

from loguru import logger


class BaseStrategy:
    """Common scaffolding for internal strategies."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description or name
        self.enabled: bool = True
        self.active: bool = False
        self.strategy_manager: Optional[Any] = None
        self.order_manager: Optional[Any] = None
        self.queue_manager: Optional[Any] = None
        self.risk_manager: Optional[Any] = None
        self.notification_manager: Optional[Any] = None
        # 统一使用 internal_strategy 模块名称，便于在前端日志面板按策略链路过滤
        self.logger = logger.bind(strategy=name, module="internal_strategy")
        self._parameter_definitions: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    def set_dependencies(
        self,
        *,
        strategy_manager: Optional[Any] = None,
        order_manager: Optional[Any] = None,
        queue_manager: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
        strategy_risk_manager: Optional[Any] = None,
        notification_manager: Optional[Any] = None,
    ) -> None:
        """Inject runtime dependencies."""

        if strategy_manager is not None:
            self.strategy_manager = strategy_manager
        if order_manager is not None:
            self.order_manager = order_manager
        if queue_manager is not None:
            self.queue_manager = queue_manager
        if risk_manager is not None:
            self.risk_manager = risk_manager
        if strategy_risk_manager is not None:
            setattr(self, "strategy_risk_manager", strategy_risk_manager)
        if notification_manager is not None:
            self.notification_manager = notification_manager

    # ------------------------------------------------------------------
    def start(self) -> bool:
        """Activate the strategy runtime."""
        if self.active:
            return False
        self.active = True
        self.logger.info("Strategy started")
        return True

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        """Deactivate the strategy runtime."""
        if not self.active:
            return False
        self.active = False
        self.logger.info("Strategy stopped")
        return True

    # ------------------------------------------------------------------
    def enable(self) -> None:
        """Enable and start the strategy."""
        self.enabled = True
        self.start()

    # ------------------------------------------------------------------
    def disable(self) -> None:
        """Disable and stop the strategy."""
        self.enabled = False
        self.stop()

    # ------------------------------------------------------------------
    def on_registered(self) -> None:
        """Hook called when the strategy is registered with the manager."""
        return None

    # ------------------------------------------------------------------
    def set_parameter_definitions(self, definitions: Dict[str, Dict[str, Any]]) -> None:
        """Register editable parameter definitions for the strategy."""

        cleaned: Dict[str, Dict[str, Any]] = {}
        for name, metadata in (definitions or {}).items():
            if not isinstance(metadata, dict):
                continue
            entry = dict(metadata)
            entry.setdefault("type", "float")
            cleaned[name] = entry
        self._parameter_definitions = cleaned

    # ------------------------------------------------------------------
    def get_parameter_definitions(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of the configured parameter definitions."""

        return {name: dict(meta) for name, meta in self._parameter_definitions.items()}

    # ------------------------------------------------------------------
    def describe_parameters(self) -> List[Dict[str, Any]]:
        """Return parameter definitions decorated with current values."""

        descriptions: List[Dict[str, Any]] = []
        for name, meta in self._parameter_definitions.items():
            entry = dict(meta)
            entry["name"] = name
            current = getattr(self, name, entry.get("default"))
            entry["value"] = current
            descriptions.append(entry)
        return descriptions

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and apply runtime parameter updates."""

        if not updates:
            return {}

        applied: Dict[str, Any] = {}
        definitions = self._parameter_definitions
        for key, raw_value in updates.items():
            if key not in definitions:
                raise ValueError(f"Parameter '{key}' is not configurable for {self.name}")
            definition = definitions[key]
            coerced = self._coerce_parameter_value(key, raw_value, definition)
            setattr(self, key, coerced)
            applied[key] = coerced
        return applied

    # ------------------------------------------------------------------
    def _coerce_parameter_value(self, name: str, value: Any, definition: Dict[str, Any]) -> Any:
        """Coerce and validate an incoming parameter value."""

        allow_null = bool(definition.get("allow_null"))
        if value == "":
            value = None

        if value is None:
            if allow_null:
                return None
            raise ValueError(f"Parameter '{name}' does not allow null values")

        param_type = str(definition.get("type", "float")).lower()

        try:
            if param_type == "int":
                coerced: Any = int(float(value))
            elif param_type == "float":
                coerced = float(value)
            elif param_type == "bool":
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"true", "1", "yes", "on"}:
                        coerced = True
                    elif lowered in {"false", "0", "no", "off"}:
                        coerced = False
                    else:
                        raise ValueError
                else:
                    coerced = bool(value)
            else:
                coerced = value
        except (TypeError, ValueError):
            raise ValueError(f"Parameter '{name}' expects {param_type} values") from None

        if param_type in {"int", "float"}:
            minimum = definition.get("min")
            maximum = definition.get("max")
            if minimum is not None and coerced < minimum:
                raise ValueError(f"Parameter '{name}' must be >= {minimum}")
            if maximum is not None and coerced > maximum:
                raise ValueError(f"Parameter '{name}' must be <= {maximum}")

        choices: Optional[Iterable[Any]] = definition.get("choices")  # type: ignore[assignment]
        if choices is not None and coerced not in choices:
            raise ValueError(f"Parameter '{name}' value {coerced} not in allowed choices")

        return coerced

