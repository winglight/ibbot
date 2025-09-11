import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict

from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

from .auth import get_current_user

router = APIRouter()

@router.get("/api/logs")
async def get_logs(
    lines: int = 50,  # 减少默认行数
    level: str = "all",
    request_id: Optional[str] = None,
    module: str = "all",
    range: str = "today",
    user: Optional[str] = Depends(get_current_user),
):
    try:
        log_file = "logs/trading_bot.log"
        if not os.path.exists(log_file):
            return {"success": True, "logs": []}
        
        # 使用异步文件读取，避免阻塞
        import aiofiles
        async with aiofiles.open(log_file, "r") as f:
            content = await f.readlines()
            # 限制最大读取行数，避免内存问题
            if len(content) > 10000:
                content = content[-10000:]

        # Filter by date range
        now = datetime.now()
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

        if module != "all":
            content = [line for line in content if f"| {module} |" in line]

        if request_id:
            content = [line for line in content if request_id in line]

        return {"success": True, "logs": content[-lines:]}
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
        now = datetime.now()
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

        now = datetime.now()
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
