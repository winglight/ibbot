from __future__ import annotations

from typing import Any, Dict, Mapping

INTERNAL_INFO_SHOULD_FAIL = False


def set_internal_info_failure(flag: bool) -> None:
    global INTERNAL_INFO_SHOULD_FAIL
    INTERNAL_INFO_SHOULD_FAIL = bool(flag)


class SampleStrategy:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.description = self.config.get("description", "Sample strategy")
        self.size = int(self.config.get("size", 1) or 1)
        self.symbol = self.config.get("symbol")

    def describe_parameters(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "size",
                "type": "int",
                "default": 1,
                "value": self.size,
            }
        ]

    def get_internal_info(self) -> Dict[str, Any]:
        if INTERNAL_INFO_SHOULD_FAIL:
            raise RuntimeError("internal info unavailable")
        return {
            "description": self.description,
            "configurable_parameters": self.describe_parameters(),
            "primary_symbol": self.symbol,
            "runtime_snapshot": {
                "size": self.size,
            },
        }

    def apply_parameter_updates(self, updates: Mapping[str, Any]) -> Dict[str, Any]:
        if "size" not in updates:
            return {}
        value = int(updates["size"])
        if value <= 0:
            raise ValueError("size must be positive")
        self.size = value
        return {"size": self.size}


def factory(config: Mapping[str, Any]) -> SampleStrategy:
    return SampleStrategy(config)
