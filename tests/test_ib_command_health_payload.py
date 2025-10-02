from datetime import datetime, timezone

from src.ib_command_health import build_ib_connection_health_payload


def test_build_ib_connection_health_payload_includes_error_code():
    now = datetime.now(timezone.utc).isoformat()
    payload = build_ib_connection_health_payload(
        {
            "last_seen": now,
            "queue_depth": "0",
            "last_error": "",
            "last_error_code": "10147",
        },
        stale_after=60,
        account_type="Paper",
    )

    assert payload["error_code"] == 10147
    assert payload["last_error"] is None
    assert payload["connected"] is False
    assert payload["queue_depth"] == 0


def test_build_ib_connection_health_payload_handles_missing_error_code():
    payload = build_ib_connection_health_payload(
        {"queue_depth": "3", "last_error": "", "last_seen": ""},
        stale_after=60,
        account_type="Paper",
    )

    assert payload["error_code"] is None
    assert payload["connected"] is False  # stale when last_seen missing


def test_build_ib_connection_health_payload_prefers_ib_connection_fields():
    now = datetime.now(timezone.utc).isoformat()
    payload = build_ib_connection_health_payload(
        {
            "last_seen": now,
            "queue_depth": "5",
            "last_error": "",
            "last_error_code": "",
            "ib_connected": "false",
            "ib_connection_error": "gateway down",
        },
        stale_after=300,
        account_type="Paper",
    )

    assert payload["connected"] is False
    assert payload["last_error"] == "gateway down"
    assert payload["stale"] is False

    healthy_payload = build_ib_connection_health_payload(
        {
            "last_seen": now,
            "queue_depth": "1",
            "ib_connected": "true",
            "ib_connection_error": "",
        },
        stale_after=300,
        account_type="Paper",
    )

    assert healthy_payload["connected"] is True
    assert healthy_payload["last_error"] is None
