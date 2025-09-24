"""Utility helpers for generating strategy modules from optimizer results."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

DEFAULT_STRATEGY_DIR = Path(os.getenv("OPTIMIZER_STRATEGY_DIR", "src/strategies"))


class StrategyGenerationError(RuntimeError):
    """Raised when strategy generation cannot be completed."""


def resolve_strategy_directory(path_value: str | Path | None = None) -> Path:
    """Resolve the output directory for generated strategies.

    The optimizer service honours the main application's strategy directory if
    provided. The ``path_value`` argument may be an absolute path, a relative
    path, or ``None`` to fall back to :data:`DEFAULT_STRATEGY_DIR`.
    """

    if isinstance(path_value, Path):
        base_dir = path_value
    elif path_value:
        base_dir = Path(path_value)
    else:
        base_dir = DEFAULT_STRATEGY_DIR

    if not base_dir.is_absolute():
        base_dir = (Path.cwd() / base_dir).resolve()

    return base_dir


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
    output_directory: str | Path | None = None,
) -> Tuple[Path, str]:
    """Persist an auto-generated strategy module.

    Returns a tuple containing the relative file path and the generated class name.
    """

    if not base_parameters:
        raise StrategyGenerationError("base_parameters must not be empty")

    target_dir = resolve_strategy_directory(output_directory)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive guard
        raise StrategyGenerationError(f"unable to prepare strategy directory: {exc}") from exc

    slug, class_name = _slugify_name(strategy_name)
    file_path = _ensure_unique_path(target_dir / f"{slug}.py")
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

    try:
        file_path.write_text(module_source, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive guard
        raise StrategyGenerationError(f"unable to write strategy file: {exc}") from exc

    return file_path, class_name
