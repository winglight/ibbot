import json

import pytest

from src.db.repositories.risk import RiskSnapshotRepository


pytestmark = pytest.mark.anyio


async def test_save_rule_snapshots_persists_config_and_rules(sqlite_db) -> None:
    repository = RiskSnapshotRepository()

    await repository.save_rule_snapshots(
        service="risk-service",
        rules={
            "ES": {"stop_loss_price": 4100.5, "enabled": True},
            "NQ": {
                "symbol": "NQ",
                "take_profit_price": 15500.25,
                "enabled": False,
                "rule_type": "trailing",
            },
        },
        captured_at="2024-01-01T12:00:00+00:00",
    )

    cursor = sqlite_db.cursor()
    try:
        cursor.execute(
            "SELECT config_key, config_value FROM config WHERE config_key LIKE ?",
            ("risk-service:%",),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    config_values = {row["config_key"]: row["config_value"] for row in rows}
    assert "risk-service:snapshot" in config_values
    snapshot_payload = json.loads(config_values["risk-service:snapshot"])
    assert snapshot_payload["rules_count"] == 2
    assert {rule["symbol"] for rule in snapshot_payload["rules"]} == {"ES", "NQ"}
    assert snapshot_payload["captured_at"].startswith("2024-01-01T12:00:00")

    rules_cursor = sqlite_db.cursor()
    try:
        rules_cursor.execute(
            "SELECT symbol, stop_loss_price, take_profit_price, enabled, rule_type FROM risk_rules ORDER BY symbol"
        )
        persisted_rules = rules_cursor.fetchall()
    finally:
        rules_cursor.close()

    assert [row["symbol"] for row in persisted_rules] == ["ES", "NQ"]
    es_rule, nq_rule = persisted_rules
    assert pytest.approx(es_rule["stop_loss_price"]) == 4100.5
    assert es_rule["enabled"] == 1
    assert pytest.approx(nq_rule["take_profit_price"]) == 15500.25
    assert nq_rule["enabled"] == 0
    assert nq_rule["rule_type"] == "trailing"


async def test_save_rule_snapshots_replaces_missing_rules(sqlite_db) -> None:
    repository = RiskSnapshotRepository()

    await repository.save_rule_snapshots(
        service="risk-service",
        rules=[{"symbol": "ES", "stop_loss_price": 4000.0}, {"symbol": "NQ"}],
        captured_at="2024-02-01T08:00:00+00:00",
    )

    await repository.save_rule_snapshots(
        service="risk-service",
        rules=[{"symbol": "ES", "stop_loss_price": 4125.0}],
        captured_at="2024-03-01T08:00:00+00:00",
    )

    cursor = sqlite_db.cursor()
    try:
        cursor.execute(
            "SELECT symbol, stop_loss_price FROM risk_rules ORDER BY symbol"
        )
        remaining = cursor.fetchall()
    finally:
        cursor.close()

    assert [row["symbol"] for row in remaining] == ["ES"]
    assert pytest.approx(remaining[0]["stop_loss_price"]) == 4125.0

    config_cursor = sqlite_db.cursor()
    try:
        config_cursor.execute(
            "SELECT config_value FROM config WHERE config_key = ?",
            ("risk-service:snapshot",),
        )
        stored = config_cursor.fetchone()
    finally:
        config_cursor.close()

    assert stored is not None
    snapshot = json.loads(stored[0])
    assert snapshot["rules_count"] == 1
    assert [rule["symbol"] for rule in snapshot["rules"]] == ["ES"]
