"""IB gateway service exposing a shared Interactive Brokers client."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

__all__ = ["run_service", "main"]

_MAIN_MODULE: ModuleType | None = None


def _load_main_module() -> ModuleType:
    """Import and cache :mod:`services.ib_gateway.main` safely."""

    global _MAIN_MODULE
    if _MAIN_MODULE is None:
        _MAIN_MODULE = import_module(f"{__name__}.main")
    return _MAIN_MODULE


def __getattr__(name: str) -> Any:
    if name in __all__:
        module = _load_main_module()
        try:
            return getattr(module, name)
        except AttributeError as exc:  # pragma: no cover - defensive guard
            raise AttributeError(name) from exc
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
