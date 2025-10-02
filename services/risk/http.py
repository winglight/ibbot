"""FastAPI application exposing the risk runtime HTTP surface."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status

from services.risk.runtime import RiskRuntime
from src.ibbot import update_service_health
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import close_global_client, get_redis_client

SERVICE_NAME = "risk-service"


def _json_error(detail: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


async def _read_json_body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-length") in (None, "", "0"):
        return {}
    body = await request.body()
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive guard
        raise _json_error("Invalid JSON payload") from exc


def create_app(runtime: RiskRuntime | None = None) -> FastAPI:
    logger = get_logger(SERVICE_NAME)
    runtime_instance: RiskRuntime | None = runtime
    provider: ConfigProvider | None = None
    redis_client: Any | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal runtime_instance, provider, redis_client

        redis_client = await get_redis_client()

        async def redis_factory() -> Any:
            return redis_client

        if runtime_instance is None:
            runtime_instance = RiskRuntime(
                redis_factory=redis_factory,
                logger_name=SERVICE_NAME,
                service_name=SERVICE_NAME,
            )
        else:
            runtime_instance.redis_factory = redis_factory  # type: ignore[assignment]

        provider = ConfigProvider(redis_client=redis_client, hash_prefix="hash:config")
        await provider.start()
        initial_config = await provider.get_module(SERVICE_NAME)
        await runtime_instance.update_config(initial_config)

        async def _config_listener(_: str, payload: Mapping[str, Any]) -> None:
            await runtime_instance.update_config(payload)

        provider.subscribe(SERVICE_NAME, _config_listener)

        await runtime_instance.start()

        try:
            yield
        finally:
            if runtime_instance is not None:
                await runtime_instance.stop()
            if provider is not None:
                await provider.close()
                provider = None
            await close_global_client()

    app = FastAPI(title="Risk Service", version="1.0.0", lifespan=lifespan)

    async def get_runtime() -> RiskRuntime:
        if runtime_instance is None:
            raise HTTPException(status_code=503, detail="Risk runtime unavailable")
        return runtime_instance

    @app.middleware("http")
    async def health_middleware(request: Request, call_next):  # type: ignore[override]
        runtime_obj = runtime_instance
        if runtime_obj is None:
            raise HTTPException(status_code=503, detail="Risk runtime unavailable")
        try:
            response = await call_next(request)
            await runtime_obj.report_health(
                healthy=True,
                extra_metrics={"endpoint": request.url.path, "state": "serving"},
            )
            return response
        except Exception as exc:
            await runtime_obj.report_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={"endpoint": request.url.path, "state": "error"},
            )
            raise

    async def _wrap_runtime_call(coro):
        try:
            return await coro
        except KeyError as exc:
            raise _json_error(f"Rule {exc.args[0]} not found", status.HTTP_404_NOT_FOUND) from exc
        except ValueError as exc:
            raise _json_error(str(exc), status.HTTP_400_BAD_REQUEST) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/risk_rules", status_code=status.HTTP_200_OK)
    async def list_risk_rules(runtime: RiskRuntime = Depends(get_runtime)) -> Mapping[str, Any]:
        return await runtime.fetch_rules()

    @app.post("/risk_rules", status_code=status.HTTP_200_OK)
    async def create_risk_rule(
        request: Request,
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        payload = await _read_json_body(request)
        return await _wrap_runtime_call(runtime.add_rule(payload))

    @app.put("/risk_rules/{symbol}", status_code=status.HTTP_200_OK)
    async def update_risk_rule(
        symbol: str,
        request: Request,
        enabled: bool | None = Query(None),
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        payload = await _read_json_body(request)
        if enabled is not None:
            payload["enabled"] = enabled
        return await _wrap_runtime_call(runtime.update_rule(symbol, payload))

    @app.delete("/risk_rules/{symbol}", status_code=status.HTTP_200_OK)
    async def delete_risk_rule(
        symbol: str,
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        return await _wrap_runtime_call(runtime.delete_rule(symbol))

    @app.get("/risk-config", status_code=status.HTTP_200_OK)
    async def read_risk_config(runtime: RiskRuntime = Depends(get_runtime)) -> Mapping[str, Any]:
        return await runtime.fetch_config()

    @app.put("/risk-config", status_code=status.HTTP_200_OK)
    async def update_risk_config(
        request: Request,
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        payload = await _read_json_body(request)
        if not payload:
            raise _json_error("Update payload cannot be empty")
        return await _wrap_runtime_call(runtime.update_config_values(payload))

    @app.get("/order_queue_status", status_code=status.HTTP_200_OK)
    async def order_queue_status(runtime: RiskRuntime = Depends(get_runtime)) -> Mapping[str, Any]:
        return await runtime.fetch_order_queue_status()

    @app.post("/position-sizing", status_code=status.HTTP_200_OK)
    async def calculate_position_sizing(
        request: Request,
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        payload = await _read_json_body(request)
        if not payload:
            raise _json_error("Position sizing payload is required")
        return await _wrap_runtime_call(runtime.position_sizing(payload))

    @app.get("/sizing/history", status_code=status.HTTP_200_OK)
    async def sizing_history(
        symbol: str | None = None,
        since: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        return await runtime.fetch_sizing_history(symbol=symbol, since=since, limit=limit)

    @app.get("/config/rules", status_code=status.HTTP_200_OK)
    async def get_config_rules(runtime: RiskRuntime = Depends(get_runtime)) -> Mapping[str, Any]:
        return await runtime.fetch_config_rules()

    @app.put("/config/rules", status_code=status.HTTP_200_OK)
    async def put_config_rules(
        request: Request,
        runtime: RiskRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        payload = await _read_json_body(request)
        return await _wrap_runtime_call(runtime.update_config_rules(payload))

    @app.get("/health", status_code=status.HTTP_200_OK)
    async def health_check() -> Mapping[str, Any]:
        metrics = {
            "service": SERVICE_NAME,
            "healthy": runtime_instance is not None,
        }
        if redis_client is not None:
            await update_service_health(
                redis_client,
                SERVICE_NAME,
                healthy=True,
                metrics=metrics,
                logger=logger,
            )
        return {"status": "ok", "metrics": metrics}

    return app


app = create_app()
