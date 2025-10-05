from __future__ import annotations

import importlib
import pkgutil
import sys
import types

import pytest

from services.strategy.discovery import (
    discover_builtin_strategies,
    discover_filesystem_strategies,
)


def test_discover_builtin_strategies_skips_helper_classes() -> None:
    mapping = discover_builtin_strategies()

    # Base helper classes should never be surfaced as selectable strategies.
    assert "BaseStrategy" not in mapping
    assert "CandleSubscriptionStrategy" not in mapping

    # Concrete implementations remain discoverable.
    assert "BuyTheDipStrategy" in mapping
    assert "DOMStrategy" in mapping


def test_discover_filesystem_strategies_normalise_names(tmp_path) -> None:
    nested = tmp_path / "space dir"
    nested.mkdir(parents=True)
    strategy_file = nested / "fancy-strategy v1!.py"
    strategy_file.write_text("print('hello')\n")

    mapping = discover_filesystem_strategies(tmp_path)

    assert "SpaceDirFancyStrategyV1" in mapping
    entry = mapping["SpaceDirFancyStrategyV1"]
    assert entry["metadata"]["file"] == "space dir/fancy-strategy v1!.py"
    assert entry["metadata"]["normalised_name"] == "SpaceDirFancyStrategyV1"


def test_discover_filesystem_strategies_deduplicates_aliases(tmp_path) -> None:
    original = tmp_path / "alpha.py"
    original.write_text("print('alpha')\n")
    alias_dir = tmp_path / "alias-folder"
    alias_dir.mkdir()
    alias = alias_dir / "alpha alias.py"
    try:
        alias.symlink_to(original)
    except OSError:
        pytest.skip("Symlinks not supported")

    mapping = discover_filesystem_strategies(tmp_path)

    assert len(mapping) == 1
    name, entry = next(iter(mapping.items()))
    assert entry["metadata"]["normalised_name"] == name


def test_discover_builtin_strategies_deduplicates_modules(monkeypatch) -> None:
    base_module = types.ModuleType("src.strategies")
    base_module.__path__ = ["dummy"]
    BaseStrategy = type("BaseStrategy", (), {})
    base_module.BaseStrategy = BaseStrategy

    alpha_module = types.ModuleType("src.strategies.alpha")

    class AlphaStrategy(BaseStrategy):
        pass

    AlphaStrategy.__module__ = "src.strategies.alpha"
    alpha_module.AlphaStrategy = AlphaStrategy

    import_calls: list[str] = []

    def fake_import(name: str):
        import_calls.append(name)
        if name == "src.strategies":
            return base_module
        if name == "src.strategies.alpha":
            return alpha_module
        raise ImportError(name)

    def fake_walk_packages(paths, prefix):
        yield pkgutil.ModuleInfo(None, "src.strategies.alpha", False)
        yield pkgutil.ModuleInfo(None, "src.strategies.alpha", False)

    monkeypatch.setitem(sys.modules, "src.strategies", base_module)
    monkeypatch.setitem(sys.modules, "src.strategies.alpha", alpha_module)
    monkeypatch.setattr(pkgutil, "walk_packages", fake_walk_packages)
    monkeypatch.setattr(importlib, "import_module", fake_import)

    mapping = discover_builtin_strategies("src.strategies")

    assert mapping == {"AlphaStrategy": "src.strategies.alpha:AlphaStrategy"}
    assert import_calls.count("src.strategies.alpha") == 1
