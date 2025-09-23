"""SQLite persistence layer for optimizer service."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, Optional

DEFAULT_DB_PATH = Path(os.getenv("OPTIMIZER_DB_PATH", "data/optimizer.db"))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_load(payload: Optional[str]) -> Any:
    if payload is None:
        return None
    return json.loads(payload)


class OptimizerDatabase:
    """Thin wrapper around sqlite3 for storing plans and tasks."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        if isinstance(db_path, Path):
            path_obj = db_path
            db_path_str = str(db_path)
        else:
            db_path_str = db_path
            path_obj = Path(db_path)

        if db_path_str not in {":memory:", ""} and not db_path_str.startswith("file:"):
            path_obj.parent.mkdir(parents=True, exist_ok=True)

        self._db_path = path_obj
        self._lock = Lock()
        self._conn = sqlite3.connect(db_path_str, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS optimizer_plans (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    algorithm TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    frequency_minutes INTEGER NOT NULL,
                    iterations INTEGER,
                    is_active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    task_history TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS optimizer_tasks (
                    task_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT,
                    fail_reasons TEXT NOT NULL,
                    report_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(plan_id) REFERENCES optimizer_plans(id) ON DELETE CASCADE
                );
                """
            )

    # ------------------------------------------------------------------ Plans
    def save_plan(self, data: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO optimizer_plans (
                    id, name, strategy, symbol, metrics, parameters,
                    algorithm, start_date, end_date, frequency_minutes,
                    iterations, is_active, created_at, updated_at,
                    last_run_at, next_run_at, task_history
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    strategy=excluded.strategy,
                    symbol=excluded.symbol,
                    metrics=excluded.metrics,
                    parameters=excluded.parameters,
                    algorithm=excluded.algorithm,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    frequency_minutes=excluded.frequency_minutes,
                    iterations=excluded.iterations,
                    is_active=excluded.is_active,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    last_run_at=excluded.last_run_at,
                    next_run_at=excluded.next_run_at,
                    task_history=excluded.task_history
                """,
                (
                    data["id"],
                    data["name"],
                    data["strategy"],
                    data["symbol"],
                    _json_dump(data["metrics"]),
                    _json_dump(data["parameters"]),
                    data["algorithm"],
                    data["start_date"],
                    data["end_date"],
                    data["frequency_minutes"],
                    data["iterations"],
                    1 if data["is_active"] else 0,
                    data["created_at"],
                    data["updated_at"],
                    data.get("last_run_at"),
                    data.get("next_run_at"),
                    _json_dump(data["task_history"]),
                ),
            )

    def delete_plan(self, plan_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM optimizer_plans WHERE id=?", (plan_id,))
            self._conn.execute("DELETE FROM optimizer_tasks WHERE plan_id=?", (plan_id,))

    def load_plans(self) -> Iterable[Dict[str, Any]]:
        cursor = self._conn.execute("SELECT * FROM optimizer_plans")
        for row in cursor.fetchall():
            raw_metrics = _json_load(row["metrics"])
            if isinstance(raw_metrics, dict):
                base_metrics = list(raw_metrics.get("base") or raw_metrics.get("base_metrics") or [])
                feature_metrics = list(raw_metrics.get("feature") or raw_metrics.get("feature_metrics") or [])
                all_metrics = list(raw_metrics.get("all") or raw_metrics.get("metrics") or [])
                if not all_metrics:
                    seen = []
                    for value in base_metrics + feature_metrics:
                        if value in seen:
                            continue
                        seen.append(value)
                    all_metrics = seen
            else:
                all_metrics = list(raw_metrics or [])
                base_metrics = list(all_metrics)
                feature_metrics = []
            yield {
                "id": row["id"],
                "name": row["name"],
                "strategy": row["strategy"],
                "symbol": row["symbol"],
                "metrics": all_metrics,
                "base_metrics": base_metrics,
                "feature_metrics": feature_metrics,
                "parameters": _json_load(row["parameters"]) or {},
                "algorithm": row["algorithm"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "frequency_minutes": row["frequency_minutes"],
                "iterations": row["iterations"],
                "is_active": bool(row["is_active"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "last_run_at": row["last_run_at"],
                "next_run_at": row["next_run_at"],
                "task_history": _json_load(row["task_history"]) or [],
            }

    # ------------------------------------------------------------------ Tasks
    def save_task(self, task_data: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO optimizer_tasks (
                    task_id, plan_id, request_json, state, progress,
                    message, fail_reasons, report_json, created_at, completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    plan_id=excluded.plan_id,
                    request_json=excluded.request_json,
                    state=excluded.state,
                    progress=excluded.progress,
                    message=excluded.message,
                    fail_reasons=excluded.fail_reasons,
                    report_json=excluded.report_json,
                    created_at=excluded.created_at,
                    completed_at=excluded.completed_at
                """,
                (
                    task_data["task_id"],
                    task_data.get("plan_id"),
                    _json_dump(task_data["request"]),
                    task_data["state"],
                    task_data["progress"],
                    task_data.get("message"),
                    _json_dump(task_data.get("fail_reasons", [])),
                    _json_dump(task_data.get("report")),
                    task_data["created_at"],
                    task_data.get("completed_at"),
                ),
            )

    def delete_task(self, task_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM optimizer_tasks WHERE task_id=?", (task_id,))

    def delete_tasks_for_plan(self, plan_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM optimizer_tasks WHERE plan_id=?", (plan_id,))

    def load_tasks(self) -> Iterable[Dict[str, Any]]:
        cursor = self._conn.execute("SELECT * FROM optimizer_tasks")
        for row in cursor.fetchall():
            yield {
                "task_id": row["task_id"],
                "plan_id": row["plan_id"],
                "request": _json_load(row["request_json"]),
                "state": row["state"],
                "progress": row["progress"],
                "message": row["message"],
                "fail_reasons": _json_load(row["fail_reasons"]) or [],
                "report": _json_load(row["report_json"]),
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
            }


optimizer_db = OptimizerDatabase()
