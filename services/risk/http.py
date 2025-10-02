"""HTTP service exposing risk configuration and analytics endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, status

from services.risk import RiskRuntime
from src.ibbot import update_service_health
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import close_global_client, get_redis_client

SERVICE_NAME = "risk-service"
DEFAULT_HEALTH_INTERVAL = 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_symbol(value: str | None) -> str:
    return value.strip().upper() if value else ""


def _strip_metadata(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"updated_at", "source", "success", "count"}:
            continue
        result[str(key)] = value
    return result


def _merge_rule(existing: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update({k: v for k, v in updates.items() if v is not None})
    symbol = _normalise_symbol(str(merged.get("symbol") or ""))
    if not symbol:
        raise ValueError("risk rule requires a symbol")
    merged["symbol"] = symbol
    return merged


def _iter_rule_payloads(payload: Any) -> Iterable[Mapping[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        rules_candidate = payload.get("rules")
        if isinstance(rules_candidate, (Mapping, list, tuple, set)):
            return _iter_rule_payloads(rules_candidate)
        items: list[Mapping[str, Any]] = []
        for key, value in payload.items():
            if isinstance(value, Mapping):
                rule_payload = dict(value)
                rule_payload.setdefault("symbol", key)
                items.append(rule_payload)
        return items
    if isinstance(payload, (list, tuple, set)):
        items = []
        for item in payload:
            if isinstance(item, Mapping):
                items.append(dict(item))
        return items
    raise ValueError("rules payload must be a mapping or iterable of objects")


async def _load_json(redis: Any, key: str) -> dict[str, Any] | None:
    try:
        raw = await redis.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return dict(payload)


async def _publish_health(app: FastAPI, *, healthy: bool = True) -> None:
    redis = getattr(app.state, "redis", None)
    runtime: RiskRuntime | None = getattr(app.state, "runtime", None)
    if redis is None or runtime is None:
        return

    metrics = runtime.health_snapshot()
    metrics["pending_tasks"] = getattr(app.state, "pending_tasks", 0)
    metrics["last_processed_at"] = _utc_now().isoformat()

    try:
        await update_service_health(
            redis,
            SERVICE_NAME,
            healthy=healthy,
            metrics=metrics,
        )
    except Exception:
        logger = get_logger(SERVICE_NAME)
        logger.debug("Failed to publish risk service health", exc_info=True)


async def _health_loop(app: FastAPI, interval: float) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            await _publish_health(app)
    except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
        raise


@asynccontextmanager
async def _lifespan(
    app: FastAPI,
    *,
    runtime_factory: Callable[[Any], RiskRuntime],
    initial_config: Mapping[str, Any] | None = None,
    health_interval: float = DEFAULT_HEALTH_INTERVAL,
    redis_factory: Callable[[], Any] | None = None,
    use_provider: bool = True,
) -> AsyncIterator[None]:
    logger = get_logger(SERVICE_NAME)
    if redis_factory is None:
        redis = await get_redis_client()
        owns_redis = True
    else:
        candidate = redis_factory()
        redis = await candidate if asyncio.iscoroutine(candidate) else candidate
        owns_redis = False
    provider: ConfigProvider | None = None
    if use_provider:
        provider = ConfigProvider(redis_client=redis, hash_prefix="hash:config")
        await provider.start()

    try:
        config_payload = dict(initial_config or {})
        if not config_payload and provider is not None:
            try:
                config_payload = dict(await provider.get_module(SERVICE_NAME))
            except Exception as exc:
                logger.warning("Failed to load initial risk config: %s", exc)
                config_payload = {}

        runtime = runtime_factory(redis)
        await runtime.update_config(config_payload)
        await runtime.start()

        app.state.redis = redis
        app.state.runtime = runtime
        app.state.config_provider = provider
        app.state.pending_tasks = 0
        app.state.health_task = asyncio.create_task(
            _health_loop(app, max(0.1, float(health_interval)))
        )

        await _publish_health(app)
        yield
    finally:
        task = getattr(app.state, "health_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        runtime = getattr(app.state, "runtime", None)
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.stop()
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
        if owns_redis:
            with contextlib.suppress(Exception):
                await close_global_client()


def create_app(
    *,
    runtime: RiskRuntime | None = None,
    initial_config: Mapping[str, Any] | None = None,
    health_interval: float = DEFAULT_HEALTH_INTERVAL,
    redis_factory: Callable[[], Any] | None = None,
    use_provider: bool = True,
) -> FastAPI:
    app = FastAPI(
        title="Risk Service",
        lifespan=lambda app: _lifespan(
            app,
            runtime_factory=lambda redis: runtime
            or RiskRuntime(
                redis_factory=lambda: redis,
                logger_name=SERVICE_NAME,
                service_name=SERVICE_NAME,
            ),
            initial_config=initial_config,
            health_interval=health_interval,
            redis_factory=redis_factory,
            use_provider=use_provider,
        ),
    )

    @app.middleware("http")
    async def _health_middleware(request: Request, call_next):  # type: ignore[override]
        try:
            response = await call_next(request)
        except Exception:
            await _publish_health(app, healthy=False)
            raise
        await _publish_health(app)
        return response

    async def _get_runtime() -> RiskRuntime:
        runtime_instance: RiskRuntime | None = getattr(app.state, "runtime", None)
        if runtime_instance is None:
            raise HTTPException(status_code=503, detail="Risk runtime unavailable")
        return runtime_instance

    async def _get_redis() -> Any:
        redis_client = getattr(app.state, "redis", None)
        if redis_client is None:
            raise HTTPException(status_code=503, detail="Redis unavailable")
        return redis_client

    async def _refresh_runtime(
        runtime_instance: RiskRuntime,
        *,
        config_updates: Mapping[str, Any] | None = None,
        rule_updates: dict[str, Mapping[str, Any]] | None = None,
        replace_rules: bool = False,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        config_snapshot = runtime_instance.get_config_snapshot()
        rules_snapshot = runtime_instance.get_rules_snapshot()
        if config_updates:
            config_snapshot.update({k: v for k, v in config_updates.items() if v is not None})
        if rule_updates is not None:
            if replace_rules:
                rules_snapshot = {k: dict(v) for k, v in rule_updates.items()}
            else:
                rules_snapshot.update({k: dict(v) for k, v in rule_updates.items()})
        payload = dict(config_snapshot)
        payload["rules"] = list(rules_snapshot.values())
        await runtime_instance.update_config(payload)
        updated_config = runtime_instance.get_config_snapshot()
        updated_rules = runtime_instance.get_rules_snapshot()
        return dict(updated_config), {k: dict(v) for k, v in updated_rules.items()}

    @app.get("/risk/config", status_code=status.HTTP_200_OK)
    @app.get("/risk-config", status_code=status.HTTP_200_OK)
    async def get_config(redis = Depends(_get_redis), runtime_instance: RiskRuntime = Depends(_get_runtime)) -> dict[str, Any]:  # type: ignore[override]
        cached = await _load_json(redis, runtime_instance.config_cache_key)
        config = _strip_metadata(cached) or runtime_instance.get_config_snapshot()
        response: dict[str, Any] = {"success": True, "config": dict(config)}
        response.update(config)
        if cached:
            response.update({k: v for k, v in cached.items() if k not in config})
        response.setdefault("updated_at", _utc_now().isoformat())
        return response

    @app.post("/risk/config", status_code=status.HTTP_200_OK)
    @app.put("/risk/config", status_code=status.HTTP_200_OK)
    @app.post("/risk-config", status_code=status.HTTP_200_OK)
    @app.put("/risk-config", status_code=status.HTTP_200_OK)
    async def update_config(
        payload: Mapping[str, Any],
        runtime_instance: RiskRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        updates = {str(k): v for k, v in payload.items()}
        try:
            config_snapshot, _ = await _refresh_runtime(
                runtime_instance, config_updates=updates
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to update risk config") from exc
        response: dict[str, Any] = {"success": True, "config": dict(config_snapshot)}
        response.update(config_snapshot)
        return response

    @app.get("/risk/rules", status_code=status.HTTP_200_OK)
    @app.get("/risk_rules", status_code=status.HTTP_200_OK)
    async def get_rules(redis = Depends(_get_redis), runtime_instance: RiskRuntime = Depends(_get_runtime)) -> dict[str, Any]:  # type: ignore[override]
        cached = await _load_json(redis, runtime_instance.rules_cache_key)
        rules_payload = {}
        if cached and isinstance(cached.get("rules"), Mapping):
            rules_payload = {
                _normalise_symbol(str(symbol)): dict(value)
                for symbol, value in cached["rules"].items()
                if isinstance(value, Mapping)
            }
        if not rules_payload:
            rules_payload = runtime_instance.get_rules_snapshot()
        response: dict[str, Any] = {
            "success": True,
            "rules": rules_payload,
            "count": len(rules_payload),
        }
        if cached:
            response.update({k: v for k, v in cached.items() if k not in response})
        return response

    async def _upsert_rule(
        symbol: str,
        updates: Mapping[str, Any],
        runtime_instance: RiskRuntime,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        symbol_normalised = _normalise_symbol(symbol)
        rules_snapshot = runtime_instance.get_rules_snapshot()
        existing = rules_snapshot.get(symbol_normalised, {"symbol": symbol_normalised})
        merged = _merge_rule(existing, updates)
        _, rules_snapshot = await _refresh_runtime(
            runtime_instance, rule_updates={symbol_normalised: merged}
        )
        rule_payload = rules_snapshot.get(symbol_normalised, dict(merged))
        return dict(rule_payload), rules_snapshot

    @app.post("/risk/rules", status_code=status.HTTP_200_OK)
    @app.post("/risk_rules", status_code=status.HTTP_200_OK)
    async def add_rule(
        payload: Mapping[str, Any],
        runtime_instance: RiskRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            rule, rules_snapshot = await _upsert_rule(
                str(payload.get("symbol")), payload, runtime_instance
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "success": True,
            "rule": rule,
            "rules": rules_snapshot,
        }

    @app.put("/risk/rules/{symbol}", status_code=status.HTTP_200_OK)
    @app.put("/risk_rules/{symbol}", status_code=status.HTTP_200_OK)
    async def update_rule(
        symbol: str,
        request: Request,
        enabled: bool | None = None,
        runtime_instance: RiskRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        body: Mapping[str, Any] | None = None
        if request.headers.get("content-length") and int(request.headers["content-length"]) > 0:
            body = await request.json()
        updates: dict[str, Any] = dict(body or {})
        if enabled is not None:
            updates["enabled"] = enabled
        try:
            rule, rules_snapshot = await _upsert_rule(
                symbol, updates, runtime_instance
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "success": True,
            "rule": rule,
            "rules": rules_snapshot,
        }

    @app.delete("/risk/rules/{symbol}", status_code=status.HTTP_200_OK)
    @app.delete("/risk_rules/{symbol}", status_code=status.HTTP_200_OK)
    async def delete_rule(
        symbol: str,
        runtime_instance: RiskRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        symbol_normalised = _normalise_symbol(symbol)
        rules_snapshot = runtime_instance.get_rules_snapshot()
        if symbol_normalised not in rules_snapshot:
            raise HTTPException(status_code=404, detail="risk rule not found")
        rules_snapshot.pop(symbol_normalised)
        _, updated_rules = await _refresh_runtime(
            runtime_instance,
            rule_updates=rules_snapshot,
            replace_rules=True,
        )
        return {
            "success": True,
            "removed": symbol_normalised,
            "rules": updated_rules,
        }

    @app.post("/risk/position-sizing", status_code=status.HTTP_200_OK)
    @app.post("/position-sizing", status_code=status.HTTP_200_OK)
    async def position_sizing(
        payload: Mapping[str, Any],
        runtime_instance: RiskRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime_instance.position_sizing(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to calculate position sizing") from exc

    @app.get("/order_queue_status", status_code=status.HTTP_200_OK)
    async def order_queue_status(runtime_instance: RiskRuntime = Depends(_get_runtime)) -> dict[str, Any]:
        try:
            return await runtime_instance.fetch_order_queue_status()
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to load order queue status") from exc

    @app.get("/sizing/history", status_code=status.HTTP_200_OK)
    async def sizing_history(
        runtime_instance: RiskRuntime = Depends(_get_runtime),
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            limit_value = int(limit)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="limit must be an integer") from exc
        try:
            return await runtime_instance.fetch_sizing_history(
                symbol=symbol,
                since=since,
                limit=limit_value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to load sizing history") from exc

    @app.get("/config/rules", status_code=status.HTTP_200_OK)
    async def get_config_rules(runtime_instance: RiskRuntime = Depends(_get_runtime)) -> dict[str, Any]:
        rules_snapshot = runtime_instance.get_rules_snapshot()
        return {
            "success": True,
            "rules": rules_snapshot,
            "count": len(rules_snapshot),
        }

    @app.put("/config/rules", status_code=status.HTTP_200_OK)
    async def update_config_rules(
        payload: Mapping[str, Any],
        runtime_instance: RiskRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            rule_payloads = list(_iter_rule_payloads(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updates: dict[str, Mapping[str, Any]] = {}
        for rule_payload in rule_payloads:
            merged = _merge_rule({"symbol": rule_payload.get("symbol")}, rule_payload)
            updates[merged["symbol"]] = merged
        try:
            _, rules_snapshot = await _refresh_runtime(
                runtime_instance,
                rule_updates=updates,
                replace_rules=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to update risk rules") from exc
        return {
            "success": True,
            "rules": rules_snapshot,
            "count": len(rules_snapshot),
        }

    @app.get("/risk/analytics/health", status_code=status.HTTP_200_OK)
    async def analytics_health(runtime_instance: RiskRuntime = Depends(_get_runtime)) -> dict[str, Any]:
        metrics = runtime_instance.health_snapshot()
        metrics["service"] = SERVICE_NAME
        metrics["timestamp"] = _utc_now().isoformat()
        return metrics

    return app


app = create_app()

