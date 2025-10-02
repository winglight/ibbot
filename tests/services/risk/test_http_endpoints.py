from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from services.risk.engine import RiskRule
from services.risk.http import SERVICE_NAME, create_app


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, str]] = []

    def set(self, key: str, value: str) -> "_FakePipeline":
        self._operations.append((key, value))
        return self

    async def execute(self) -> list[bool]:
        results: list[bool] = []
        for key, value in self._operations:
            results.append(bool(await self._redis.set(key, value)))
        self._operations.clear()
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.hset_calls: list[tuple[str, dict[str, str]]] = []
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.store[key] = value
        return True

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def hset(self, key: str, mapping: Mapping[str, Any]) -> int:
        payload = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            payload[str(field)] = str(value)
        self.hset_calls.append((key, dict(payload)))
        return len(mapping)

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


class StubRuntime:
    config_cache_key = "json:risk:config"
    rules_cache_key = "json:risk:rules"
    rules_update_channel = "pubsub:risk:rules:update"

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._config: dict[str, Any] = {}
        self._rules: dict[str, dict[str, Any]] = {}
        self._rules_history: list[dict[str, Any]] = []
        self._sizing_history: list[dict[str, Any]] = []
        self.update_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.started = False
        self.last_position_ts: datetime | None = None

    async def update_config(self, config: Mapping[str, Any]) -> None:
        self.update_calls += 1
        config_map = dict(config)
        rules_payload = config_map.pop("rules", [])
        timestamp = datetime.now(timezone.utc).isoformat()
        self._config = {str(k): v for k, v in config_map.items()}
        self._config["updated_at"] = timestamp

        config_snapshot = dict(self._config)
        config_snapshot["updated_at"] = timestamp
        await self._redis.set(self.config_cache_key, json.dumps(config_snapshot))

        rules_map: dict[str, dict[str, Any]] = {}
        iterable: Any
        if isinstance(rules_payload, Mapping):
            iterable = rules_payload.values()
        else:
            iterable = rules_payload
        for entry in iterable or []:
            if not isinstance(entry, Mapping):
                continue
            symbol = str(entry.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            data = dict(entry)
            data["symbol"] = symbol
            data.setdefault("updated_at", timestamp)
            rules_map[symbol] = RiskRule.normalise_payload(data)
        self._rules = rules_map
        await self._redis.set(
            self.rules_cache_key,
            json.dumps(
                {
                    "success": True,
                    "rules": self._rules,
                    "updated_at": timestamp,
                    "source": SERVICE_NAME,
                    "count": len(self._rules),
                }
            ),
        )
        await self._redis.publish(
            self.rules_update_channel,
            json.dumps(
                {
                    "type": "risk_rules",
                    "success": True,
                    "rules": self._rules,
                    "updated_at": timestamp,
                    "source": SERVICE_NAME,
                    "count": len(self._rules),
                }
            ),
        )

    async def load_risk_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def save_risk_config(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        for key, value in updates.items():
            key_str = str(key)
            if key_str in {"rules", "updated_at"}:
                continue
            if value is None:
                self._config.pop(key_str, None)
            else:
                self._config[key_str] = value
        self._config["updated_at"] = timestamp
        return dict(self._config)

    async def load_risk_rules(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._rules.items()}

    async def save_risk_rule(self, symbol: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        symbol_str = str(symbol or updates.get("symbol") or "").strip().upper()
        if not symbol_str:
            raise ValueError("risk rule requires a symbol")
        existing = dict(self._rules.get(symbol_str, {"symbol": symbol_str}))
        merged = {k: v for k, v in existing.items() if k != "updated_at"}
        for key, value in updates.items():
            key_str = str(key)
            if key_str in {"symbol", "updated_at"}:
                continue
            if value is None:
                merged.pop(key_str, None)
            else:
                merged[key_str] = value
        merged["symbol"] = symbol_str
        timestamp = datetime.now(timezone.utc).isoformat()
        merged["updated_at"] = timestamp
        normalised = RiskRule.normalise_payload(merged)
        normalised["symbol"] = symbol_str
        normalised["updated_at"] = timestamp
        self._rules[symbol_str] = dict(normalised)
        self._rules_history.append(
            {
                "event": "upsert",
                "symbol": symbol_str,
                "updated_at": timestamp,
                "payload": dict(normalised),
            }
        )
        return dict(normalised)

    async def delete_risk_rule(self, symbol: str) -> bool:
        symbol_str = str(symbol or "").strip().upper()
        if symbol_str in self._rules:
            self._rules.pop(symbol_str, None)
            timestamp = datetime.now(timezone.utc).isoformat()
            self._rules_history.append(
                {
                    "event": "delete",
                    "symbol": symbol_str,
                    "updated_at": timestamp,
                }
            )
            return True
        return False

    async def reload_from_storage(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        payload = {k: v for k, v in self._config.items() if k != "updated_at"}
        payload["rules"] = list(self._rules.values())
        await self.update_config(payload)
        return self.get_config_snapshot(), self.get_rules_snapshot()

    async def append_sizing_history(self, payload: Mapping[str, Any]) -> None:
        self._sizing_history.append(dict(payload))

    async def fetch_rules_history(self, *, symbol: str | None = None, limit: int = 100) -> dict[str, Any]:
        symbol_key = str(symbol or "").strip().upper()
        items = [
            {"id": str(index), **entry}
            for index, entry in enumerate(self._rules_history)
            if not symbol_key or str(entry.get("symbol") or "").upper() == symbol_key
        ]
        items = items[-max(1, int(limit or 1)) :]
        return {"success": True, "items": items, "count": len(items)}

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        symbol_key = str(symbol or "").strip().upper()
        items = [
            dict(entry)
            for entry in self._sizing_history
            if not symbol_key or str(entry.get("symbol") or "").upper() == symbol_key
        ]
        items = items[-max(1, int(limit or 1)) :]
        return {"success": True, "items": items, "count": len(items)}

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.started = False

    def get_config_snapshot(self) -> dict[str, Any]:
        return dict(self._config)

    def get_rules_snapshot(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._rules.items()}

    def health_snapshot(self) -> dict[str, Any]:
        metrics = {
            "pending_commands": 0,
            "config_snapshot_at": self._config.get("updated_at"),
            "rules_snapshot_at": max((v.get("updated_at") for v in self._rules.values()), default=self._config.get("updated_at")),
            "sizing_history_events": len(self._sizing_history),
            "rules_history_events": len(self._rules_history),
        }
        if self.last_position_ts is not None:
            metrics["position_last_requested_at"] = self.last_position_ts.isoformat()
        return metrics

    async def position_sizing(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        stop_distance = float(payload.get("stop_distance") or 0)
        if stop_distance <= 0:
            raise ValueError("stop_distance must be positive")
        config = self.get_config_snapshot()
        equity = float(config.get("account_equity", 100000.0))
        risk_fraction = float(config.get("risk_per_trade_r", 0.01))
        point_value_map = config.get("point_value_map", {})
        symbol = str(payload.get("symbol") or "").strip().upper()
        point_value = float(point_value_map.get(symbol, 1.0)) if isinstance(point_value_map, Mapping) else 1.0
        risk_capital = max(0.0, equity * risk_fraction)
        contracts = max(0, int(risk_capital / (point_value * stop_distance)))
        self.last_position_ts = datetime.now(timezone.utc)
        result = {
            "success": True,
            "symbol": symbol,
            "contracts": contracts,
            "risk_capital": risk_capital,
            "point_value": point_value,
            "stop_distance": stop_distance,
        }
        self._sizing_history.append({**result, "timestamp": datetime.now(timezone.utc).isoformat()})
        return result


def _build_app(runtime: StubRuntime, *, initial_config: Mapping[str, Any] | None = None) -> TestClient:
    redis = runtime._redis

    def redis_factory() -> _FakeRedis:
        return redis

    app = create_app(
        runtime=runtime,
        initial_config=initial_config,
        redis_factory=redis_factory,
        use_provider=False,
        health_interval=0.05,
    )
    return TestClient(app)


def test_config_endpoints_update_snapshots_and_health() -> None:
    redis = _FakeRedis()
    runtime = StubRuntime(redis)
    client = _build_app(runtime, initial_config={"risk_per_trade_r": 0.02})

    with client:
        response = client.get("/risk/config")
        assert response.status_code == 200
        payload = response.json()
        assert pytest.approx(payload["risk_per_trade_r"]) == 0.02

        response = client.post("/risk/config", json={"stop_loss_ratio": 0.03})
        assert response.status_code == 200
        updated = response.json()
        assert pytest.approx(updated["stop_loss_ratio"]) == 0.03

        cached = client.get("/risk-config").json()
        assert pytest.approx(cached["stop_loss_ratio"]) == 0.03

    assert runtime.start_calls == 1
    assert runtime.stop_calls == 1
    assert any("pending_tasks" in mapping for _, mapping in redis.hset_calls)


def test_rule_crud_operations() -> None:
    redis = _FakeRedis()
    runtime = StubRuntime(redis)
    initial_rules = [
        {
            "symbol": "MNQ",
            "stop_loss_price": 100.0,
            "metadata": {
                "rule_type": "trailing",
                "atr_params": {"multiplier_sl": 1.5, "multiplier_tp": 2.5},
            },
        }
    ]
    client = _build_app(runtime, initial_config={"rules": initial_rules})

    with client:
        response = client.get("/risk/rules")
        data = response.json()
        assert "MNQ" in data["rules"]
        mnq_rule = data["rules"]["MNQ"]
        assert mnq_rule["rule_type"] == "trailing"
        assert mnq_rule["atr_params"] == {
            "multiplier_sl": 1.5,
            "multiplier_tp": 2.5,
        }
        assert "rule_type" not in (mnq_rule.get("metadata") or {})

        response = client.post("/risk/rules", json={"symbol": "ES", "stop_loss_price": 25.0})
        assert response.status_code == 200
        assert "ES" in response.json()["rules"]

        response = client.put("/risk_rules/ES", params={"enabled": "false"})
        assert response.status_code == 200
        assert response.json()["rules"]["ES"]["enabled"] is False

        response = client.delete("/risk/rules/ES")
        assert response.status_code == 200
        assert response.json()["removed"] == "ES"

        response = client.get("/risk/rules")
        assert "ES" not in response.json()["rules"]


def test_create_rule_updates_cache_and_publishes_event() -> None:
    redis = _FakeRedis()
    runtime = StubRuntime(redis)
    client = _build_app(runtime, initial_config={})

    with client:
        response = client.post(
            "/risk/rules",
            json={"symbol": "CL", "stop_loss_price": 12.5},
        )
        assert response.status_code == 200

    cached_rules = json.loads(redis.store[runtime.rules_cache_key])
    assert cached_rules["rules"]["CL"]["stop_loss_price"] == 12.5

    assert redis.published, "Expected a pubsub message for rule update"
    channel, message = redis.published[-1]
    assert channel == runtime.rules_update_channel
    payload = json.loads(message)
    assert payload["type"] == "risk_rules"
    assert payload["rules"]["CL"]["stop_loss_price"] == 12.5
    assert payload["count"] == len(cached_rules["rules"])


def test_position_sizing_uses_config_defaults() -> None:
    redis = _FakeRedis()
    runtime = StubRuntime(redis)
    config = {"risk_per_trade_r": 0.02, "account_equity": 50000, "point_value_map": {"ES": 50}}
    client = _build_app(runtime, initial_config=config)

    with client:
        response = client.post("/risk/position-sizing", json={"symbol": "ES", "stop_distance": 10})
        assert response.status_code == 200
        payload = response.json()
        assert payload["contracts"] == int((50000 * 0.02) / (50 * 10))


def test_position_sizing_validation_error() -> None:
    redis = _FakeRedis()
    runtime = StubRuntime(redis)
    client = _build_app(runtime, initial_config={})

    with client:
        response = client.post("/risk/position-sizing", json={"symbol": "ES", "stop_distance": 0})
        assert response.status_code == 400
        assert "stop_distance" in response.json()["detail"]
