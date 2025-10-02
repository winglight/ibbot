from __future__ import annotations

from src.startup_checks import (
    detect_open_sqlite_handles,
    get_sqlite_handle_check,
    record_sqlite_handle_check,
    reset_startup_checks,
)


def test_detect_open_sqlite_handles_identifies_open_files(tmp_path):
    path = tmp_path / "sample.sqlite"
    path.write_text("data")

    # No handles should be reported when the file is closed.
    handles_when_closed = detect_open_sqlite_handles()
    assert path.name not in "".join(handles_when_closed)

    with path.open("r"):
        handles = detect_open_sqlite_handles()
        assert any(path.name in handle for handle in handles)

    handles_after_close = detect_open_sqlite_handles()
    assert all(path.name not in handle for handle in handles_after_close)


def test_record_sqlite_handle_check_records_health_state():
    reset_startup_checks()

    record_sqlite_handle_check([])
    result = get_sqlite_handle_check()
    assert result is not None
    assert result["healthy"] is True
    assert "detail" in result

    record_sqlite_handle_check(["/tmp/example.sqlite", "/tmp/example.sqlite"])
    result = get_sqlite_handle_check()
    assert result is not None
    assert result["healthy"] is False
    payload = result.get("payload") or {}
    assert payload.get("handles") == ["/tmp/example.sqlite"]
