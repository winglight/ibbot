from __future__ import annotations

import core
import pytest

from src.data_layer.quote_feed import IBQuoteFeed


def test_ib_quote_feed_resolves_client_without_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """IBQuoteFeed should not rely on the core container being initialised."""

    # Force the registry lookup to fail so the fallback path is exercised.
    import src.ib_client_registry as registry

    def _raise(*_: object, **__: object) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(registry, "get_client_sync", _raise)

    class DummyClient:
        def ensure_connection(self, **_: object) -> None:  # pragma: no cover - simple stub
            return None

    # Ensure no cached client is present and provide a simple fallback factory.
    core.__dict__.pop("ibkr_client", None)
    monkeypatch.setitem(core.__dict__, "IBKRClient", DummyClient, raising=False)

    feed = IBQuoteFeed()
    client = feed._resolve_ibkr_client()

    assert isinstance(client, DummyClient)
    assert core.__dict__.get("ibkr_client") is client

    # Clean up the cached instance for other tests.
    core.__dict__.pop("ibkr_client", None)
