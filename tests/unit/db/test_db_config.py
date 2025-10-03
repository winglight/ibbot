import pytest

from src.db import DatabaseConfigError, get_database_url
from src.db.mariadb import set_engine


@pytest.fixture(autouse=True)
def clear_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IBBOT_DB_URL", raising=False)
    monkeypatch.delenv("DATA_LAYER_DB_URL", raising=False)
    set_engine(None)
    yield
    set_engine(None)


def test_get_database_url_prefers_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "mysql://user:pass@localhost:3306/ibbot"
    monkeypatch.setenv("IBBOT_DB_URL", url)
    assert get_database_url() == url


def test_get_database_url_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "sqlite:///test.db"
    monkeypatch.setenv("DATA_LAYER_DB_URL", url)
    assert get_database_url() == url


def test_get_database_url_raises_when_missing() -> None:
    with pytest.raises(DatabaseConfigError):
        get_database_url()
