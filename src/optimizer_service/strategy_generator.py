"""Utility helpers for generating strategy modules from optimizer results."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

STRATEGY_DIR = Path("src/strategies")


class StrategyGenerationError(RuntimeError):
    """Raised when strategy generation cannot be completed."""


def _slugify_name(name: str) -> Tuple[str, str]:
    """Return filename slug and class name from the provided strategy name."""

    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", name or "").strip("_")
    if not cleaned:
        cleaned = "dom_strategy"
    slug = cleaned.lower()
    class_name = "".join(part.capitalize() for part in cleaned.split("_"))
    if not class_name:
        class_name = "DomStrategy"
    return slug, class_name


def _ensure_unique_path(base_path: Path) -> Path:
    """Ensure the generated filename does not override existing strategies."""

    if not base_path.exists():
        return base_path
    counter = 1
    while True:
        candidate = base_path.with_name(f"{base_path.stem}_{counter}{base_path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _format_json(value: Dict[str, Any]) -> str:
    return json.dumps(value or {}, indent=4, ensure_ascii=False)


def generate_strategy_file(
    *,
    strategy_name: str,
    base_parameters: Dict[str, Any],
    feature_parameters: Dict[str, Any],
    plan_id: str | None = None,
    task_id: str | None = None,
) -> Tuple[str, str]:
    """Persist an auto-generated strategy module.

    Returns a tuple containing the relative file path and the generated class name.
    """

    if not base_parameters:
        raise StrategyGenerationError("base_parameters must not be empty")

    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)

    slug, class_name = _slugify_name(strategy_name)
    file_path = _ensure_unique_path(STRATEGY_DIR / f"{slug}.py")
    timestamp = datetime.utcnow().isoformat() + "Z"

    base_json = _format_json(base_parameters)
    feature_json = _format_json(feature_parameters)

    module_source = f'''"""Auto-generated strategy created from DOM optimizer results."""

from __future__ import annotations

from typing import Any, Dict

from src.strategies.base import BaseStrategy

BASE_PARAMETERS: Dict[str, Any] = {base_json}
FEATURE_PARAMETERS: Dict[str, Any] = {feature_json}


class {class_name}(BaseStrategy):
    """Auto-generated on {timestamp} via DOM optimizer."""

    source_plan_id: str | None = {plan_id!r}
    source_task_id: str | None = {task_id!r}

    def __init__(self, name: str = {strategy_name!r}) -> None:
        super().__init__(name=name, description="Auto generated DOM optimizer strategy")
        self.base_parameters = dict(BASE_PARAMETERS)
        self.feature_parameters = dict(FEATURE_PARAMETERS)

    def get_configuration(self) -> Dict[str, Any]:
        """Return a copy of the optimization parameters."""

        return {{
            "base": dict(self.base_parameters),
            "features": dict(self.feature_parameters),
        }}
'''

    file_path.write_text(module_source, encoding="utf-8")

    try:
        relative_path = file_path.relative_to(Path.cwd())
    except ValueError:
        relative_path = file_path

    return str(relative_path), class_name
