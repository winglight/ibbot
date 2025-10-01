import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional, Dict

from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

from src.utils import now_local

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
):
    try:
        return await load_logs_data(
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
    except Exception as e:
        logger.error(f"Get logs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/logs/modules")
async def get_log_modules(
    range: str = "today",
    user: Optional[str] = Depends(get_current_user),
):
    """Get unique module names from logs for dropdown filtering."""
    try:
        log_file = "logs/trading_bot.log"
        if not os.path.exists(log_file):
            return {"success": True, "modules": []}
        
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

        # Extract unique modules from log lines
        modules = set()
        for line in content:
            try:
                parts = line.split('|')
                # Expected format: | YYYY-mm-dd HH:MM:SS | LEVEL | MODULE | ...
                if len(parts) >= 4:
                    module = parts[3].strip()
                    if module and module != "-":
                        modules.add(module)
            except Exception:
                continue
        
        # Sort modules alphabetically
        sorted_modules = sorted(list(modules))
        
        return {"success": True, "modules": sorted_modules}
    except Exception as e:
        logger.error(f"Get log modules error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/logs/stats")
async def get_log_stats(
    range: str = "today",
    module: str = "all",
    user: Optional[str] = Depends(get_current_user),
):
    try:
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

        pattern = re.compile(r"REQUEST \w+ (?P<path>\S+) status=(?P<status>\d+) duration=(?P<duration>[0-9.]+)ms")
        stats: Dict[str, Dict[str, Dict[str, float]]] = {}

        for line in lines:
            parts = line.split("|")
            if len(parts) < 6:
                continue
            mod = parts[3].strip()
            if module != "all" and mod != module:
                continue
            m = pattern.search(line)
            if not m:
                continue
            path = m.group("path")
            status = int(m.group("status"))
            duration = float(m.group("duration"))
            mod_stats = stats.setdefault(mod, {})
            entry = mod_stats.setdefault(path, {"count": 0, "total_time": 0.0, "success": 0})
            entry["count"] += 1
            entry["total_time"] += duration
            if status < 400:
                entry["success"] += 1

        for mod, paths in stats.items():
            for path, data in paths.items():
                data["avg_time"] = round(data["total_time"] / data["count"], 2)
                data["success_rate"] = round(data["success"] / data["count"], 2)
                del data["total_time"]
                del data["success"]

        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Get log stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
