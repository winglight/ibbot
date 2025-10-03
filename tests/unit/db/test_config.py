import pytest

from src.ibbot.db import config


def _clear_cache() -> None:
    config.get_database_url.cache_clear()


def test_resolve_database_url_uses_primary_env() -> None:
    url = "mysql+asyncmy://user:pass@localhost:3306/ibbot"
    env = {config.PRIMARY_ENV_VAR: url}
    assert config.resolve_database_url(env) == url


def test_resolve_database_url_uses_fallback() -> None:
    url = "mysql+asyncmy://user:pass@localhost:3306/ibbot"
    env = {config.FALLBACK_ENV_VAR: url}
    assert config.resolve_database_url(env) == url


def test_resolve_database_url_rejects_unknown_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {config.PRIMARY_ENV_VAR: "postgresql+asyncpg://user:pass@db/test"}
    with pytest.raises(config.DatabaseConfigError):
        config.resolve_database_url(env)


def test_resolve_database_url_accepts_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_file = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    env = {config.PRIMARY_ENV_VAR: url}
    assert config.resolve_database_url(env) == url


def test_resolve_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(config.DatabaseConfigError):
        config.resolve_database_url({})


def test_get_database_url_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    first_url = "mysql+asyncmy://user:pass@localhost/db"
    second_url = "mysql+asyncmy://user:pass@localhost/other"
    monkeypatch.setenv(config.PRIMARY_ENV_VAR, first_url)
    monkeypatch.delenv(config.FALLBACK_ENV_VAR, raising=False)
    _clear_cache()
    assert config.get_database_url() == first_url
    # Update environment; cached value should be returned.
    monkeypatch.setenv(config.PRIMARY_ENV_VAR, second_url)
    assert config.get_database_url() == first_url
    _clear_cache()
