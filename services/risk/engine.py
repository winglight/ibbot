from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalise_symbol(symbol: str) -> str:
    return symbol.strip().upper()


@dataclass(slots=True)
class RiskRule:
    """Runtime representation of a configured risk rule."""

    symbol: str
    stop_loss: float | None = None
    take_profit: float | None = None
    enabled: bool = True
    max_position: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    _BASE_FIELDS = {
        "symbol",
        "stop_loss",
        "stop_loss_price",
        "take_profit",
        "take_profit_price",
        "enabled",
        "max_position",
        "updated_at",
    }
    _PROMOTED_KEYS = {"rule_type", "atr_params", "auto_trailing", "trail_ticks"}
    _PROMOTED_PREFIXES = ("trail_",)
    _PROMOTED_SUFFIXES = ("_multiplier",)

    @staticmethod
    def _flatten_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        flat: dict[str, Any] = {}

        def _merge(source: Mapping[str, Any]) -> None:
            for key, value in source.items():
                key_str = str(key)
                if key_str == "metadata" and isinstance(value, Mapping):
                    _merge(value)
                else:
                    flat[key_str] = value

        _merge(metadata)
        return flat

    @classmethod
    def _should_promote_metadata(cls, key: str) -> bool:
        if key in cls._PROMOTED_KEYS:
            return True
        return key.startswith(cls._PROMOTED_PREFIXES) or key.endswith(
            cls._PROMOTED_SUFFIXES
        )

    @classmethod
    def normalise_payload(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Promote known metadata keys to the top-level rule payload."""

        base: dict[str, Any] = {}
        metadata_bucket: dict[str, Any] = {}

        for key, value in payload.items():
            key_str = str(key)
            if key_str == "metadata" and isinstance(value, Mapping):
                continue
            if key_str in cls._BASE_FIELDS or cls._should_promote_metadata(key_str):
                base[key_str] = value
            else:
                metadata_bucket[key_str] = value

        raw_metadata = payload.get("metadata")
        if isinstance(raw_metadata, Mapping):
            flattened = cls._flatten_metadata(raw_metadata)
            for key, value in flattened.items():
                key_str = str(key)
                if cls._should_promote_metadata(key_str):
                    if key_str == "atr_params" and isinstance(value, Mapping):
                        base[key_str] = {str(k): v for k, v in value.items()}
                    else:
                        base[key_str] = value
                else:
                    metadata_bucket[key_str] = value

        if metadata_bucket:
            base["metadata"] = metadata_bucket
        else:
            base.pop("metadata", None)

        return base

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RiskRule":
        symbol_raw = payload.get("symbol")
        if not symbol_raw:
            raise ValueError("risk rule requires a symbol")
        symbol = _normalise_symbol(str(symbol_raw))
        stop_loss = _to_float(payload.get("stop_loss") or payload.get("stop_loss_price"))
        take_profit = _to_float(payload.get("take_profit") or payload.get("take_profit_price"))
        max_position_value = payload.get("max_position")
        max_position = None
        if max_position_value is not None and str(max_position_value).strip():
            try:
                max_position = int(max_position_value)
            except (TypeError, ValueError):
                max_position = None
        enabled = _to_bool(payload.get("enabled"), True)
        metadata: dict[str, Any] = {}
        raw_metadata = payload.get("metadata")
        if isinstance(raw_metadata, Mapping):
            metadata.update(cls._flatten_metadata(raw_metadata))
        for key, value in payload.items():
            key_str = str(key)
            if key_str in cls._BASE_FIELDS or key_str == "metadata":
                continue
            metadata[key_str] = value
        return cls(
            symbol=symbol,
            stop_loss=stop_loss,
            take_profit=take_profit,
            enabled=enabled,
            max_position=max_position,
            metadata=metadata,
        )

    def to_payload(self) -> dict[str, Any]:
        """Return a serialisable representation of the rule."""

        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "enabled": self.enabled,
            "stop_loss": self.stop_loss,
            "stop_loss_price": self.stop_loss,
            "take_profit": self.take_profit,
            "take_profit_price": self.take_profit,
        }
        if self.max_position is not None:
            payload["max_position"] = self.max_position
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return self.normalise_payload(payload)


@dataclass(slots=True)
class RiskEvent:
    """Outcome produced by the risk engine when a rule triggers."""

    symbol: str
    trigger: str
    price: float
    rule: RiskRule
    context: Mapping[str, Any]
    command_id: str | None = None

    def build_command(self, *, command_id: str) -> dict[str, Any]:
        """Return the command payload to enqueue for the triggered event."""

        reason = f"risk_{self.trigger}"
        payload = {
            "command_id": command_id,
            "method": "submit_risk_close",
            "args": [],
            "kwargs": {
                "symbol": self.symbol,
                "reason": reason,
                "price": self.price,
            },
        }
        return payload


class RiskEngine:
    """Evaluate incoming market data against configured rules."""

    def __init__(self) -> None:
        self._rules: dict[str, RiskRule] = {}
        self._trigger_state: dict[tuple[str, str], bool] = {}

    # ------------------------------------------------------------------
    # configuration management
    # ------------------------------------------------------------------
    def load_config(self, config: Mapping[str, Any]) -> None:
        rules = self._parse_rules(config)
        self._rules = {rule.symbol: rule for rule in rules}
        # Drop trigger state for removed symbols
        active_symbols = set(self._rules.keys())
        self._trigger_state = {
            key: value
            for key, value in self._trigger_state.items()
            if key[0] in active_symbols
        }

    def _parse_rules(self, config: Mapping[str, Any]) -> Iterable[RiskRule]:
        raw_rules = config.get("rules")
        parsed_rules: list[RiskRule] = []
        if isinstance(raw_rules, str):
            text = raw_rules.strip()
            if text:
                try:
                    items = json.loads(text)
                except json.JSONDecodeError:
                    items = []
                parsed_rules.extend(self._convert_sequence(items))
        elif isinstance(raw_rules, Iterable):
            parsed_rules.extend(self._convert_sequence(raw_rules))
        else:
            # Support flattened config using keys like "rules.MNQ.stop_loss"
            grouped: dict[str, dict[str, Any]] = {}
            for key, value in config.items():
                if not isinstance(key, str):
                    continue
                if not key.startswith("rules."):
                    continue
                parts = key.split(".")
                if len(parts) < 3:
                    continue
                _, symbol, field = parts[0], parts[1], ".".join(parts[2:])
                bucket = grouped.setdefault(symbol, {})
                bucket[field] = value
            parsed_rules.extend(self._convert_sequence(
                [
                    {"symbol": symbol, **values}
                    for symbol, values in grouped.items()
                ]
            ))
        return parsed_rules

    def _convert_sequence(self, items: Iterable[Any]) -> list[RiskRule]:
        rules: list[RiskRule] = []
        for item in items:
            if isinstance(item, Mapping):
                try:
                    rule = RiskRule.from_mapping(item)
                except ValueError:
                    continue
                rules.append(rule)
        return rules

    def export_rules(self) -> dict[str, dict[str, Any]]:
        """Return the currently active rules as serialisable payloads."""

        return {symbol: rule.to_payload() for symbol, rule in self._rules.items()}

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def evaluate_price(self, symbol: str, price: float, context: Mapping[str, Any]) -> list[RiskEvent]:
        symbol_key = _normalise_symbol(symbol)
        rule = self._rules.get(symbol_key)
        if rule is None or not rule.enabled:
            return []

        events: list[RiskEvent] = []
        if rule.stop_loss is not None and price <= rule.stop_loss:
            if not self._is_triggered(symbol_key, "stop_loss"):
                events.append(self._activate(symbol_key, "stop_loss", price, rule, context))
        if rule.take_profit is not None and price >= rule.take_profit:
            if not self._is_triggered(symbol_key, "take_profit"):
                events.append(self._activate(symbol_key, "take_profit", price, rule, context))
        return events

    def _is_triggered(self, symbol: str, trigger: str) -> bool:
        return self._trigger_state.get((symbol, trigger), False)

    def _activate(
        self,
        symbol: str,
        trigger: str,
        price: float,
        rule: RiskRule,
        context: Mapping[str, Any],
    ) -> RiskEvent:
        self._trigger_state[(symbol, trigger)] = True
        return RiskEvent(symbol=symbol, trigger=trigger, price=price, rule=rule, context=context)

    def reset_trigger(self, symbol: str, trigger: str) -> None:
        key = (_normalise_symbol(symbol), trigger)
        if key in self._trigger_state:
            self._trigger_state[key] = False

    # ------------------------------------------------------------------
    # inspection helpers
    # ------------------------------------------------------------------
    @property
    def rules(self) -> Mapping[str, RiskRule]:
        return dict(self._rules)

    def is_armed(self, symbol: str, trigger: str) -> bool:
        return not self._is_triggered(_normalise_symbol(symbol), trigger)
