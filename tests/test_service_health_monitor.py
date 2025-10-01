from datetime import datetime, timedelta, timezone

from main import _has_meaningful_health_change


def _payload(last_seen: datetime, **overrides):
    base = {
        "connected": True,
        "account_type": "Paper",
        "last_seen": last_seen.isoformat(),
        "queue_depth": 0,
        "last_error": None,
        "stale": False,
        "source": "redis",
        "type": "connection_status",
    }
    base.update(overrides)
    return base


def test_has_meaningful_health_change_ignores_small_last_seen_updates():
    now = datetime.now(timezone.utc)
    previous = _payload(now)
    updated = _payload(now + timedelta(seconds=15))

    assert (
        _has_meaningful_health_change(
            updated,
            previous,
            interval=5,
            stale_after=15,
        )
        is False
    )


def test_has_meaningful_health_change_detects_state_changes():
    now = datetime.now(timezone.utc)
    previous = _payload(now)
    degraded = _payload(
        now + timedelta(minutes=5),
        connected=False,
        stale=True,
        last_error="disconnected",
    )

    assert (
        _has_meaningful_health_change(
            degraded,
            previous,
            interval=5,
            stale_after=15,
        )
        is True
    )
