"""Entry point and orchestration helpers for the optimizer service."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel

from src.config import load_project_env
from src.optimizer_service import router as optimizer_router
from src.optimizer_service.metadata import describe_dom_storage, list_dom_symbols

try:  # redis is optional for some test environments
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis optional in CI
    Redis = Any  # type: ignore

from src.db import MariaDBConfigurationError, run_sql_query
from src.ibbot.redis import get_redis_client
from src.ibbot.storage import get_market_data_writer, summarise_dom_directory

OPTIMIZER_APP_TITLE = "IBBot Optimizer"
OPTIMIZER_APP_VERSION = "0.2.0"
DEFAULT_HOST = os.getenv("OPTIMIZER_HOST", "0.0.0.0")
DEFAULT_PORT = 8001

_DOM_WORD_PATTERN = re.compile(r"(?<![A-Za-z0-9])dom(?![A-Za-z0-9])", re.IGNORECASE)
_SYMBOL_KEY_PATTERN = re.compile(r"(symbol|ticker|contract|instrument)", re.IGNORECASE)
_DATE_KEY_PATTERN = re.compile(r"(start|end)[-_]?(date|day|time)", re.IGNORECASE)
_DOM_BOOL_KEYS = {"requires_dom", "use_dom", "dom_required", "dom_enabled"}
_DOM_VALUE_KEYS = {"data_source", "data_type", "mode", "category", "strategy", "strategy_type"}

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


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y%m%d")
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _coerce_symbol(value: Any) -> str | None:
    if isinstance(value, str):
        token = value.strip().upper().split()[0]
        if not token:
            return None
        if re.fullmatch(r"[A-Z][A-Z0-9\.-]{0,15}", token):
            return token
        return None
    if isinstance(value, Mapping):
        for inner in value.values():
            candidate = _coerce_symbol(inner)
            if candidate:
                return candidate
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            candidate = _coerce_symbol(item)
            if candidate:
                return candidate
    return None


def _ns_to_date(ns: int | None) -> date | None:
    if ns is None:
        return None
    try:
        dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except Exception:
        return None
    return dt.date()


def _ns_to_iso(ns: int | None) -> str:
    if ns is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except Exception:
        return ""
    return dt.isoformat()


def _extract_dom_requirements(
    strategy_id: str,
    config: Mapping[str, Any] | None,
) -> tuple[bool, str | None, datetime | None, datetime | None]:
    requires_dom = bool(_DOM_WORD_PATTERN.search(strategy_id or ""))
    symbol: str | None = None
    start_dt: datetime | None = None
    end_dt: datetime | None = None

    def _walk(value: Any, key_hint: str = "") -> None:
        nonlocal requires_dom, symbol, start_dt, end_dt

        if isinstance(value, Mapping):
            for key, inner in value.items():
                key_text = str(key)
                lower_key = key_text.lower()
                if _DOM_WORD_PATTERN.search(key_text):
                    requires_dom = True
                if lower_key in _DOM_BOOL_KEYS:
                    if isinstance(inner, bool) and inner:
                        requires_dom = True
                    elif isinstance(inner, str) and _DOM_WORD_PATTERN.search(inner):
                        requires_dom = True
                if lower_key in _DOM_VALUE_KEYS and isinstance(inner, str):
                    if _DOM_WORD_PATTERN.search(inner):
                        requires_dom = True
                if symbol is None and _SYMBOL_KEY_PATTERN.search(lower_key):
                    candidate = _coerce_symbol(inner)
                    if candidate:
                        symbol = candidate
                if _DATE_KEY_PATTERN.search(lower_key):
                    candidate_dt = _coerce_datetime(inner)
                    if candidate_dt is not None:
                        if "start" in lower_key and start_dt is None:
                            start_dt = candidate_dt
                        elif "end" in lower_key and end_dt is None:
                            end_dt = candidate_dt
                _walk(inner, key_text)
            return

        if isinstance(value, (list, tuple, set)):
            for item in value:
                _walk(item, key_hint)
            return

        lower_hint = key_hint.lower()
        if isinstance(value, bool):
            if lower_hint in _DOM_BOOL_KEYS and value:
                requires_dom = True
            return

        if isinstance(value, str):
            if _DOM_WORD_PATTERN.search(value):
                if not lower_hint or lower_hint in _DOM_VALUE_KEYS or lower_hint in _DOM_BOOL_KEYS:
                    requires_dom = True
            if symbol is None and _SYMBOL_KEY_PATTERN.search(lower_hint):
                candidate = _coerce_symbol(value)
                if candidate:
                    symbol = candidate
            if _DATE_KEY_PATTERN.search(lower_hint):
                candidate_dt = _coerce_datetime(value)
                if candidate_dt is not None:
                    if "start" in lower_hint and start_dt is None:
                        start_dt = candidate_dt
                    elif "end" in lower_hint and end_dt is None:
                        end_dt = candidate_dt
            return

        if _DATE_KEY_PATTERN.search(lower_hint):
            candidate_dt = _coerce_datetime(value)
            if candidate_dt is not None:
                if "start" in lower_hint and start_dt is None:
                    start_dt = candidate_dt
                elif "end" in lower_hint and end_dt is None:
                    end_dt = candidate_dt

    if config:
        _walk(config)

    return requires_dom, symbol, start_dt, end_dt


def _ensure_dom_training_data(
    strategy_id: str,
    config: Mapping[str, Any] | None,
) -> None:
    requires_dom, symbol, start_dt, end_dt = _extract_dom_requirements(strategy_id, config)
    if not requires_dom:
        return

    if not symbol:
        raise OptimizerDataError(
            f"Unable to determine DOM symbol for strategy '{strategy_id}'. Update the strategy configuration with a symbol before running optimization."
        )

    try:
        writer = get_market_data_writer()
    except Exception as exc:  # pragma: no cover - defensive guard
        raise OptimizerDataError(f"Failed to initialise market data storage: {exc}") from exc

    reason = getattr(writer, "_reason", None)
    if reason:
        raise OptimizerDataError(
            f"DOM data storage unavailable ({reason}). Install PyArrow and configure MARKET_DATA_STORAGE_PATH before optimising DOM strategies."
        )

    try:
        summary = summarise_dom_directory(writer.base_dir, symbol)
    except Exception as exc:
        raise OptimizerDataError(f"Failed to inspect DOM storage for {symbol}: {exc}") from exc

    if summary.total_rows <= 0:
        raise OptimizerDataError(
            f"No DOM snapshots found for {symbol}. Run a DOM backfill from the Market Data panel before optimising '{strategy_id}'."
        )

    earliest_date = _ns_to_date(summary.earliest_ns)
    latest_date = _ns_to_date(summary.latest_ns)

    if start_dt and earliest_date and earliest_date > start_dt.date():
        raise OptimizerDataError(
            f"DOM data for {symbol} starts on {earliest_date.isoformat()}, earlier than the optimisation start date {start_dt.date().isoformat()}. Run a backfill to cover the missing range."
        )

    if end_dt and latest_date and latest_date < end_dt.date():
        raise OptimizerDataError(
            f"DOM data for {symbol} ends on {latest_date.isoformat()}, before the optimisation end date {end_dt.date().isoformat()}. Run a backfill to extend the coverage."
        )

    try:
        sample = writer.read_dom(symbol, limit=1)
    except Exception as exc:
        raise OptimizerDataError(f"Failed to read DOM snapshots for {symbol}: {exc}") from exc

    if not sample:
        earliest_iso = _ns_to_iso(summary.earliest_ns)
        latest_iso = _ns_to_iso(summary.latest_ns)
        raise OptimizerDataError(
            f"DOM storage under {writer.base_dir}/dom/{symbol} contains metadata but yielded no snapshots (coverage {earliest_iso or 'unknown'} – {latest_iso or 'unknown'}). Verify the files and rerun backfill."
        )


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

        parsed_config = _parse_mapping(strategy_config)
        _ensure_dom_training_data(submission.strategy_id, parsed_config)

        await redis.xadd(
            EVENT_STREAM_KEY,
            {"type": "task_progress", "task_id": submission.task_id, "progress": "50"},
        )

        training_data = await self._query_database(submission.training_query)

        parsed_params = _parse_mapping(param_space)
        parsed_history = _parse_mapping(history_metrics)

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


def _normalise_symbols(symbols: Sequence[str] | None) -> list[str]:
    if not symbols:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for entry in symbols:
        if not entry:
            continue
        text = entry.strip().upper()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def collect_dom_coverage(
    symbols: Sequence[str] | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if limit is not None:
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            limit_value = 0
    else:
        limit_value = 0

    resolved_limit = limit_value if limit_value > 0 else None

    try:
        available_symbols = list_dom_symbols()
    except RuntimeError as exc:
        raise OptimizerDataError(str(exc)) from exc

    targets = _normalise_symbols(symbols)
    if not targets:
        targets = available_symbols

    results: list[dict[str, Any]] = []
    for symbol in targets:
        try:
            summary = describe_dom_storage(symbol, limit=resolved_limit)
        except RuntimeError as exc:
            results.append({"symbol": symbol, "error": str(exc)})
            continue
        results.append(summary)

    return results


def print_dom_coverage(symbols: Sequence[str] | None, limit: int | None) -> int:
    try:
        coverage = collect_dom_coverage(symbols, limit=limit)
    except OptimizerDataError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(coverage, indent=2, ensure_ascii=False))
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run optimizer FastAPI service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Service host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Service port (default 8001)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    parser.add_argument(
        "--dom-coverage",
        nargs="*",
        metavar="SYMBOL",
        help="Display DOM storage coverage for the provided symbols (or all symbols if omitted) and exit",
    )
    parser.add_argument(
        "--dom-limit",
        type=int,
        default=5,
        help="Maximum number of DOM files to include per symbol when using --dom-coverage (0 = all)",
    )
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

    if args.dom_coverage is not None:
        exit_code = print_dom_coverage(args.dom_coverage, args.dom_limit)
        if exit_code != 0:
            sys.exit(exit_code)
        return
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
