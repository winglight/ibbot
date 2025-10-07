import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional, Dict

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from src.utils import now_local
from src.routes.service_clients import ServiceClientError, ServiceUnavailableError

from .auth import get_current_user

router = APIRouter()


async def load_logs_data(
    *,
    lines: int = 50,
    level: str = "all",
    request_id: Optional[str] = None,
    module: str = "all",
    range: str = "today",
    cursor: Optional[int] = None,
    page_size: int = 200,
    direction: str = "backward",
    search: Optional[str] = None,
) -> Dict[str, Any]:
    log_file = "logs/trading_bot.log"
    if not os.path.exists(log_file):
        return {"success": True, "logs": [], "pagination": {"total": 0}}

    # 使用异步文件读取，避免阻塞
    import aiofiles

    async with aiofiles.open(log_file, "r") as f:
        content = await f.readlines()
        # 限制最大读取行数，避免内存问题
        if len(content) > 10000:
            content = content[-10000:]

    # Filter by date range
    now = now_local()
    if range == "today":
        date_str = now.strftime("%Y-%m-%d")
        content = [line for line in content if line.startswith(f"| {date_str}")]
    elif range == "week":
        start = now - timedelta(days=now.weekday())
        filtered = []
        for line in content:
            try:
                ts = datetime.strptime(line[2:21], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts.date() >= start.date():
                filtered.append(line)
        content = filtered

    if level.lower() != "all":
        # Inclusive level filtering: warning -> WARNING, ERROR, CRITICAL
        levels_order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        try:
            idx = levels_order.index(level.upper())
            allowed = set(levels_order[idx:])
        except ValueError:
            allowed = set(levels_order)  # fallback to all

        def extract_level(line: str):
            try:
                parts = line.split('|')
                # Expected format: | YYYY-mm-dd HH:MM:SS | LEVEL | MODULE | ...
                if len(parts) >= 3:
                    return parts[2].strip().upper()
            except Exception:
                return None
            return None

        content = [line for line in content if extract_level(line) in allowed]

    if module and module != "all":
        targets = {m.strip() for m in module.split(',') if m.strip()}

        def extract_module(line: str) -> Optional[str]:
            try:
                parts = line.split('|')
                if len(parts) >= 4:
                    return parts[3].strip()
            except Exception:
                return None
            return None

        if targets:
            content = [line for line in content if extract_module(line) in targets]

    if request_id:
        content = [line for line in content if request_id in line]

    if search:
        try:
            pattern = re.compile(search, re.IGNORECASE)
            content = [line for line in content if pattern.search(line)]
        except re.error:
            content = [line for line in content if search.lower() in line.lower()]

    total = len(content)
    if total == 0:
        return {
            "success": True,
            "logs": [],
            "pagination": {
                "total": 0,
                "page_size": page_size,
                "direction": direction,
                "cursor": 0,
                "has_more_backward": False,
                "has_more_forward": False,
            },
        }

    # Backwards compatibility: honour legacy `lines` parameter when explicit
    effective_page_size = max(1, min(page_size or lines, 500))
    if page_size <= 0 and lines > 0:
        effective_page_size = min(lines, 500)

    normalised_direction = direction.lower() if direction else "backward"
    if normalised_direction not in {"backward", "forward"}:
        normalised_direction = "backward"

    if cursor is None:
        cursor = total if normalised_direction == "backward" else 0
    cursor = max(0, min(cursor, total))

    if normalised_direction == "forward":
        start = cursor
        end = min(total, cursor + effective_page_size)
    else:
        end = cursor or total
        start = max(0, end - effective_page_size)

    window = content[start:end]
    pagination = {
        "total": total,
        "page_size": effective_page_size,
        "direction": normalised_direction,
        "cursor": cursor,
        "start": start,
        "end": end,
        "has_more_backward": start > 0,
        "has_more_forward": end < total,
        "next_backward_cursor": start if start > 0 else None,
        "next_forward_cursor": end if end < total else None,
        "tail_cursor": total,
    }

    return {"success": True, "logs": window, "pagination": pagination}


async def _load_log_modules(*, range: str = "today") -> Dict[str, Any]:
    """Load available log modules within the requested time range."""

    log_file = "logs/trading_bot.log"
    if not os.path.exists(log_file):
        return {"success": True, "modules": []}

    import aiofiles

    async with aiofiles.open(log_file, "r") as f:
        content = await f.readlines()
        if len(content) > 10000:
            content = content[-10000:]

    now = now_local()
    if range == "today":
        date_str = now.strftime("%Y-%m-%d")
        content = [line for line in content if line.startswith(f"| {date_str}")]
    elif range == "week":
        start = now - timedelta(days=now.weekday())
        filtered = []
        for line in content:
            try:
                ts = datetime.strptime(line[2:21], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts.date() >= start.date():
                filtered.append(line)
        content = filtered

    modules = set()
    for line in content:
        try:
            parts = line.split("|")
            if len(parts) >= 4:
                module = parts[3].strip()
                if module and module != "-":
                    modules.add(module)
        except Exception:
            continue

    return {"success": True, "modules": sorted(modules)}


async def _load_log_stats(*, range: str = "today", module: str = "all") -> Dict[str, Any]:
    """Compute per-route log statistics for the requested range and module."""

    log_file = "logs/trading_bot.log"
    if not os.path.exists(log_file):
        return {"success": True, "stats": {}}

    with open(log_file, "r") as f:
        lines = f.readlines()

    now = now_local()
    if range == "today":
        date_str = now.strftime("%Y-%m-%d")
        lines = [line for line in lines if line.startswith(f"| {date_str}")]
    elif range == "week":
        start = now - timedelta(days=now.weekday())
        filtered = []
        for line in lines:
            try:
                ts = datetime.strptime(line[2:21], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts.date() >= start.date():
                filtered.append(line)
        lines = filtered

    pattern = re.compile(
        r"REQUEST \w+ (?P<path>\S+) status=(?P<status>\d+) duration=(?P<duration>[0-9.]+)ms"
    )
    stats: Dict[str, Dict[str, Dict[str, float]]] = {}

    for line in lines:
        parts = line.split("|")
        if len(parts) < 6:
            continue
        mod = parts[3].strip()
        if module != "all" and mod != module:
            continue
        match = pattern.search(line)
        if not match:
            continue
        path = match.group("path")
        status = int(match.group("status"))
        duration = float(match.group("duration"))
        module_stats = stats.setdefault(mod, {})
        entry = module_stats.setdefault(path, {"count": 0, "total_time": 0.0, "success": 0})
        entry["count"] += 1
        entry["total_time"] += duration
        if status < 400:
            entry["success"] += 1

    for module_stats in stats.values():
        for path, data in module_stats.items():
            total = data["count"] or 1
            data["avg_time"] = round(data["total_time"] / total, 2)
            data["success_rate"] = round(data["success"] / total, 2)
            del data["total_time"]
            del data["success"]

    return {"success": True, "stats": stats}


class _FileLogsServiceClient:
    async def get_logs(self, **kwargs: Any) -> Dict[str, Any]:
        return await load_logs_data(**kwargs)

    async def get_modules(self, *, range: str = "today") -> Dict[str, Any]:
        return await _load_log_modules(range=range)

    async def get_stats(self, *, range: str = "today", module: str = "all") -> Dict[str, Any]:
        return await _load_log_stats(range=range, module=module)


_LOGS_SERVICE_CLIENT = _FileLogsServiceClient()


def get_logs_service_client() -> _FileLogsServiceClient:
    return _LOGS_SERVICE_CLIENT


def _get_logs_service_client() -> _FileLogsServiceClient:
    return get_logs_service_client()


@router.get("/api/logs")
async def get_logs(
    lines: int = 50,  # backwards compatibility fallback
    level: str = "all",
    request_id: Optional[str] = None,
    module: str = "all",
    range: str = "today",
    cursor: Optional[int] = None,
    page_size: int = 200,
    direction: str = "backward",
    search: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
    client: _FileLogsServiceClient = Depends(_get_logs_service_client),
):
    try:
        return await client.get_logs(
            lines=lines,
            level=level,
            request_id=request_id,
            module=module,
            range=range,
            cursor=cursor,
            page_size=page_size,
            direction=direction,
            search=search,
        )
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Get logs error: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/logs/modules")
async def get_log_modules(
    range: str = "today",
    user: Optional[str] = Depends(get_current_user),
    client: _FileLogsServiceClient = Depends(_get_logs_service_client),
):
    """Get unique module names from logs for dropdown filtering."""
    try:
        return await client.get_modules(range=range)
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Get log modules error: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/logs/stats")
async def get_log_stats(
    range: str = "today",
    module: str = "all",
    user: Optional[str] = Depends(get_current_user),
    client: _FileLogsServiceClient = Depends(_get_logs_service_client),
):
    try:
        return await client.get_stats(range=range, module=module)
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Get log stats error: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
