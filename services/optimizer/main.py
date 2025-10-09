"""Entry point and orchestration helpers for the optimizer service."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

try:
    from src.event_loop_manager import setup_event_loop_policy
    _event_loop_manager_available = True
except ImportError:
    _event_loop_manager_available = False

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel

from src.config import load_project_env
from src.optimizer_service import router as optimizer_router

try:  # redis is optional for some test environments
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis optional in CI
    Redis = Any  # type: ignore

from src.db import MariaDBConfigurationError, run_sql_query
from src.ibbot.redis import get_redis_client

OPTIMIZER_APP_TITLE = "IBBot Optimizer"
OPTIMIZER_APP_VERSION = "0.2.0"
DEFAULT_HOST = os.getenv("OPTIMIZER_HOST", "0.0.0.0")
DEFAULT_PORT = 8001

ROOT_DIR = Path(__file__).resolve().parents[3]
OPTIMIZER_UI_DIR = ROOT_DIR / "optimizer_ui"
EVENT_STREAM_KEY = "stream:optimizer:events"
RESULT_KEY_TEMPLATE = "hash:optimizer:result:{task_id}"
CONFIG_KEY_TEMPLATE = "hash:optimizer:strategy:{strategy_id}:config"
PARAM_SPACE_KEY_TEMPLATE = "hash:optimizer:strategy:{strategy_id}:params"
HISTORY_KEY_TEMPLATE = "hash:optimizer:strategy:{strategy_id}:history"


class OptimizerDataError(RuntimeError):
    """Raised when prerequisite data for an optimizer task is unavailable."""


class TaskSubmission(BaseModel):
    """Request payload for submitting a new optimizer task."""

    task_id: str
    strategy_id: str
    training_query: str


class TaskAcceptedResponse(BaseModel):
    """Response returned immediately after submitting a task."""

    task_id: str
    status: str = "accepted"


class TaskResultResponse(BaseModel):
    """Response model for optimizer task results."""

    task_id: str
    status: str
    best_params: Mapping[str, Any]
    metrics: Mapping[str, Any]
    samples: int


def _stringify_mapping(payload: Mapping[str, Any]) -> Mapping[str, str]:
    """Convert arbitrary values to Redis-friendly string payloads."""

    result: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, (dict, list, tuple)):
            result[key] = json.dumps(value)
        elif isinstance(value, float):
            result[key] = f"{value:.6f}"
        else:
            result[key] = str(value)
    return result


def _parse_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Attempt to JSON-deserialise Redis payloads while keeping strings intact."""

    parsed: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                parsed[key] = value
                continue
            try:
                parsed[key] = json.loads(raw)
                continue
            except json.JSONDecodeError:
                parsed[key] = value
        else:
            parsed[key] = value
    return parsed


def _first_value(sequence: Sequence[Any]) -> Any:
    return sequence[0] if sequence else None


def _compute_score(training_data: Iterable[Any]) -> float:
    values: list[float] = []
    for item in training_data:
        if isinstance(item, Mapping) and "value" in item:
            try:
                values.append(float(item["value"]))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, (int, float)):
            values.append(float(item))
    if not values:
        return 0.0
    return sum(values) / len(values)


@dataclass
class OptimizerService:
    """Encapsulates access to Redis and MariaDB for optimizer tasks."""

    redis_factory: Callable[[], Awaitable[Redis] | Redis] = get_redis_client
    db_query: Callable[[str], Awaitable[Iterable[Any]] | Iterable[Any]] = run_sql_query

    async def _get_redis(self) -> Redis:
        instance = self.redis_factory()
        if inspect.isawaitable(instance):
            return await instance  # type: ignore[return-value]
        return instance  # type: ignore[return-value]

    async def _query_database(self, query: str) -> list[Any]:
        try:
            result = self.db_query(query)
        except MariaDBConfigurationError as exc:
            raise OptimizerDataError(f"Database configuration error: {exc}") from exc
        except Exception as exc:
            raise OptimizerDataError(f"Failed to load training data: {exc}") from exc

        if inspect.isawaitable(result):
            result = await result  # type: ignore[assignment]

        if isinstance(result, Sequence):
            return list(result)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: list(result))

    async def submit_task(self, submission: TaskSubmission) -> TaskResultResponse:
        redis = await self._get_redis()
        await redis.xadd(
            EVENT_STREAM_KEY,
            {"type": "task_submitted", "task_id": submission.task_id, "progress": "0"},
        )

        config_key = CONFIG_KEY_TEMPLATE.format(strategy_id=submission.strategy_id)
        params_key = PARAM_SPACE_KEY_TEMPLATE.format(strategy_id=submission.strategy_id)
        history_key = HISTORY_KEY_TEMPLATE.format(strategy_id=submission.strategy_id)

        strategy_config = await redis.hgetall(config_key)
        param_space = await redis.hgetall(params_key)
        history_metrics = await redis.hgetall(history_key)

        if not strategy_config or not param_space or not history_metrics:
            raise OptimizerDataError(
                f"Missing optimizer prerequisites for strategy {submission.strategy_id}"
            )

        await redis.xadd(
            EVENT_STREAM_KEY,
            {"type": "task_progress", "task_id": submission.task_id, "progress": "50"},
        )

        training_data = await self._query_database(submission.training_query)

        parsed_params = _parse_mapping(param_space)
        parsed_history = _parse_mapping(history_metrics)
        parsed_config = _parse_mapping(strategy_config)

        best_params: dict[str, Any] = {}
        for key, value in parsed_params.items():
            if isinstance(value, list):
                best_params[key] = _first_value(value)
            else:
                best_params[key] = value

        score = _compute_score(training_data)
        baseline = float(parsed_history.get("sharpe", 0) or 0)

        result_payload = TaskResultResponse(
            task_id=submission.task_id,
            status="completed",
            best_params=best_params,
            metrics={
                "score": score,
                "baseline_sharpe": baseline,
                "config": parsed_config,
            },
            samples=len(training_data),
        )

        redis_payload = _stringify_mapping(
            {
                "status": result_payload.status,
                "best_params": result_payload.best_params,
                "metrics": result_payload.metrics,
                "samples": result_payload.samples,
            }
        )
        await redis.hset(RESULT_KEY_TEMPLATE.format(task_id=submission.task_id), mapping=redis_payload)

        await redis.xadd(
            EVENT_STREAM_KEY,
            {"type": "task_completed", "task_id": submission.task_id, "progress": "100"},
        )

        return result_payload

    async def get_result(self, task_id: str) -> TaskResultResponse:
        redis = await self._get_redis()
        payload = await redis.hgetall(RESULT_KEY_TEMPLATE.format(task_id=task_id))
        if not payload:
            raise OptimizerDataError(f"Result for task {task_id} not found")
        parsed = _parse_mapping(payload)
        return TaskResultResponse(
            task_id=task_id,
            status=parsed.get("status", "unknown"),
            best_params=parsed.get("best_params", {}),
            metrics=parsed.get("metrics", {}),
            samples=int(parsed.get("samples", 0) or 0),
        )


def ensure_optimizer_ui_dir() -> None:
    try:
        OPTIMIZER_UI_DIR.mkdir(parents=True, exist_ok=True)
        (OPTIMIZER_UI_DIR / "templates").mkdir(parents=True, exist_ok=True)
        (OPTIMIZER_UI_DIR / "static").mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unable to create optimizer UI directory: {}", exc)


def create_app(*, service: OptimizerService | None = None) -> FastAPI:
    load_project_env()
    ensure_optimizer_ui_dir()

    optimizer_service = service or OptimizerService()

    app = FastAPI(title=OPTIMIZER_APP_TITLE, version=OPTIMIZER_APP_VERSION)
    templates_dir = OPTIMIZER_UI_DIR / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    static_dir = OPTIMIZER_UI_DIR / "static"
    if static_dir.exists():
        app.mount("/optimizer-static", StaticFiles(directory=str(static_dir)), name="optimizer_static")

    app.include_router(optimizer_router)

    router = APIRouter(prefix="/api/optimizer", tags=["optimizer-service"])

    def get_service() -> OptimizerService:
        return optimizer_service

    @router.post("/tasks", response_model=TaskAcceptedResponse)
    async def submit_task_endpoint(
        submission: TaskSubmission, service: OptimizerService = Depends(get_service)
    ) -> TaskAcceptedResponse:
        try:
            await service.submit_task(submission)
        except OptimizerDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TaskAcceptedResponse(task_id=submission.task_id)

    @router.get("/tasks/{task_id}/result", response_model=TaskResultResponse)
    async def get_result_endpoint(
        task_id: str, service: OptimizerService = Depends(get_service)
    ) -> TaskResultResponse:
        try:
            return await service.get_result(task_id)
        except OptimizerDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    app.include_router(router)

    @app.get("/", response_class=HTMLResponse)
    async def optimizer_index(request: Request):
        template_path = templates_dir / "index.html"
        if not template_path.exists():
            return HTMLResponse("<h3>Optimizer UI is under construction.</h3>", status_code=200)
        return templates.TemplateResponse("index.html", {"request": request})

    @app.get("/compare", response_class=HTMLResponse)
    async def optimizer_compare(request: Request):
        template_path = templates_dir / "compare.html"
        if not template_path.exists():
            return HTMLResponse("<h3>横评页面暂未配置。</h3>", status_code=200)
        return templates.TemplateResponse("compare.html", {"request": request})

    return app


app = create_app()


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run optimizer FastAPI service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Service host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Service port (default 8001)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    return parser.parse_args(argv)


def configure_logging() -> None:
    try:
        logger.remove()
    except ValueError:
        pass
    logger.add(lambda msg: print(msg, end=""), level="INFO")


def main() -> None:
    args = parse_args()
    configure_logging()
    
    # 设置事件循环策略以提高兼容性
    if _event_loop_manager_available:
        try:
            setup_event_loop_policy()
            logger.info("Optimizer service: 事件循环策略设置成功")
        except Exception as e:
            logger.warning(f"Optimizer service: 事件循环策略设置失败: {e}")
    
    logger.info(
        "Starting optimizer service on http://{}:{} (reload={})",
        args.host,
        args.port,
        "on" if args.reload else "off",
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
