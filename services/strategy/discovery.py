"""Helpers for discovering bundled strategy implementations."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import pkgutil
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping

DEFAULT_METADATA_PREFIX = "json:strategy"
_FILESYSTEM_SUFFIXES = (".py", ".json", ".yaml", ".yml")
_FILESYSTEM_METADATA_KEYS = {
    "file",
    "file_path",
    "filepath",
    "path",
    "script",
    "scripts",
    "files",
    "paths",
}

_SKIPPED_MODULE_BASENAMES = {"__init__", "base", "candle_base"}
_HELPER_CLASS_FLAGS = (
    "__abstract__",
    "__is_strategy_helper__",
    "__strategy_helper__",
    "__strategy_base__",
)


@dataclass(slots=True)
class StrategyMetadata:
    """Structure representing persisted strategy metadata."""

    name: str
    factory: str | None
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


def _is_helper_strategy_class(candidate: type[object]) -> bool:
    """Return ``True`` when ``candidate`` represents a helper/base strategy class."""

    if inspect.isabstract(candidate):
        return True
    for flag in _HELPER_CLASS_FLAGS:
        if getattr(candidate, "__dict__", {}).get(flag):
            return True
    return False


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

    seen_modules: set[str] = set()

    for module_info in pkgutil.walk_packages(package_paths, prefix=prefix):
        if module_info.ispkg:
            continue
        module_name = module_info.name
        if module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        module_basename = module_name.rsplit(".", 1)[-1]
        if module_basename in _SKIPPED_MODULE_BASENAMES:
            continue
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
            if _is_helper_strategy_class(attr_value):
                continue
            strategy_name = attr_value.__name__
            discovered.setdefault(strategy_name, f"{module_name}:{strategy_name}")
    return discovered


def _normalise_spec(
    spec: Any,
) -> tuple[str | None, dict[str, Any], dict[str, Any], str | None, bool | None]:
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


def _coerce_existing_metadata(
    name: str,
    payload: Mapping[str, Any],
    *,
    default_timestamp: str,
) -> StrategyMetadata:
    params_value = payload.get("params")
    metadata_value = payload.get("metadata")
    description_value = payload.get("description")

    params: dict[str, Any] = {}
    if isinstance(params_value, Mapping):
        params = {str(k): v for k, v in params_value.items()}

    metadata: dict[str, Any] = {}
    if isinstance(metadata_value, Mapping):
        metadata = {str(k): v for k, v in metadata_value.items()}

    factory_value = payload.get("factory")
    factory = None
    if isinstance(factory_value, str):
        factory = factory_value or None
    elif factory_value is not None:
        factory = str(factory_value) or None

    description = str(description_value) if isinstance(description_value, str) else None
    created_at = str(payload.get("created_at") or default_timestamp)
    updated_at = str(payload.get("updated_at") or default_timestamp)
    enabled = bool(payload.get("enabled", True))

    return StrategyMetadata(
        name=name,
        factory=factory,
        params=params,
        metadata=metadata,
        description=description,
        enabled=enabled,
        created_at=created_at,
        updated_at=updated_at,
    )


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
    factory: str | None,
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

    if "normalised_name" not in metadata_payload:
        for key in _FILESYSTEM_METADATA_KEYS:
            value = metadata_payload.get(key)
            if isinstance(value, str) and value:
                candidate = value.rsplit(".", 1)[0]
                normalised_candidate = _to_camel_case(candidate)
                if normalised_candidate:
                    metadata_payload["normalised_name"] = normalised_candidate
                break

    description_value = description if description is not None else base_metadata.get("description")
    enabled_value = (
        bool(enabled)
        if enabled is not None
        else bool(base_metadata.get("enabled", True))
    )

    existing_factory = base_metadata.get("factory")
    if isinstance(existing_factory, str) and existing_factory:
        default_factory: str | None = existing_factory
    else:
        default_factory = None

    if factory is not None and isinstance(factory, str):
        factory_value: str | None = factory or default_factory
    else:
        factory_value = default_factory

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


def _resolve_strategy_path(path_value: str | os.PathLike[str] | None) -> Path | None:
    if not path_value:
        return None
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def _iter_strategy_files(base: Path) -> Iterable[Path]:
    if base.is_dir():
        for candidate in base.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in _FILESYSTEM_SUFFIXES:
                continue
            yield candidate
    elif base.is_file():
        if base.suffix.lower() in _FILESYSTEM_SUFFIXES:
            yield base


_IDENTIFIER_RE = re.compile(r"[^0-9A-Za-z]+")


def _to_camel_case(value: str) -> str:
    parts = [segment for segment in _IDENTIFIER_RE.split(value) if segment]
    if not parts:
        return ""
    camel = "".join(segment[:1].upper() + segment[1:] for segment in parts)
    if camel and camel[0].isdigit():
        camel = f"_{camel}"
    return camel


def _derive_strategy_name(candidate: Path, *, base: Path | None) -> str:
    relative: str
    if base is not None and base.exists():
        try:
            relative = candidate.relative_to(base).as_posix()
        except ValueError:
            relative = candidate.name
    else:
        relative = candidate.name
    without_suffix = relative.rsplit(".", 1)[0]
    normalised = _to_camel_case(without_suffix)
    if not normalised:
        normalised = _to_camel_case(candidate.stem)
    return normalised or candidate.stem or candidate.name


def discover_filesystem_strategies(
    path_value: str | os.PathLike[str] | None,
) -> dict[str, Mapping[str, Any]]:
    resolved = _resolve_strategy_path(path_value)
    if resolved is None:
        return {}

    base_dir = resolved if resolved.is_dir() else resolved.parent if resolved.parent != resolved else resolved
    discovered: dict[str, Mapping[str, Any]] = {}

    seen_files: set[Path] = set()

    for candidate in _iter_strategy_files(resolved):
        try:
            resolved_candidate = candidate.resolve()
        except OSError:
            resolved_candidate = candidate
        if resolved_candidate in seen_files:
            continue
        seen_files.add(resolved_candidate)
        if resolved.is_dir():
            try:
                relative_path = candidate.relative_to(resolved).as_posix()
            except ValueError:
                relative_path = candidate.name
        else:
            try:
                relative_path = candidate.relative_to(base_dir).as_posix()
            except ValueError:
                relative_path = candidate.name

        strategy_name = _derive_strategy_name(candidate, base=base_dir if base_dir.exists() else resolved)
        metadata = {
            "source": "filesystem",
            "file": relative_path,
            "normalised_name": strategy_name,
        }
        spec: dict[str, Any] = {
            "factory": None,
            "params": {},
            "metadata": metadata,
            "description": None,
            "enabled": False,
        }
        discovered.setdefault(strategy_name, spec)

    return discovered


async def sync_strategy_metadata(
    redis_client: Any,
    configured: Mapping[str, Any],
    *,
    metadata_prefix: str = DEFAULT_METADATA_PREFIX,
    strategy_path: str | os.PathLike[str] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Merge discovered strategies with configured ones and persist metadata."""

    discovered = discover_builtin_strategies()
    merged: dict[str, Any] = {str(k): v for k, v in configured.items()}

    filesystem = discover_filesystem_strategies(strategy_path)

    for name, spec in filesystem.items():
        existing_spec = merged.get(name)
        if existing_spec is None:
            merged[name] = spec
            continue
        if isinstance(existing_spec, Mapping):
            base_spec = dict(existing_spec)
        else:
            base_spec = {"factory": existing_spec}
        metadata_payload = dict(base_spec.get("metadata") or {})
        for key, value in dict(spec.get("metadata") or {}).items():
            metadata_payload.setdefault(key, value)
        combined = {**spec, **base_spec}
        if metadata_payload:
            combined["metadata"] = metadata_payload
        if base_spec.get("factory"):
            combined["factory"] = base_spec.get("factory")
        merged[name] = combined

    for name, factory in discovered.items():
        merged.setdefault(name, {"factory": factory})

    now = datetime.now(timezone.utc).isoformat()
    existing_docs = await _load_existing_documents(redis_client, metadata_prefix=metadata_prefix)

    documents: dict[str, StrategyMetadata] = {}

    invalid: set[str] = set()

    for name, spec in merged.items():
        factory, params, metadata, description, enabled = _normalise_spec(spec)
        metadata_has_file = False
        if isinstance(metadata, Mapping):
            metadata_has_file = any(metadata.get(key) for key in _FILESYSTEM_METADATA_KEYS)
        if not factory and not metadata_has_file:
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

    for name, payload in existing_docs.items():
        if name in documents or name in invalid:
            continue
        if not isinstance(payload, Mapping):
            continue
        documents[name] = _coerce_existing_metadata(name, payload, default_timestamp=now)

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
    "discover_filesystem_strategies",
    "sync_strategy_metadata",
]
