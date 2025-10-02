"""Helpers for discovering bundled strategy implementations."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Mapping

DEFAULT_METADATA_PREFIX = "json:strategy"


@dataclass(slots=True)
class StrategyMetadata:
    """Structure representing persisted strategy metadata."""

    name: str
    factory: str
    params: dict[str, Any]
    metadata: dict[str, Any]
    description: str | None
    enabled: bool
    created_at: str
    updated_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "factory": self.factory,
            "params": self.params,
            "metadata": self.metadata,
            "description": self.description,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _load_package(package: str) -> ModuleType | None:
    try:
        return importlib.import_module(package)
    except Exception:  # pragma: no cover - defensive import guard
        return None


def discover_builtin_strategies(package: str = "src.strategies") -> dict[str, str]:
    """Return mapping of bundled strategy names to their factory path."""

    module = _load_package(package)
    if module is None or not hasattr(module, "BaseStrategy"):
        return {}

    base_cls = getattr(module, "BaseStrategy")
    package_paths = getattr(module, "__path__", None)
    if package_paths is None:
        return {}

    discovered: dict[str, str] = {}
    prefix = f"{package}."

    for module_info in pkgutil.walk_packages(package_paths, prefix=prefix):
        if module_info.ispkg:
            continue
        module_name = module_info.name
        try:
            candidate = importlib.import_module(module_name)
        except Exception:  # pragma: no cover - defensive import guard
            continue
        for attr_name, attr_value in inspect.getmembers(candidate, inspect.isclass):
            if attr_value is base_cls:
                continue
            if not issubclass(attr_value, base_cls):
                continue
            if attr_value.__module__ != module_name:
                continue
            strategy_name = attr_value.__name__
            discovered.setdefault(strategy_name, f"{module_name}:{strategy_name}")
    return discovered


def _normalise_spec(spec: Any) -> tuple[str | None, dict[str, Any], dict[str, Any], str | None, bool | None]:
    factory: str | None = None
    params: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    description: str | None = None
    enabled: bool | None = None

    if isinstance(spec, Mapping):
        candidate = spec.get("factory") or spec.get("callable")
        if isinstance(candidate, str):
            factory = candidate
        params_value = spec.get("params") or spec.get("config")
        if isinstance(params_value, Mapping):
            params = {str(k): v for k, v in params_value.items()}
        metadata_value = spec.get("metadata")
        if isinstance(metadata_value, Mapping):
            metadata = {str(k): v for k, v in metadata_value.items()}
        description_value = spec.get("description")
        if isinstance(description_value, str):
            description = description_value
        enabled_value = spec.get("enabled")
        if enabled_value is not None:
            enabled = bool(enabled_value)
    elif isinstance(spec, str):
        factory = spec

    return factory, params, metadata, description, enabled


async def _load_existing_documents(redis_client: Any, *, metadata_prefix: str) -> dict[str, dict[str, Any]]:
    index_key = f"{metadata_prefix}:index"
    documents: dict[str, dict[str, Any]] = {}
    try:
        raw_index = await redis_client.get(index_key)
    except Exception:  # pragma: no cover - defensive guard
        return documents

    names: list[str] = []
    if isinstance(raw_index, str):
        try:
            decoded = json.loads(raw_index)
        except json.JSONDecodeError:
            decoded = []
        if isinstance(decoded, list):
            names = [str(entry) for entry in decoded]
    elif isinstance(raw_index, list):
        names = [str(entry) for entry in raw_index]

    for name in names:
        metadata_key = f"{metadata_prefix}:{name}"
        try:
            raw_doc = await redis_client.get(metadata_key)
        except Exception:  # pragma: no cover - defensive guard
            continue
        if raw_doc is None:
            continue
        payload: dict[str, Any] | None = None
        if isinstance(raw_doc, str):
            try:
                payload_candidate = json.loads(raw_doc)
            except json.JSONDecodeError:
                payload_candidate = None
            if isinstance(payload_candidate, dict):
                payload = payload_candidate
        elif isinstance(raw_doc, Mapping):
            payload = dict(raw_doc)
        if payload is not None:
            documents[name] = payload
    return documents


def _build_metadata(
    name: str,
    *,
    factory: str,
    params: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    description: str | None,
    enabled: bool | None,
    existing: Mapping[str, Any] | None,
    now: str,
    discovered: bool,
) -> StrategyMetadata:
    base_metadata = dict(existing or {})
    created_at = str(base_metadata.get("created_at") or now)
    metadata_payload = dict(base_metadata.get("metadata") or {})
    params_payload = dict(base_metadata.get("params") or {})

    if params:
        params_payload = {str(k): v for k, v in params.items()}

    if metadata:
        metadata_payload.update({str(k): v for k, v in metadata.items()})

    if discovered:
        metadata_payload.setdefault("source", "builtin")

    description_value = description if description is not None else base_metadata.get("description")
    enabled_value = (
        bool(enabled)
        if enabled is not None
        else bool(base_metadata.get("enabled", True))
    )

    factory_value = factory or str(base_metadata.get("factory") or "")

    return StrategyMetadata(
        name=name,
        factory=factory_value,
        params=params_payload,
        metadata=metadata_payload,
        description=description_value,
        enabled=enabled_value,
        created_at=created_at,
        updated_at=now,
    )


async def sync_strategy_metadata(
    redis_client: Any,
    configured: Mapping[str, Any],
    *,
    metadata_prefix: str = DEFAULT_METADATA_PREFIX,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Merge discovered strategies with configured ones and persist metadata."""

    discovered = discover_builtin_strategies()
    merged: dict[str, Any] = {str(k): v for k, v in configured.items()}
    for name, factory in discovered.items():
        merged.setdefault(name, {"factory": factory})

    now = datetime.now(timezone.utc).isoformat()
    existing_docs = await _load_existing_documents(redis_client, metadata_prefix=metadata_prefix)

    documents: dict[str, StrategyMetadata] = {}

    invalid: set[str] = set()

    for name, spec in merged.items():
        factory, params, metadata, description, enabled = _normalise_spec(spec)
        if not factory:
            if logger is not None:
                logger.warning("Strategy %s is missing a factory specification", name)
            invalid.add(name)
            continue
        existing = existing_docs.get(name)
        metadata_obj = _build_metadata(
            name,
            factory=factory,
            params=params,
            metadata=metadata,
            description=description,
            enabled=enabled,
            existing=existing,
            now=now,
            discovered=name in discovered and name not in configured,
        )
        documents[name] = metadata_obj

    for name in invalid:
        merged.pop(name, None)

    if not documents:
        return merged

    names_sorted = sorted(documents)
    index_payload = json.dumps(names_sorted, ensure_ascii=False)
    try:
        await redis_client.set(f"{metadata_prefix}:index", index_payload)
    except Exception:  # pragma: no cover - defensive guard
        if logger is not None:
            logger.warning("Failed to persist strategy index", exc_info=True)
    for name, metadata_obj in documents.items():
        try:
            await redis_client.set(
                f"{metadata_prefix}:{name}",
                json.dumps(metadata_obj.to_payload(), ensure_ascii=False),
            )
        except Exception:  # pragma: no cover - defensive guard
            if logger is not None:
                logger.warning("Failed to persist metadata for strategy %s", name, exc_info=True)
    return merged


__all__ = [
    "DEFAULT_METADATA_PREFIX",
    "discover_builtin_strategies",
    "sync_strategy_metadata",
]
