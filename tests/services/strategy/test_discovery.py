from __future__ import annotations

from services.strategy.discovery import discover_builtin_strategies


def test_discover_builtin_strategies_skips_helper_classes() -> None:
    mapping = discover_builtin_strategies()

    # Base helper classes should never be surfaced as selectable strategies.
    assert "BaseStrategy" not in mapping
    assert "CandleSubscriptionStrategy" not in mapping

    # Concrete implementations remain discoverable.
    assert "BuyTheDipStrategy" in mapping
    assert "DOMStrategy" in mapping
