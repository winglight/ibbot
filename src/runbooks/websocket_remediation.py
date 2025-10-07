"""Runbook implementation for the WebSocket data remediation workflow."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Mapping, Optional
from urllib.parse import urlparse, urlunparse

from loguru import logger

from src.db import DatabaseConfigError, MariaDBConfigurationError, get_database_url
from src.data_layer import DataSourceError, DataSubscriptionRequest
from src.data_layer.bootstrap import get_data_source_manager
from src.dom_service import get_dom_service
from src.ibbot.storage import get_market_data_writer, summarise_dom_directory
from src.utils import to_local_iso

StepStatus = Literal["success", "warning", "error"]
TaskStatus = Literal["pending", "running", "success", "warning", "error"]
RunbookStatus = Literal["idle", "running", "success", "warning", "error"]


@dataclass(slots=True)
class StepResult:
    status: StepStatus
    details: Dict[str, Any]


@dataclass(slots=True)
class RunbookStep:
    id: str
    title: str
    description: str
    runner: Callable[[], Awaitable[StepResult]]


@dataclass(slots=True)
class RunbookTaskState:
    id: str
    title: str
    description: str
    status: TaskStatus = "pending"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None

    def clone(self) -> "RunbookTaskState":
        return RunbookTaskState(
            id=self.id,
            title=self.title,
            description=self.description,
            status=self.status,
            started_at=self.started_at,
            completed_at=self.completed_at,
            details=None if self.details is None else dict(self.details),
        )


@dataclass(slots=True)
class RunbookState:
    status: RunbookStatus = "idle"
    run_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    tasks: List[RunbookTaskState] = field(default_factory=list)

    def clone(self) -> "RunbookState":
        return RunbookState(
            status=self.status,
            run_id=self.run_id,
            started_at=self.started_at,
            completed_at=self.completed_at,
            tasks=[task.clone() for task in self.tasks],
        )


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _parse_positive_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(parsed, 0)


def _parse_positive_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(parsed, 0.0)


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ns_to_iso_local(ns: Optional[int]) -> Optional[str]:
    if ns is None:
        return None
    try:
        dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except Exception:
        return None
    return to_local_iso(dt) or dt.isoformat()


def _ms_to_iso_local(ms: Optional[float | int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except Exception:
        return None
    return to_local_iso(dt) or dt.isoformat()


def _normalize_redis_endpoint(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover - defensive parsing
        return url
    host = parsed.hostname or ""
    port = parsed.port or 6379
    path = parsed.path.lstrip("/") or "0"
    return f"{host}:{port}/{path}"


def _redact_database_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover - defensive parsing
        return url
    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username = userinfo.split(":", 1)[0]
            userinfo = f"{username}:***"
        elif userinfo:
            userinfo = f"{userinfo}:***"
        netloc = f"{userinfo}@{host}" if userinfo else host
    return urlunparse(parsed._replace(netloc=netloc))


async def _redis_configuration_step() -> StepResult:
    names = [
        "IBBOT_REDIS_URL",
        "ACCOUNT_CACHE_REDIS_URL",
        "DATA_LAYER_REDIS_URL",
        "STRATEGY_REDIS_URL",
        "ORDERS_REDIS_URL",
    ]
    endpoints = {name: _env(name) for name in names}
    normalized = {name: _normalize_redis_endpoint(value) for name, value in endpoints.items() if value}
    missing = [name for name, value in endpoints.items() if not value]
    unique = sorted({value for value in normalized.values() if value})
    if missing:
        message = "缺少 Redis 连接配置：" + ", ".join(missing)
        status: StepStatus = "warning"
    elif len(unique) <= 1:
        message = "所有 Redis 连接指向同一实例。"
        status = "success"
    else:
        message = "检测到多个 Redis 实例，请确认切换数据库时保持一致。"
        status = "warning"
    details = {
        "message": message,
        "configured": endpoints,
        "resolved": normalized,
        "unique_instances": unique,
    }
    return StepResult(status=status, details=details)


async def _snapshot_refresh_step() -> StepResult:
    refresh_raw = _env("ACCOUNT_SNAPSHOT_REFRESH_INTERVAL_SECONDS")
    check_raw = _env("ACCOUNT_SNAPSHOT_CHECK_INTERVAL_SECONDS")
    refresh_interval = _parse_positive_int(refresh_raw)
    check_interval = _parse_positive_int(check_raw)
    recommended = 15
    details = {
        "message": "",
        "refresh_interval": refresh_interval,
        "check_interval": check_interval,
        "recommended_refresh_interval": recommended,
    }
    if refresh_raw is None:
        details["message"] = "未设置刷新间隔，默认值较大，请在环境变量中显式配置为 15 秒。"
        status: StepStatus = "warning"
    elif refresh_interval is None:
        details["message"] = "刷新间隔配置无法解析，请确认为整数。"
        status = "error"
    elif refresh_interval > recommended:
        details["message"] = f"当前刷新间隔为 {refresh_interval} 秒，高于推荐值。"
        status = "warning"
    else:
        details["message"] = "刷新间隔已满足 15 秒要求。"
        status = "success"
    return StepResult(status=status, details=details)


async def _strategy_service_step() -> StepResult:
    redis_url = _env("STRATEGY_REDIS_URL")
    metadata_prefix = _env("STRATEGY_METADATA_REDIS_PREFIX", "json:strategy")
    health_key = _env("STRATEGY_SERVICE_HEALTH_KEY", "hash:service:health:strategy-service")
    issues: List[str] = []
    if not redis_url:
        fallback = _env("IBBOT_REDIS_URL")
        if fallback:
            issues.append("STRATEGY_REDIS_URL 未设置，将回退使用 IBBOT_REDIS_URL。")
        else:
            issues.append("未检测到策略服务 Redis 配置，请设置 STRATEGY_REDIS_URL。")
    if metadata_prefix == "json:strategy":
        issues.append("仍在使用默认元数据前缀，请确认索引是否存在或需要重新同步。")
    details = {
        "message": issues[0] if issues else "策略服务配置看起来正常。",
        "redis_url": redis_url,
        "metadata_prefix": metadata_prefix,
        "health_key": health_key,
        "notes": issues,
    }
    status: StepStatus = "warning" if issues else "success"
    return StepResult(status=status, details=details)


async def _orders_snapshot_step() -> StepResult:
    snapshot_key = _env("ACCOUNT_ORDERS_SNAPSHOT_KEY", "json:orders:snapshot")
    command_method = _env("ACCOUNT_ORDERS_COMMAND", "get_orders")
    timeout_raw = _env("ACCOUNT_ORDERS_COMMAND_TIMEOUT")
    timeout_value = _parse_positive_float(timeout_raw) if timeout_raw is not None else None
    details = {
        "snapshot_key": snapshot_key,
        "command_method": command_method,
        "command_timeout": timeout_value,
        "message": "",
        "notes": [],
    }
    if timeout_raw is not None and timeout_value is None:
        details["message"] = "订单指令超时时间配置无效，请填写数字。"
        status: StepStatus = "error"
    elif snapshot_key == "json:orders:snapshot":
        note = "仍在使用默认快照键，切换数据库后请确认是否需要清理陈旧缓存。"
        details["message"] = note
        details["notes"].append(note)
        status = "warning"
    else:
        details["message"] = "订单快照配置已准备就绪。"
        status = "success"
    return StepResult(status=status, details=details)


async def _log_path_step() -> StepResult:
    log_path = _env("LOG_FILE_PATH")
    if not log_path:
        message = "未配置 LOG_FILE_PATH，日志面板无法提供具体错误指引。"
        status: StepStatus = "warning"
        exists = False
    else:
        path = Path(log_path)
        exists = path.exists()
        if exists:
            message = "日志文件存在，可直接读取。"
            status = "success"
        else:
            message = "日志文件不存在，请确认路径或部署机器上的挂载情况。"
            status = "warning"
    details = {
        "message": message,
        "log_path": log_path,
        "exists": exists,
    }
    return StepResult(status=status, details=details)


async def _database_consistency_step() -> StepResult:
    try:
        database_url = get_database_url()
    except MariaDBConfigurationError as exc:
        message = f"数据库配置缺失：{exc}"
        status: StepStatus = "error"
        database_url = None
    except DatabaseConfigError as exc:
        message = f"数据库配置不完整：{exc}"
        status = "warning"
        database_url = None
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unexpected database configuration error")
        message = f"解析数据库配置失败：{exc}"
        status = "error"
        database_url = None
    else:
        message = "成功解析数据库连接配置。"
        status = "success"
    details = {
        "message": message,
        "database_url": _redact_database_url(database_url),
    }
    return StepResult(status=status, details=details)


async def _realtime_price_step() -> StepResult:
    symbol = (_env("RUNBOOK_SYMBOL_TICK", "ES") or "ES").strip().upper()
    wait_seconds = _parse_positive_float(_env("RUNBOOK_MARKET_WAIT_SECONDS")) or 3.0

    try:
        manager = get_data_source_manager()
    except Exception as exc:
        return StepResult(
            status="error",
            details={"message": f"数据层初始化失败：{exc}"},
        )

    request = DataSubscriptionRequest(channel="market.tick", symbol=symbol)

    try:
        snapshot = await manager.snapshot(request)
    except DataSourceError as exc:
        return StepResult(
            status="error",
            details={"message": f"读取快照失败：{exc}", "symbol": symbol},
        )

    details: Dict[str, Any] = {
        "symbol": symbol,
        "wait_seconds": wait_seconds,
    }

    snapshot_ts = _coerce_int((snapshot or {}).get("ts_ns") or (snapshot or {}).get("timestamp_ns"))
    details["snapshot"] = {
        "timestamp": _ns_to_iso_local(snapshot_ts),
        "last": (snapshot or {}).get("last"),
        "bid": (snapshot or {}).get("bid"),
        "ask": (snapshot or {}).get("ask"),
    } if snapshot else None

    queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=1)
    token = None
    bus_token = None
    event_payload: Optional[Any] = None

    def _handler(envelope: Any) -> None:
        if getattr(envelope, "symbol", None) == symbol and not queue.full():
            queue.put_nowait(envelope)

    try:
        token = await manager.subscribe(request)
        bus_token = manager.event_bus.subscribe("market.tick", _handler)
        try:
            event_payload = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            event_payload = None
    except DataSourceError as exc:
        return StepResult(
            status="error",
            details={"message": f"订阅实时价格失败：{exc}", "symbol": symbol},
        )
    finally:
        if bus_token is not None:
            manager.event_bus.unsubscribe(bus_token)
        if token is not None:
            try:
                await manager.unsubscribe(token)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("Failed to release market tick subscription", error=str(exc), symbol=symbol)

    if event_payload is not None:
        ts_ns = _coerce_int(getattr(event_payload, "ts_ns", None))
        details["event"] = {
            "timestamp": _ns_to_iso_local(ts_ns),
            "source": getattr(event_payload, "source", None),
        }
        details["message"] = "成功接收实时价格推送。"
        return StepResult(status="success", details=details)

    if snapshot:
        details["message"] = "存在价格快照，但等待窗口内未收到推送，请检查行情订阅或 IB Feed。"
        return StepResult(status="warning", details=details)

    details["message"] = "缺少实时价格快照，请确认 IB Feed 是否运行以及 Redis 是否可写。"
    return StepResult(status="error", details=details)


async def _dom_depth_step() -> StepResult:
    base_symbol = _env("RUNBOOK_SYMBOL_DOM") or _env("RUNBOOK_SYMBOL_TICK") or "ES"
    symbol = (base_symbol or "ES").strip().upper()
    num_rows = _parse_positive_int(_env("RUNBOOK_DOM_ROWS")) or 5
    wait_seconds = _parse_positive_float(_env("RUNBOOK_DOM_WAIT_SECONDS")) or 3.0

    try:
        manager = get_data_source_manager()
    except Exception as exc:
        return StepResult(
            status="error",
            details={"message": f"数据层初始化失败：{exc}"},
        )

    request = DataSubscriptionRequest(
        channel="dom.depth",
        symbol=symbol,
        options={"num_rows": num_rows},
    )

    try:
        snapshot = await manager.snapshot(request)
    except DataSourceError as exc:
        return StepResult(
            status="error",
            details={"message": f"读取盘口快照失败：{exc}", "symbol": symbol},
        )

    snapshot_ts = _coerce_int((snapshot or {}).get("ts_ns") or (snapshot or {}).get("timestamp_ns"))
    details: Dict[str, Any] = {
        "symbol": symbol,
        "requested_rows": num_rows,
        "wait_seconds": wait_seconds,
        "snapshot": {
            "timestamp": _ns_to_iso_local(snapshot_ts),
            "bid_levels": len((snapshot or {}).get("bids", []) or []),
            "ask_levels": len((snapshot or {}).get("asks", []) or []),
        }
        if snapshot
        else None,
    }

    queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=1)
    token = None
    bus_token = None
    event_payload: Optional[Any] = None

    def _dom_handler(envelope: Any) -> None:
        if getattr(envelope, "symbol", None) == symbol and not queue.full():
            queue.put_nowait(envelope)

    try:
        token = await manager.subscribe(request)
        bus_token = manager.event_bus.subscribe("dom.depth", _dom_handler)
        try:
            event_payload = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            event_payload = None
    except DataSourceError as exc:
        return StepResult(
            status="error",
            details={"message": f"订阅盘口数据失败：{exc}", "symbol": symbol},
        )
    finally:
        if bus_token is not None:
            manager.event_bus.unsubscribe(bus_token)
        if token is not None:
            try:
                await manager.unsubscribe(token)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("Failed to release DOM depth subscription", error=str(exc), symbol=symbol)

    writer = None
    storage_details: Dict[str, Any] = {}
    try:
        writer = get_market_data_writer()
    except Exception as exc:
        storage_details["error"] = str(exc)

    if writer is not None:
        try:
            summary = summarise_dom_directory(writer.base_dir, symbol)
        except Exception as exc:
            storage_details["error"] = str(exc)
        else:
            storage_details.update(
                {
                    "total_files": len(summary.files),
                    "total_rows": summary.total_rows,
                    "earliest": _ns_to_iso_local(summary.earliest_ns),
                    "latest": _ns_to_iso_local(summary.latest_ns),
                }
            )

    dom_service = get_dom_service()
    cache_history = dom_service.get_history(symbol, limit=1, include_snapshots=False)
    cache_metrics = cache_history.get("metrics") or []
    latest_metric_ts = None
    if cache_metrics:
        candidate = cache_metrics[-1]
        if isinstance(candidate, Mapping):
            latest_metric_ts = candidate.get("timestamp")

    cache_details = {
        "total_snapshots": cache_history.get("total_snapshots", 0),
        "total_metrics": cache_history.get("total_metrics", 0),
        "latest_metric": _ms_to_iso_local(latest_metric_ts),
    }

    if event_payload is not None:
        ts_ns = _coerce_int(getattr(event_payload, "ts_ns", None))
        details["event"] = {
            "timestamp": _ns_to_iso_local(ts_ns),
            "source": getattr(event_payload, "source", None),
        }
        details["message"] = "成功接收 DOM 深度推送。"
        status: StepStatus = "success"
    elif snapshot:
        details["message"] = "存在盘口快照，但未收到实时推送，请检查 depth 订阅或 IB Feed。"
        status = "warning"
    else:
        details["message"] = "缺少盘口快照，请确认 DOM 服务是否运行并与 IB Feed 连通。"
        status = "error"

    details["storage"] = storage_details
    details["cache"] = cache_details

    if not storage_details and status == "success":
        details["note"] = "本地未发现 DOM 落盘文件，如需供优化器使用请执行补拉。"

    return StepResult(status=status, details=details)


class WebsocketRemediationRunbook:
    """Runbook executing a sequence of configuration checks for WebSocket data."""

    identifier = "websocket-remediation"

    def __init__(self) -> None:
        self.name = "WebSocket 数据修复任务"
        self.description = "基于数据流排查清单的自动化检查与配置核对步骤。"
        self._steps: List[RunbookStep] = [
            RunbookStep(
                id="redis-config",
                title="Redis 配置一致性",
                description="校验主程序、独立服务与缓存的 Redis 地址是否一致。",
                runner=_redis_configuration_step,
            ),
            RunbookStep(
                id="realtime-price",
                title="实时价格订阅",
                description="检查 market.tick 快照与推送是否正常，提示行情链路异常。",
                runner=_realtime_price_step,
            ),
            RunbookStep(
                id="dom-depth",
                title="DOM 深度订阅",
                description="验证 dom.depth 推送、缓存与本地落盘覆盖情况。",
                runner=_dom_depth_step,
            ),
            RunbookStep(
                id="snapshot-refresh",
                title="仓位快照刷新频率",
                description="确认仓位刷新间隔已调整为 15 秒并能够执行刷新。",
                runner=_snapshot_refresh_step,
            ),
            RunbookStep(
                id="strategy-metadata",
                title="策略服务自愈配置",
                description="检查策略服务的 Redis 连接与元数据前缀配置。",
                runner=_strategy_service_step,
            ),
            RunbookStep(
                id="orders-snapshot",
                title="订单快照回源",
                description="核对 orders-service 快照键与补拉配置，提示陈旧缓存风险。",
                runner=_orders_snapshot_step,
            ),
            RunbookStep(
                id="log-path",
                title="日志面板路径",
                description="确认日志路径是否配置且文件可访问。",
                runner=_log_path_step,
            ),
            RunbookStep(
                id="database-consistency",
                title="数据库切换一致性",
                description="验证数据库连接配置可解析，便于进一步健康检查。",
                runner=_database_consistency_step,
            ),
        ]
        self._state = RunbookState(
            tasks=[
                RunbookTaskState(
                    id=step.id,
                    title=step.title,
                    description=step.description,
                )
                for step in self._steps
            ]
        )
        self._lock = asyncio.Lock()
        self._runner_task: Optional[asyncio.Task[None]] = None

    async def get_state(self) -> RunbookState:
        async with self._lock:
            return self._state.clone()

    async def start(self) -> RunbookState:
        async with self._lock:
            if self._state.status == "running":
                raise RuntimeError("runbook_running")
            if self._runner_task and not self._runner_task.done():
                raise RuntimeError("runbook_running")
            self._state = RunbookState(
                status="running",
                run_id=uuid.uuid4().hex,
                started_at=datetime.utcnow(),
                tasks=[
                    RunbookTaskState(
                        id=step.id,
                        title=step.title,
                        description=step.description,
                    )
                    for step in self._steps
                ],
            )
            self._runner_task = asyncio.create_task(self._execute())
            return self._state.clone()

    async def _execute(self) -> None:
        warnings_detected = False
        for index, step in enumerate(self._steps):
            async with self._lock:
                state = self._state
                task_state = state.tasks[index]
                run_id = state.run_id
                task_state.status = "running"
                task_state.started_at = datetime.utcnow()
            try:
                result = await step.runner()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.exception("Runbook step failed: %s", step.id)
                result = StepResult(
                    status="error",
                    details={"message": f"任务执行失败：{exc}"},
                )
            async with self._lock:
                # Abort if a new run has started while executing this step.
                if self._state.run_id != run_id:
                    return
                task_state = self._state.tasks[index]
                task_state.completed_at = datetime.utcnow()
                task_state.details = result.details
                if result.status == "success":
                    task_state.status = "success"
                elif result.status == "warning":
                    warnings_detected = True
                    task_state.status = "warning"
                else:
                    task_state.status = "error"
                    self._state.status = "error"
                    self._state.completed_at = datetime.utcnow()
                    return
        async with self._lock:
            if self._state.status == "running":
                self._state.status = "warning" if warnings_detected else "success"
                self._state.completed_at = datetime.utcnow()


def serialize_runbook_state(runbook: WebsocketRemediationRunbook, state: RunbookState) -> Dict[str, Any]:
    return {
        "id": runbook.identifier,
        "name": runbook.name,
        "description": runbook.description,
        "status": state.status,
        "run_id": state.run_id,
        "started_at": _isoformat(state.started_at),
        "completed_at": _isoformat(state.completed_at),
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "status": task.status,
                "started_at": _isoformat(task.started_at),
                "completed_at": _isoformat(task.completed_at),
                "details": task.details,
            }
            for task in state.tasks
        ],
    }


__all__ = [
    "RunbookState",
    "WebsocketRemediationRunbook",
    "serialize_runbook_state",
]

