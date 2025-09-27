"""
Strategy Management Module
Handles strategy storage, execution, and performance tracking
"""

import os
import json
import csv
import hashlib
import hmac
import time
import importlib
import importlib.util
import sys
import asyncio
from copy import deepcopy
from threading import RLock
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Any, TYPE_CHECKING, Set, Tuple, Iterable, Union, Coroutine
from dataclasses import dataclass, asdict
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .utils import (
    ensure_local_timezone,
    get_us_market_session,
    now_local,
    parse_local_datetime,
    slugify_strategy_name,
    to_local_iso,
)

DEFAULT_STRATEGY_PATH = "src/strategies/"
LEGACY_STRATEGY_PATHS = {"strategy", "strategy/", "./strategy", "./strategy/"}
LEGACY_STRATEGY_PATHS_NORMALIZED = {p.replace("\\", "/").rstrip("/") for p in LEGACY_STRATEGY_PATHS}

if TYPE_CHECKING:
    from src.strategies.base import BaseStrategy

# Module-specific logger for strategy_manager
strategy_logger = logger.bind(module="strategy_manager", request_id="-")

from .database import DatabaseManager, get_db_manager, Strategy as DatabaseStrategy


@dataclass
class Strategy:
    """Strategy data class"""
    name: str
    strategy_type: str  # "external" or "internal"
    enabled: bool = True
    api_endpoint: Optional[str] = None
    file_path: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime = None
    trigger_count: int = 0
    last_triggered: Optional[datetime] = None
    schedule: Optional[List[Dict[str, Any]]] = None
    primary_symbol: Optional[str] = None

    def __post_init__(self):
        self.created_at = ensure_local_timezone(self.created_at) or now_local()
        self.last_triggered = ensure_local_timezone(self.last_triggered)



class StrategyManager:
    """Strategy Manager Class"""
    
    def __init__(self, data_dir: str = "data", strategy_dir: str = DEFAULT_STRATEGY_PATH):
        self.data_dir = Path(data_dir)
        normalized_strategy_dir = self._normalize_strategy_path_value(strategy_dir)
        self.strategy_dir = Path(normalized_strategy_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        
        # External and internal API clients
        self.external_clients = {}
        
        # Database manager
        self.db_manager = get_db_manager()

        # Dependencies - will be injected
        self.risk_manager = None
        self.queue_manager = None
        self.notification_manager = None
        self.strategy_risk_manager = None

        # 内存缓存，从数据库加载
        self.strategies: Dict[str, Strategy] = {}
        self.settings: Dict[str, Any] = {}
        self.internal_strategies_runtime: Dict[str, "BaseStrategy"] = {}
        self._loaded_internal_modules: Dict[str, str] = {}
        self._last_position_add: Dict[Tuple[str, str, str], datetime] = {}
        self._auto_close_tasks: Dict[Tuple[str, str], asyncio.Task] = {}
        self.data_push_state: Dict[str, Dict[str, Any]] = {}
        self._pending_data_push_requests: List[
            Tuple[str, Optional[str], Optional[str], bool]
        ] = []
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._strategies_cache: Dict[str, Any] = {
            "data": None,
            "expires_at": 0.0,
            "names": (),
        }
        self._strategies_cache_lock = RLock()
        self._strategies_cache_ttl = 5.0

        # 认证token（用于外部策略访问）
        self.auth_token = None

        # 调度器
        self.scheduler = AsyncIOScheduler()
        self._heartbeat_job_id = "strategy_manager_heartbeat"
        self._heartbeat_interval_seconds = 300
        self._data_push_log_limit = 20

        # 加载数据
        self._load_from_database()

    def set_dependencies(
        self,
        risk_manager=None,
        queue_manager=None,
        notification_manager=None,
        strategy_risk_manager=None,
    ):
        """Set dependency objects for strategy manager"""
        self.risk_manager = risk_manager
        self.queue_manager = queue_manager
        if notification_manager:
            self.notification_manager = notification_manager
        if strategy_risk_manager:
            self.strategy_risk_manager = strategy_risk_manager
        strategy_logger.info(
            "Dependencies set for StrategyManager",
            risk_manager="initialized" if risk_manager else "None",
            queue_manager="initialized" if queue_manager else "None",
            notification_manager="initialized" if notification_manager else "None",
            strategy_risk_manager="initialized" if strategy_risk_manager else "None",
        )

        # Propagate dependencies to runtime strategies
        for strategy in self.internal_strategies_runtime.values():
            try:
                strategy.set_dependencies(
                    strategy_manager=self,
                    risk_manager=self.risk_manager,
                    queue_manager=self.queue_manager,
                    strategy_risk_manager=self.strategy_risk_manager,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(f"Failed to propagate dependencies to {strategy.name}: {exc}")

    def start(self):
        """Start strategy manager scheduler"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        self._event_loop = loop
        self._start_scheduler()
        strategy_logger.info("Strategy manager started")
        self._notify_scheduler_state("started")

    def stop(self):
        """Stop strategy manager scheduler"""
        try:
            if self.scheduler.running:
                self.scheduler.shutdown()
                strategy_logger.info("Strategy manager scheduler stopped")
                self._notify_scheduler_state("stopped")
        except Exception as e:
            strategy_logger.error(f"Error stopping strategy manager scheduler: {e}")

    def restart_scheduler(self):
        """Restart scheduler after strategy updates"""
        self.stop()
        self.start()

    def _normalize_strategy_path_value(self, path_value: Optional[str]) -> str:
        """Normalize incoming strategy path values and migrate legacy defaults."""

        if path_value is None:
            return DEFAULT_STRATEGY_PATH

        path_str = str(path_value).strip()
        if not path_str:
            return DEFAULT_STRATEGY_PATH

        normalized = path_str.replace("\\", "/")
        stripped = normalized.rstrip("/")

        if stripped in LEGACY_STRATEGY_PATHS_NORMALIZED:
            return DEFAULT_STRATEGY_PATH

        return path_str

    def _start_scheduler(self):
        """Start scheduler and schedule jobs"""
        if not self.scheduler.running:
            self.scheduler.start()
            strategy_logger.info("Strategy manager scheduler started")
        self._schedule_scheduler_heartbeat()
        self.schedule_strategy_orders()

    def _schedule_scheduler_heartbeat(self) -> None:
        """Ensure the scheduler heartbeat job is registered."""
        try:
            existing_job = self.scheduler.get_job(self._heartbeat_job_id)
            if existing_job:
                return
            self.scheduler.add_job(
                self._emit_scheduler_heartbeat,
                "interval",
                seconds=self._heartbeat_interval_seconds,
                id=self._heartbeat_job_id,
                replace_existing=True,
            )
            strategy_logger.debug(
                f"Scheduler heartbeat job registered every {self._heartbeat_interval_seconds} seconds"
            )
        except Exception as exc:
            strategy_logger.warning(f"Failed to register scheduler heartbeat: {exc}")

    def _emit_scheduler_heartbeat(self) -> None:
        """Emit a heartbeat log with scheduler and queue metrics."""
        heartbeat_logger = strategy_logger.bind(event="scheduler_heartbeat")
        if not self.scheduler.running:
            heartbeat_logger.warning("Scheduler heartbeat skipped because scheduler is not running")
            return

        enabled_names = self.get_enabled_strategy_names()
        enabled_count = len(enabled_names)

        next_run_iso = None
        try:
            jobs = [job for job in self.scheduler.get_jobs() if job.id != self._heartbeat_job_id]
            next_run_times = [ensure_local_timezone(job.next_run_time) for job in jobs if job.next_run_time]
            if next_run_times:
                next_run_iso = min(next_run_times).isoformat()
        except Exception as exc:  # pragma: no cover - defensive guard
            heartbeat_logger.warning(f"Failed to inspect scheduler jobs: {exc}")

        queue_metrics: Dict[str, Any] = {}
        oldest_pending_age = None
        try:
            if self.queue_manager:
                queue_metrics = self.queue_manager.get_queue_status()
                oldest = getattr(self.queue_manager, "get_oldest_pending_task_info", None)
                if callable(oldest):
                    info = oldest()
                    if info:
                        oldest_pending_age = info.get("age_seconds")
        except Exception as exc:  # pragma: no cover - defensive guard
            heartbeat_logger.warning(f"Failed to gather queue metrics: {exc}")

        heartbeat_logger.info(
            "Scheduler heartbeat emitted",
            enabled_strategies=enabled_count,
            strategy_names=enabled_names,
            next_run=next_run_iso,
            queue_pending=queue_metrics.get("pending_count"),
            queue_processing=queue_metrics.get("processing_count"),
            queue_failed=queue_metrics.get("failed_count"),
            oldest_pending_seconds=oldest_pending_age,
        )

    def _schedule_notification(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Dispatch coroutine-based notifications safely."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._event_loop

        if loop and loop.is_running():
            loop.create_task(coro)
        else:  # pragma: no cover - fallback for sync contexts
            asyncio.run(coro)

    def _record_data_push_event(
        self,
        strategy_name: str,
        message: str,
        *,
        level: str = "info",
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append an entry to the in-memory data push event log."""

        state = self.data_push_state.setdefault(strategy_name, {})
        entry: Dict[str, Any] = {
            "timestamp": now_local().isoformat(timespec="seconds"),
            "level": level,
            "message": message,
        }
        if details:
            entry["details"] = details
        log_entries: List[Dict[str, Any]] = state.setdefault("event_log", [])  # type: ignore[assignment]
        log_entries.append(entry)
        if len(log_entries) > self._data_push_log_limit:
            del log_entries[:-self._data_push_log_limit]
        if reason is not None:
            state["status_reason"] = reason

    def _set_data_push_reason(self, strategy_name: str, reason: Optional[str]) -> None:
        state = self.data_push_state.setdefault(strategy_name, {})
        state["status_reason"] = reason

    def _notify_scheduler_state(self, state: str) -> None:
        """Send scheduler state change notifications when enabled."""
        manager = self.notification_manager
        if manager is None:
            try:
                import core as core_module  # type: ignore

                manager = getattr(core_module, "notification_manager", None)
            except Exception:  # pragma: no cover - defensive guard
                manager = None

        if manager is None:
            return

        enabled_names = self.get_enabled_strategy_names()
        enabled_count = len(enabled_names)
        message = (
            "策略调度器已启动" if state == "started" else "策略调度器已停止"
        )
        details = f"启用策略: {enabled_count}" + (
            f" ({', '.join(enabled_names)})" if enabled_names else ""
        )

        coro = manager.send_notification(
            message=f"{message}\n{details}",
            event_type=f"strategy_{state}",
            scheduler_state=state,
            enabled_strategy_count=enabled_count,
            enabled_strategies=enabled_names,
        )
        try:
            self._schedule_notification(coro)
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_logger.warning(f"Failed to dispatch scheduler notification: {exc}")

    def get_enabled_strategy_names(self) -> List[str]:
        """Return names of strategies currently marked as enabled."""
        try:
            return [name for name, strat in self.strategies.items() if strat.enabled]
        except Exception:  # pragma: no cover - defensive guard
            return []

    def _invalidate_strategies_cache(self) -> None:
        with self._strategies_cache_lock:
            self._strategies_cache = {
                "data": None,
                "expires_at": 0.0,
                "names": (),
            }

    def _enqueue_strategy_order(self, payload: Dict[str, Any]):
        """Helper to add strategy order task to queue"""
        if not self.queue_manager:
            strategy_logger.warning("QueueManager not set; cannot enqueue strategy order")
            return
        try:
            self.queue_manager.add_task("strategy_order", payload)
            strategy_logger.info(f"Scheduled strategy order queued: {payload}")
        except Exception as e:
            strategy_logger.error(f"Failed to queue scheduled strategy order: {e}")

    def schedule_strategy_orders(self):
        """Parse strategy schedules and register jobs"""
        if not self.scheduler:
            return
        try:
            for job in self.scheduler.get_jobs():
                if job.id == self._heartbeat_job_id:
                    continue
                try:
                    self.scheduler.remove_job(job.id)
                except Exception as exc:
                    strategy_logger.debug(f"Failed to remove scheduler job {job.id}: {exc}")
        except Exception as exc:
            strategy_logger.warning(f"Failed to cleanup existing scheduler jobs: {exc}")
        for strategy in self.strategies.values():
            if strategy.strategy_type != "internal":
                continue
            if not strategy.enabled or not strategy.schedule:
                continue
            for idx, item in enumerate(strategy.schedule):
                time_str = item.get("time")
                symbol = item.get("symbol")
                action = item.get("action")
                qty = item.get("qty")
                order_type = item.get("type", "MARKET")
                price = item.get("price")
                exchange = item.get("exchange")
                sec_type = item.get("sec_type", "STK")
                if not time_str or not symbol or not action or qty is None:
                    continue
                try:
                    hour, minute = map(int, time_str.split(":"))
                except Exception:
                    strategy_logger.warning(
                        f"Invalid time format in schedule for {strategy.name}: {time_str}"
                    )
                    continue
                payload = {
                    "strategy": strategy.name,
                    "symbol": symbol,
                    "action": action,
                    "qty": qty,
                    "type": order_type,
                    "price": price,
                    "exchange": exchange,
                    "sec_type": sec_type,
                }
                job_id = f"{strategy.name}_{idx}"
                self.scheduler.add_job(
                    self._enqueue_strategy_order,
                    'cron',
                    hour=hour,
                    minute=minute,
                    id=job_id,
                    args=[payload],
                    replace_existing=True,
                    coalesce=True,
                    misfire_grace_time=10,
                    max_instances=1,
                )
        self._schedule_scheduler_heartbeat()

    def register_internal_strategy(self, strategy_instance: "BaseStrategy") -> None:
        """Register a runtime-managed internal strategy instance."""

        name = strategy_instance.name
        self.internal_strategies_runtime[name] = strategy_instance
        strategy_instance.strategy_manager = self

        dependency_kwargs = {
            "strategy_manager": self,
            "risk_manager": self.risk_manager,
            "queue_manager": self.queue_manager,
        }

        core_module = None
        try:  # pragma: no cover - lazy import to avoid circular reference at module load
            import core as core_module  # type: ignore
        except Exception:
            core_module = None

        if core_module is not None:
            order_mgr = getattr(core_module, "order_manager", None)
            if order_mgr is not None:
                dependency_kwargs["order_manager"] = order_mgr
            notifier = getattr(core_module, "notification_manager", None)
            if notifier is not None:
                dependency_kwargs["notification_manager"] = notifier

        try:
            strategy_instance.set_dependencies(**dependency_kwargs)
        except TypeError:
            # Fall back to minimal dependency injection for custom signatures
            try:
                strategy_instance.set_dependencies(strategy_manager=self)
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(f"Failed to set dependencies for {name}: {exc}")
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_logger.warning(f"Failed to set dependencies for {name}: {exc}")

        if getattr(strategy_instance, "dom_service", None) is None:
            try:
                from src.dom_service import get_dom_service

                strategy_instance.set_dependencies(dom_service=get_dom_service())
            except TypeError:
                # Strategy does not accept DOM dependency; ignore silently
                pass
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    f"Failed to inject DOM service dependency for {name}: {exc}"
                )

        self._apply_persisted_parameters(name, strategy_instance)

        record = self.strategies.get(name)
        if not record:
            record = Strategy(
                name=name,
                strategy_type="internal",
                enabled=True,
                api_endpoint=None,
                file_path=getattr(strategy_instance, "file_path", None),
                description=getattr(strategy_instance, "description", name),
                created_at=now_local(),
            )
            self.strategies[name] = record
            try:
                db_strategy = DatabaseStrategy(
                    name=record.name,
                    strategy_type=record.strategy_type,
                    enabled=record.enabled,
                    api_endpoint=record.api_endpoint,
                    file_path=record.file_path,
                    description=record.description,
                    created_at=to_local_iso(record.created_at),
                )
                self.db_manager.save_strategy(db_strategy)
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(f"Failed to persist internal strategy {name}: {exc}")
        else:
            if record.strategy_type != "internal":
                record.strategy_type = "internal"
            runtime_description = getattr(strategy_instance, "description", None)
            if runtime_description:
                record.description = runtime_description
            runtime_path = getattr(strategy_instance, "file_path", None)
            if runtime_path:
                record.file_path = runtime_path
            self._save_strategies()

        stored_primary = self._get_strategy_setting(name, "primary_symbol")
        runtime_symbol = (
            stored_primary
            or (getattr(record, "primary_symbol", None) if record else None)
        )
        symbol_value: Optional[str] = None
        if runtime_symbol:
            try:
                symbol_value = str(runtime_symbol).strip().upper() or None
            except Exception:
                symbol_value = None
        if symbol_value and hasattr(strategy_instance, "symbol"):
            try:
                setattr(strategy_instance, "symbol", symbol_value)
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    f"Failed to apply primary symbol to runtime strategy {name}: {exc}"
                )
        if symbol_value and hasattr(strategy_instance, "primary_symbol"):
            try:
                setattr(strategy_instance, "primary_symbol", symbol_value)
            except Exception:
                pass

        strategy_instance.enabled = record.enabled if record else strategy_instance.enabled
        if strategy_instance.enabled:
            strategy_instance.start()
        else:
            strategy_instance.stop()

        try:
            strategy_instance.on_registered()
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_logger.warning(f"Strategy {name} on_registered hook failed: {exc}")

        strategy_logger.info(f"Internal strategy registered: {name}")

        if record and record.enabled:
            subscription = self._get_strategy_setting(name, "data_subscription")
            target_symbol = (
                self._get_strategy_setting(name, "primary_symbol")
                or getattr(record, "primary_symbol", None)
            )
            if subscription and target_symbol:
                self._request_data_push_activation(
                    name, subscription=subscription, symbol=target_symbol
                )

        self._invalidate_strategies_cache()

    def _load_from_database(self):
        """从数据库加载策略设置、策略和执行记录"""
        try:
            # 加载策略设置
            self.settings = self.db_manager.load_strategy_config()
            
            # 设置默认值
            if not self.settings:
                self.settings = {
                    "api_endpoint": "/api/strategy",
                    "api_method": "POST",
                    "api_payload_template": {
                        "strategy": "{{strategy_name}}",
                        "symbol": "{{symbol}}",
                        "action": "{{action}}",
                        "qty": "{{quantity}}",
                        "type": "{{order_type}}",
                        "price": "{{price}}"
                    },
                    "strategy_path": DEFAULT_STRATEGY_PATH,
                    "enable_auth": False,
                    "auth_token": None
                }
                # 保存默认设置到数据库
                self.db_manager.save_strategy_config(self.settings)

            normalized_strategy_path = self._normalize_strategy_path_value(self.settings.get("strategy_path"))
            if normalized_strategy_path != self.settings.get("strategy_path"):
                strategy_logger.info(
                    f"Normalizing legacy strategy_path from {self.settings.get('strategy_path')} to {normalized_strategy_path}"
                )
                self.settings["strategy_path"] = normalized_strategy_path
                try:
                    self.db_manager.save_strategy_config(self.settings)
                except Exception as exc:  # pragma: no cover - defensive guard
                    strategy_logger.warning(f"Failed to persist normalized strategy_path: {exc}")

            # 更新实例变量
            self.api_endpoint = self.settings["api_endpoint"]
            self.api_method = self.settings["api_method"]
            self.api_payload_template = self.settings["api_payload_template"]
            self.strategy_dir = Path(self.settings["strategy_path"]).resolve()
            self.enable_auth = self.settings.get("enable_auth", False)
            self.auth_token = self.settings.get("auth_token")
            
            # 确保策略目录存在
            self.strategy_dir.mkdir(parents=True, exist_ok=True)

            # 加载策略
            db_strategies = self.db_manager.load_strategies()
            for strategy_data in db_strategies:
                # 将数据库的策略数据类转换为本地策略数据类
                schedule = self.settings.get(f"{strategy_data.name}_schedule")
                strategy = Strategy(
                    name=strategy_data.name,
                    strategy_type=strategy_data.strategy_type,
                    enabled=strategy_data.enabled,
                    api_endpoint=strategy_data.api_endpoint,
                    file_path=strategy_data.file_path,
                    description=strategy_data.description,
                    created_at=parse_local_datetime(strategy_data.created_at) if strategy_data.created_at else now_local(),
                    trigger_count=0,  # 数据库数据类中没有这个字段，设为默认值
                    last_triggered=None,
                    schedule=schedule
                )
                primary_symbol = self.settings.get(f"{strategy_data.name}_primary_symbol")
                if primary_symbol:
                    strategy.primary_symbol = primary_symbol
                self.strategies[strategy.name] = strategy
            
            strategy_logger.info(f"Loaded {len(self.strategies)} strategies from database")
        except Exception as e:
            strategy_logger.error(f"Error loading from database: {e}")
            self._load_from_files_and_migrate()
        finally:
            self._invalidate_strategies_cache()
    
    def _load_from_files_and_migrate(self):
        """从文件加载数据并迁移到数据库（向后兼容）"""
        strategy_logger.info("Loading from legacy files and migrating to database...")
        
        # 加载设置
        self._load_settings()
        
        # 加载策略
        self._load_strategies()
        
        # 迁移到数据库
        try:
            # 保存设置到数据库
            self.db_manager.save_strategy_config(self.settings)

            # 保存策略到数据库
            for strategy in self.strategies.values():
                db_strategy = DatabaseStrategy(
                    name=strategy.name,
                    strategy_type=strategy.strategy_type,
                    enabled=strategy.enabled,
                    api_endpoint=strategy.api_endpoint,
                    file_path=strategy.file_path,
                    description=strategy.description,
                    created_at=to_local_iso(strategy.created_at)
                )
                self.db_manager.save_strategy(db_strategy)

            strategy_logger.info("Successfully migrated data from files to database")

        except Exception as e:
            strategy_logger.error(f"Error migrating data to database: {e}")
        finally:
            self._invalidate_strategies_cache()

    def _load_settings(self):
        """Load strategy settings - 兼容性方法，现在从数据库加载"""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r') as f:
                    data = json.load(f)
            except Exception as e:
                strategy_logger.warning(f"Failed to load settings: {e}")
                data = {}
        else:
            data = {}
            
        # Default settings
        defaults = {
            "api_endpoint": "/api/strategy",
            "api_method": "POST",
            "api_payload_template": {
                "strategy": "{{strategy_name}}",
                "symbol": "{{symbol}}",
                "action": "{{action}}",
                "qty": "{{quantity}}",
                "type": "{{order_type}}",
                "price": "{{price}}"
            },
            "strategy_path": DEFAULT_STRATEGY_PATH,
            "enable_auth": False,
            "auth_token": None
        }

        self.settings = {**defaults, **data}

        normalized_strategy_path = self._normalize_strategy_path_value(self.settings.get("strategy_path"))
        if normalized_strategy_path != self.settings.get("strategy_path"):
            strategy_logger.info(
                f"Normalizing legacy strategy_path from {self.settings.get('strategy_path')} to {normalized_strategy_path}"
            )
            self.settings["strategy_path"] = normalized_strategy_path

        # Update instance variables
        self.api_endpoint = self.settings["api_endpoint"]
        self.api_method = self.settings["api_method"]
        self.api_payload_template = self.settings["api_payload_template"]
        self.strategy_dir = Path(self.settings["strategy_path"]).resolve()
        self.enable_auth = self.settings.get("enable_auth", False)
        self.auth_token = self.settings.get("auth_token")
        
        # Ensure strategy directory exists
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        
    def _save_settings(self):
        """Save strategy settings - 现在保存到数据库"""
        try:
            self.db_manager.save_strategy_config(self.settings)
            strategy_logger.info("Strategy settings saved to database")
        except Exception as e:
            strategy_logger.error(f"Failed to save settings to database: {e}")

    def _persist_strategy_settings(self, updates: Optional[Dict[str, Any]] = None, removals: Optional[Iterable[str]] = None) -> None:
        """Persist incremental strategy setting changes while keeping existing entries intact."""

        if not updates and not removals:
            return

        current = dict(self.settings) if isinstance(self.settings, dict) else {}
        changed = False

        if updates:
            updated_primary_symbol: Optional[str] = None
            for key, value in updates.items():
                if current.get(key) != value:
                    current[key] = value
                    changed = True

        if removals:
            for key in removals:
                if key in current:
                    current.pop(key)
                    changed = True

        if not changed:
            return

        try:
            self.db_manager.save_strategy_config(current)
            self.settings = current
        except Exception as exc:
            strategy_logger.error(f"Failed to persist strategy settings: {exc}")

    def _get_strategy_setting(self, strategy_name: str, suffix: str, default: Any = None) -> Any:
        key = f"{strategy_name}_{suffix}"
        return (self.settings or {}).get(key, default)

    def _normalize_strategy_file_display(
        self,
        path_value: Optional[Union[str, Path]],
        module_path: Optional[Path] = None,
    ) -> Optional[str]:
        """Normalize strategy file paths for persistence/display."""

        if path_value is None:
            return None

        text = str(path_value).strip()
        if not text:
            return None

        text = text.replace("\\", "/")

        try:
            candidate = Path(text)
        except Exception:
            return text

        if candidate.is_absolute():
            try:
                return candidate.relative_to(self.strategy_dir).as_posix()
            except ValueError:
                try:
                    return candidate.relative_to(Path.cwd()).as_posix()
                except ValueError:
                    return candidate.as_posix()

        if module_path:
            try:
                module_relative = module_path.relative_to(self.strategy_dir).as_posix()
            except ValueError:
                module_relative = module_path.as_posix()
            if text == module_path.as_posix():
                return module_relative

        return text

    def _resolve_strategy_file_path(self, file_path: Optional[str]) -> Optional[Path]:
        """Resolve absolute filesystem path for an internal strategy module."""

        if not file_path:
            return None

        candidate = Path(file_path)
        search_paths = []
        if candidate.is_absolute():
            search_paths.append(candidate)
        else:
            search_paths.append(Path.cwd() / candidate)
            search_paths.append(self.strategy_dir / candidate)

        for path in search_paths:
            try:
                resolved = path.resolve()
            except FileNotFoundError:
                continue
            if resolved.exists():
                return resolved
        return None

    def _load_internal_strategy_runtime(self, strategy: Optional[Strategy]) -> Optional["BaseStrategy"]:
        """Dynamically load and register an internal strategy runtime instance."""

        if not strategy or strategy.strategy_type != "internal":
            return None

        existing = self.internal_strategies_runtime.get(strategy.name)
        if existing:
            return existing

        module_path = self._resolve_strategy_file_path(strategy.file_path)
        if not module_path:
            strategy_logger.warning(
                f"Strategy file not found for {strategy.name}: {strategy.file_path}"
            )
            return None

        module_key = str(module_path)
        import_name, package_name = self._infer_module_import_context(module_path)
        module_name = self._loaded_internal_modules.get(module_key)

        module = None
        if module_name and module_name in sys.modules:
            module = sys.modules[module_name]
        else:
            if import_name:
                try:
                    module = importlib.import_module(import_name)
                    module_name = import_name
                    self._loaded_internal_modules[module_key] = module_name
                except Exception as exc:
                    strategy_logger.error(
                        f"Failed to import internal strategy module {import_name}: {exc}"
                    )
                    module = None

            if module is None:
                module_name = f"internal_strategy_{hashlib.md5(module_key.encode()).hexdigest()}"
                try:
                    spec = importlib.util.spec_from_file_location(module_name, module_path)
                    if not spec or not spec.loader:
                        raise ImportError("Unable to resolve module specification")
                    module = importlib.util.module_from_spec(spec)
                    if package_name:
                        module.__package__ = package_name
                        parent_pkg = package_name
                        if parent_pkg not in sys.modules:
                            try:
                                importlib.import_module(parent_pkg)
                            except Exception:
                                pass
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)  # type: ignore[arg-type]
                    self._loaded_internal_modules[module_key] = module_name
                except Exception as exc:
                    strategy_logger.error(
                        f"Failed to load internal strategy module {module_path}: {exc}"
                    )
                    if module_name in sys.modules:
                        sys.modules.pop(module_name, None)
                    return None

        if not module:
            return None

        from src.strategies.dom_strategy import DOMStrategy
        from src.strategies.base import BaseStrategy

        strategy_cls: Optional[type] = None
        for attr in module.__dict__.values():
            if (
                isinstance(attr, type)
                and issubclass(attr, DOMStrategy)
                and attr is not DOMStrategy
            ):
                strategy_cls = attr
                break

        if strategy_cls is None:
            for attr in module.__dict__.values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseStrategy)
                    and attr is not BaseStrategy
                ):
                    strategy_cls = attr
                    break

        if strategy_cls is None:
            strategy_logger.warning(
                f"No BaseStrategy subclass found in module {module_path} for {strategy.name}"
            )
            return None

        try:
            instance = strategy_cls()
        except Exception as exc:
            strategy_logger.error(
                f"Failed to instantiate strategy class {strategy_cls.__name__}: {exc}"
            )
            return None

        desired_file_path = self._normalize_strategy_file_display(
            strategy.file_path, module_path
        )
        runtime_file_path = self._normalize_strategy_file_display(
            getattr(instance, "file_path", None), module_path
        )

        if desired_file_path:
            if desired_file_path != runtime_file_path:
                try:
                    setattr(instance, "file_path", desired_file_path)
                    runtime_file_path = desired_file_path
                except Exception:
                    pass
        else:
            fallback_path = self._normalize_strategy_file_display(module_path, module_path)
            if fallback_path and fallback_path != runtime_file_path:
                try:
                    setattr(instance, "file_path", fallback_path)
                    runtime_file_path = fallback_path
                except Exception:
                    pass
            desired_file_path = fallback_path

        if desired_file_path and strategy.file_path != desired_file_path:
            strategy.file_path = desired_file_path

        if getattr(instance, "name", None) != strategy.name:
            try:
                setattr(instance, "name", strategy.name)
            except Exception:
                pass

        try:
            self.register_internal_strategy(instance)
        except Exception as exc:
            strategy_logger.error(
                f"Failed to register internal strategy runtime for {strategy.name}: {exc}"
            )
            return None

        return instance

    def _infer_module_import_context(self, module_path: Path) -> Tuple[Optional[str], Optional[str]]:
        """Determine the import and package names for a strategy module path."""

        try:
            resolved = module_path.resolve()
        except FileNotFoundError:
            return None, None

        import_name: Optional[str] = None
        package_name: Optional[str] = None

        try:
            src_root = Path(__file__).resolve().parent
            relative = resolved.relative_to(src_root)
        except ValueError:
            return None, None

        parts = list(relative.parts)
        if not parts:
            return None, None

        module_stem = resolved.stem
        parent_parts = parts[:-1]

        if parent_parts:
            candidate_parent = ["src", *parent_parts]
            if all(part.isidentifier() for part in candidate_parent):
                package_name = ".".join(candidate_parent)
        else:
            package_name = "src"

        if module_stem.isidentifier() and package_name:
            import_name = f"{package_name}.{module_stem}"

        return import_name, package_name

    def _apply_persisted_parameters(self, name: str, runtime: "BaseStrategy") -> None:
        """Load persisted parameter overrides for a runtime strategy instance."""

        getter = getattr(runtime, "get_parameter_definitions", None)
        updater = getattr(runtime, "apply_parameter_updates", None)
        if not callable(getter) or not callable(updater):
            return

        definitions = getter()
        if not definitions:
            return

        overrides: Dict[str, Any] = {}
        settings = self.settings or {}
        for param_name in definitions.keys():
            key = f"{name}_param_{param_name}"
            if key in settings:
                overrides[param_name] = settings[key]

        if not overrides:
            return

        try:
            updater(overrides)
            strategy_logger.info(
                f"Applied persisted parameters for {name}: {', '.join(sorted(overrides.keys()))}"
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_logger.warning(
                f"Failed to apply persisted parameters for {name}: {exc}"
            )

    def _attach_parameter_schema(
        self, name: str, payload: Dict[str, Any], runtime: Optional["BaseStrategy"]
    ) -> None:
        """Populate parameter metadata for API responses."""

        describer = getattr(runtime, "describe_parameters", None) if runtime else None
        schema: List[Dict[str, Any]] = []
        if callable(describer):
            try:
                schema = describer() or []
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(f"Failed to describe parameters for {name}: {exc}")
                schema = []

        payload["parameters"] = schema
        if "configurable_parameters" not in payload or not payload["configurable_parameters"]:
            payload["configurable_parameters"] = schema

    def _request_data_push_activation(
        self,
        strategy_name: str,
        *,
        subscription: Optional[str] = None,
        symbol: Optional[str] = None,
        force_restart: bool = False,
    ) -> None:
        """Schedule asynchronous activation of a strategy's data push."""

        async def _activate() -> None:
            try:
                result = await self.set_data_push(
                    strategy_name,
                    True,
                    subscription=subscription,
                    symbol=symbol,
                    force_restart=force_restart,
                )
                if not result.get("success"):
                    error_msg = result.get("error") or "unknown error"
                    strategy_logger.warning(
                        f"Auto data push enable failed for {strategy_name}: {error_msg}"
                    )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    f"Failed to auto-start data push for {strategy_name}: {exc}"
                )

        log_subscription = subscription or self._get_strategy_setting(
            strategy_name, "data_subscription"
        )
        log_symbol = symbol or self._get_strategy_setting(strategy_name, "primary_symbol")

        immediate_loop: Optional[asyncio.AbstractEventLoop]
        try:
            immediate_loop = asyncio.get_running_loop()
        except RuntimeError:
            immediate_loop = None

        if immediate_loop and immediate_loop.is_running():
            strategy_logger.info(
                "Activating data push immediately",
                strategy=strategy_name,
                subscription=log_subscription,
                symbol=log_symbol,
                force_restart=force_restart,
            )
            immediate_loop.create_task(_activate())
            return

        target_loop = self._event_loop if self._event_loop and self._event_loop.is_running() else None
        if target_loop:
            strategy_logger.info(
                "Scheduling data push activation on manager loop",
                strategy=strategy_name,
                subscription=log_subscription,
                symbol=log_symbol,
                force_restart=force_restart,
            )
            asyncio.run_coroutine_threadsafe(_activate(), target_loop)
            return

        strategy_logger.info(
            "Deferring data push activation until loop available",
            strategy=strategy_name,
            subscription=log_subscription,
            symbol=log_symbol,
            force_restart=force_restart,
        )
        self._pending_data_push_requests.append(
            (strategy_name, subscription, symbol, force_restart)
        )

    def get_data_push_state(self, strategy_name: str) -> Dict[str, Any]:
        return dict(self.data_push_state.get(strategy_name, {}))

    def _schedule_data_push_disable(
        self,
        strategy_name: str,
        *,
        subscription: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> None:
        """Schedule disabling a data push from synchronous contexts."""

        async def _disable() -> None:
            try:
                await self.set_data_push(
                    strategy_name,
                    False,
                    subscription=subscription,
                    symbol=symbol,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    f"Failed to auto-stop data push for {strategy_name}: {exc}"
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(_disable())
            return

        target_loop = self._event_loop if self._event_loop and self._event_loop.is_running() else None
        if target_loop:
            asyncio.run_coroutine_threadsafe(_disable(), target_loop)
            return

        asyncio.run(_disable())

    async def set_data_push(
        self,
        strategy_name: str,
        enabled: bool,
        subscription: Optional[str] = None,
        symbol: Optional[str] = None,
        force_restart: bool = False,
    ) -> Dict[str, Any]:
        """Control live data push for an internal strategy."""

        record = self.strategies.get(strategy_name)
        if not record or record.strategy_type != "internal":
            return {"success": False, "error": "Strategy not found or not internal"}

        state = self.data_push_state.setdefault(strategy_name, {})
        prior_state = dict(state)
        stored_subscription = subscription or self._get_strategy_setting(strategy_name, "data_subscription")

        target_symbol_candidate = (
            symbol
            or state.get("symbol")
            or getattr(record, "primary_symbol", None)
            or self._get_strategy_setting(strategy_name, "primary_symbol")
        )
        target_symbol = (
            str(target_symbol_candidate).strip().upper()
            if target_symbol_candidate
            else None
        )

        if enabled and not getattr(record, "enabled", True):
            state.update(
                {
                    "enabled": False,
                    "active": False,
                    "subscription": stored_subscription,
                    "symbol": target_symbol,
                    "awaiting_data": False,
                    "last_data_timestamp": None,
                }
            )
            state.pop("_last_gap_warning_at", None)
            state.pop("_last_restart_attempt", None)
            state.pop("restart_requested", None)
            reason = "策略已禁用"
            self._set_data_push_reason(strategy_name, reason)
            self._record_data_push_event(
                strategy_name,
                "数据推送启动失败：策略已禁用",
                level="warning",
                reason=reason,
            )
            strategy_logger.warning(
                "Data push enable rejected - strategy disabled",
                strategy=strategy_name,
            )
            return {
                "success": False,
                "error": "Strategy disabled",
                "status_reason": reason,
                "active": False,
                "subscription": stored_subscription,
                "symbol": target_symbol,
                "awaiting_data": False,
                "last_data_timestamp": None,
            }

        if not stored_subscription:
            state.update(
                {
                    "enabled": False,
                    "active": False,
                    "awaiting_data": False,
                    "last_data_timestamp": None,
                    "subscription": None,
                    "symbol": target_symbol,
                }
            )
            state.pop("_last_gap_warning_at", None)
            if enabled:
                reason = "未配置数据订阅"
                self._record_data_push_event(
                    strategy_name,
                    "数据推送启动失败：未配置订阅",
                    level="warning",
                    reason=reason,
                )
                strategy_logger.warning(
                    "Data push request rejected - subscription missing",
                    strategy=strategy_name,
                    requested_action="enable" if enabled else "disable",
                )
                return {
                    "success": False,
                    "error": "No subscription configured",
                    "status_reason": reason,
                }

            self._set_data_push_reason(strategy_name, "推送已停止")
            return {
                "success": True,
                "active": False,
                "subscription": None,
                "symbol": target_symbol,
                "awaiting_data": False,
                "last_data_timestamp": None,
                "status_reason": "推送已停止",
            }

        strategy_logger.info(
            "Data push change requested",
            strategy=strategy_name,
            requested_action="enable" if enabled else "disable",
            subscription=stored_subscription,
            symbol=target_symbol,
            prior_state=prior_state,
        )

        dom_service = None

        if enabled:
            market_open, session_reason = get_us_market_session()
            if not market_open:
                reason = session_reason or "美股休市"
                state.update(
                    {
                        "enabled": False,
                        "active": False,
                        "subscription": stored_subscription,
                        "symbol": target_symbol,
                        "awaiting_data": False,
                        "last_data_timestamp": None,
                    }
                )
                state.pop("_last_gap_warning_at", None)
                state.pop("_last_restart_attempt", None)
                state.pop("restart_requested", None)
                self._set_data_push_reason(strategy_name, reason)
                self._record_data_push_event(
                    strategy_name,
                    "美股休市，推送已暂停",
                    level="info",
                    reason=reason,
                )
                strategy_logger.info(
                    "Data push blocked due to market closure",
                    strategy=strategy_name,
                    subscription=stored_subscription,
                    symbol=target_symbol,
                    reason=reason,
                )
                return {
                    "success": True,
                    "active": False,
                    "subscription": stored_subscription,
                    "symbol": target_symbol,
                    "awaiting_data": False,
                    "last_data_timestamp": None,
                    "status_reason": reason,
                    "market_closed": True,
                }
            if not target_symbol:
                state.update({"enabled": False, "active": False, "awaiting_data": False, "last_data_timestamp": None})
                state.pop("_last_gap_warning_at", None)
                reason = "未设置主交易标的"
                self._record_data_push_event(
                    strategy_name,
                    "数据推送启动失败：缺少主交易标的",
                    level="warning",
                    reason=reason,
                )
                strategy_logger.warning(
                    "Data push enable failed - primary symbol missing",
                    strategy=strategy_name,
                    subscription=stored_subscription,
                )
                return {
                    "success": False,
                    "error": "Primary symbol required",
                    "status_reason": reason,
                }

            ok = False
            error: Optional[str] = None
            try:
                if stored_subscription == "dom_trend":
                    from src.dom_service import get_dom_service

                    dom_service = get_dom_service()
                    strategy_logger.info(
                        "Starting DOM stream for strategy",
                        strategy=strategy_name,
                        symbol=target_symbol,
                    )
                    restart_requested = force_restart or bool(state.get("restart_requested"))
                    try:
                        ok = await dom_service.start_stream(
                            target_symbol,
                            force_restart=restart_requested,
                        )
                    except Exception as exc:
                        error = str(exc)
                        ok = False
                    if not ok and not restart_requested:
                        strategy_logger.info(
                            "Retrying DOM stream start with forced restart",
                            strategy=strategy_name,
                            symbol=target_symbol,
                        )
                        try:
                            ok = await dom_service.start_stream(
                                target_symbol,
                                force_restart=True,
                            )
                            if ok:
                                restart_requested = True
                        except Exception as exc:
                            error = str(exc)
                            ok = False
                    if dom_service and not ok and not error:
                        try:
                            dom_error, _status = dom_service.get_last_start_error()
                        except Exception:
                            dom_error = None
                        if dom_error:
                            error = dom_error
                elif stored_subscription == "market_data":
                    try:
                        import core

                        if not core.connection_manager.is_connected():
                            error = "IBKR connection required"
                            ok = False
                        else:
                            if core.ibkr_client is None:
                                core.ibkr_client = core.IBKRClient()
                            data = await core.ibkr_client.get_real_time_data(target_symbol)
                            ok = bool(data)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        error = str(exc)
                        ok = False
                else:
                    error = f"Unsupported subscription: {stored_subscription}"
            except Exception as exc:  # pragma: no cover - defensive guard
                error = str(exc)
                ok = False

            if not ok:
                state.update({
                    "enabled": False,
                    "active": False,
                    "subscription": stored_subscription,
                    "symbol": target_symbol,
                    "awaiting_data": False,
                    "last_data_timestamp": None,
                })
                state.pop("_last_gap_warning_at", None)
                state.pop("restart_requested", None)
                reason = f"启动失败：{error}" if error else "启动失败"
                self._record_data_push_event(
                    strategy_name,
                    "数据推送启动失败",
                    level="error",
                    reason=reason,
                    details={"error": error} if error else None,
                )
                strategy_logger.error(
                    "Failed to enable data push",
                    strategy=strategy_name,
                    subscription=stored_subscription,
                    symbol=target_symbol,
                    error=error or "Failed to start data push",
                )
                payload = {
                    "success": False,
                    "error": error or "Failed to start data push",
                    "status_reason": reason,
                }
                return payload

            runtime = self.internal_strategies_runtime.get(strategy_name)
            if runtime is None:
                runtime = self._load_internal_strategy_runtime(record)
            if runtime:
                if target_symbol and hasattr(runtime, "symbol"):
                    try:
                        setattr(runtime, "symbol", target_symbol)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        strategy_logger.warning(
                            f"Failed to update runtime symbol for {strategy_name}: {exc}"
                        )
                if dom_service and hasattr(runtime, "set_dependencies"):
                    try:
                        runtime.set_dependencies(dom_service=dom_service)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        strategy_logger.warning(
                            f"Failed to refresh DOM dependency for {strategy_name}: {exc}"
                        )
                if getattr(runtime, "enabled", False) and not getattr(runtime, "active", False):
                    try:
                        runtime.start()
                    except Exception as exc:  # pragma: no cover - defensive guard
                        strategy_logger.warning(f"Failed to start runtime for {strategy_name}: {exc}")

            state.update({
                "enabled": True,
                "active": True,
                "subscription": stored_subscription,
                "symbol": target_symbol,
                "last_started": now_local().isoformat(),
                "awaiting_data": True,
                "last_data_timestamp": None,
            })
            if force_restart:
                state["_last_restart_attempt"] = time.time()
            else:
                state.pop("_last_restart_attempt", None)
            state.pop("restart_requested", None)
            state.pop("_last_gap_warning_at", None)
            reason_text = "已启动推送，等待数据"
            if force_restart:
                reason_text = "重新订阅后等待数据"
            self._record_data_push_event(
                strategy_name,
                "数据推送已启动",
                level="info",
                reason=reason_text,
                details={"subscription": stored_subscription, "symbol": target_symbol},
            )
            strategy_logger.info(
                "Data push enabled",
                strategy=strategy_name,
                subscription=stored_subscription,
                symbol=target_symbol,
            )
            return {
                "success": True,
                "active": True,
                "subscription": stored_subscription,
                "symbol": target_symbol,
                "awaiting_data": True,
                "last_data_timestamp": None,
                "status_reason": reason_text,
            }

        # Disable
        try:
            if state.get("subscription") == "dom_trend" and state.get("symbol"):
                from src.dom_service import get_dom_service

                dom_service = get_dom_service()
                await dom_service.stop_stream(state.get("symbol") or "")
        except Exception:  # pragma: no cover - defensive guard
            pass

        state.update({
            "enabled": False,
            "active": False,
            "subscription": stored_subscription,
            "symbol": target_symbol,
            "awaiting_data": False,
            "last_data_timestamp": None,
        })
        state.pop("_last_gap_warning_at", None)
        state.pop("_last_restart_attempt", None)
        state.pop("restart_requested", None)
        self._record_data_push_event(
            strategy_name,
            "数据推送已停止",
            level="info",
            reason="推送已停止",
        )
        strategy_logger.info(
            "Data push disabled",
            strategy=strategy_name,
            subscription=stored_subscription,
            symbol=target_symbol,
        )
        return {
            "success": True,
            "active": False,
            "subscription": stored_subscription,
            "symbol": target_symbol,
            "awaiting_data": False,
            "last_data_timestamp": None,
            "status_reason": "推送已停止",
        }

    async def restore_data_push_states(self) -> None:
        """Attempt to enable stored data push subscriptions for internal strategies."""

        pending_requests = list(self._pending_data_push_requests)
        self._pending_data_push_requests.clear()

        if pending_requests:
            strategy_logger.info(
                "Processing deferred data push activations",
                count=len(pending_requests),
            )

        for strategy_name, subscription, symbol, force_restart in pending_requests:
            try:
                strategy_logger.info(
                    "Replaying deferred data push request",
                    strategy=strategy_name,
                    subscription=subscription,
                    symbol=symbol,
                    force_restart=force_restart,
                )
                await self.set_data_push(
                    strategy_name,
                    True,
                    subscription=subscription,
                    symbol=symbol,
                    force_restart=force_restart,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    f"Deferred data push activation failed for {strategy_name}: {exc}"
                )

        for record in list(self.strategies.values()):
            if record.strategy_type != "internal" or not record.enabled:
                continue

            state = self.data_push_state.get(record.name)
            if state and state.get("active"):
                continue

            subscription = self._get_strategy_setting(record.name, "data_subscription")
            if not subscription:
                continue

            target_symbol = (
                self._get_strategy_setting(record.name, "primary_symbol")
                or getattr(record, "primary_symbol", None)
            )

            if not target_symbol:
                continue

            try:
                strategy_logger.info(
                    "Restoring stored data push",
                    strategy=record.name,
                    subscription=subscription,
                    symbol=target_symbol,
                )
                result = await self.set_data_push(
                    record.name,
                    True,
                    subscription=subscription,
                    symbol=target_symbol,
                )
                if not result.get("success"):
                    error_msg = result.get("error") or "unknown error"
                    strategy_logger.warning(
                        f"Failed to restore data push for {record.name}: {error_msg}"
                    )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    f"Exception while restoring data push for {record.name}: {exc}"
                )

    def _handle_position_setting_change(self, strategy_name: str) -> None:
        """Adjust runtime helpers when position-related settings are updated."""

        close_minutes = self._get_strategy_setting(strategy_name, "close_before_close_minutes")
        try:
            close_value = int(close_minutes) if close_minutes is not None else 0
        except (TypeError, ValueError):
            close_value = 0

        if close_value <= 0:
            keys_to_cancel = [key for key in list(self._auto_close_tasks.keys()) if key[0] == strategy_name]
            for key in keys_to_cancel:
                task = self._auto_close_tasks.pop(key, None)
                if task:
                    task.cancel()

    def _rename_internal_state(self, old_name: str, new_name: str) -> None:
        if old_name == new_name:
            return

        for key in list(self._last_position_add.keys()):
            strategy_key, symbol, direction = key
            if strategy_key == old_name:
                timestamp = self._last_position_add.pop(key)
                self._last_position_add[(new_name, symbol, direction)] = timestamp

        for key in list(self._auto_close_tasks.keys()):
            strategy_key, symbol = key
            if strategy_key == old_name:
                task = self._auto_close_tasks.pop(key)
                self._auto_close_tasks[(new_name, symbol)] = task

    def _cleanup_internal_state(self, strategy_name: str) -> None:
        for key in list(self._last_position_add.keys()):
            if key[0] == strategy_name:
                self._last_position_add.pop(key, None)

        for key in list(self._auto_close_tasks.keys()):
            if key[0] == strategy_name:
                task = self._auto_close_tasks.pop(key, None)
                if task:
                    task.cancel()

    def _load_strategies(self):
        """Load strategies from CSV file - 兼容性方法"""
        if self.strategies_file.exists():
            try:
                with open(self.strategies_file, 'r', newline='') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # Convert string values back to appropriate types
                        row['enabled'] = row['enabled'].lower() == 'true'
                        if row['created_at']:
                            row['created_at'] = parse_local_datetime(row['created_at'])
                        if row['last_triggered']:
                            row['last_triggered'] = parse_local_datetime(row['last_triggered'])
                        else:
                            row['last_triggered'] = None
                        if row['trigger_count']:
                            row['trigger_count'] = int(row['trigger_count'])
                        
                        # Handle empty/None values
                        for key in ['api_endpoint', 'file_path', 'description']:
                            if not row[key]:
                                row[key] = None
                        
                        self.strategies[row['name']] = Strategy(**row)
                        
                strategy_logger.info(f"Loaded {len(self.strategies)} strategies from CSV")
            except Exception as e:
                strategy_logger.error(f"Failed to load strategies: {e}")
                self.strategies = {}
                
    def _save_strategies(self):
        """Save strategies - 现在保存到数据库"""
        try:
            for strategy in self.strategies.values():
                db_strategy = DatabaseStrategy(
                    name=strategy.name,
                    strategy_type=strategy.strategy_type,
                    enabled=strategy.enabled,
                    api_endpoint=strategy.api_endpoint,
                    file_path=strategy.file_path,
                    description=strategy.description,
                    created_at=to_local_iso(strategy.created_at) if strategy.created_at else now_local().isoformat()
                )
                self.db_manager.save_strategy(db_strategy)
            strategy_logger.info("Strategies saved to database")
            self._invalidate_strategies_cache()
        except Exception as e:
            strategy_logger.error(f"Failed to save strategies to database: {e}")
            
    def get_settings(self) -> Dict[str, Any]:
        """Get current settings"""
        return self.settings.copy()
        
    def update_settings(self, new_settings: Dict[str, Any]) -> bool:
        """Update settings"""
        try:
            # 过滤掉值为 None 的字段，避免覆盖已有设置
            cleaned = {k: v for k, v in new_settings.items() if v is not None}
            if "strategy_path" in cleaned:
                cleaned["strategy_path"] = self._normalize_strategy_path_value(cleaned["strategy_path"])
            self.settings.update(cleaned)
            self._save_settings()
            self._load_from_database()  # 重新加载以更新实例变量
            return True
        except Exception as e:
            strategy_logger.error(f"Failed to update settings: {e}")
            return False
            
    def add_strategy(self, strategy_data: Dict[str, Any]) -> bool:
        """Add a new strategy"""
        try:
            name = strategy_data['name']
            if name in self.strategies:
                strategy_logger.warning(f"Strategy {name} already exists")
                return False
            
            # 处理内部策略的文件选择逻辑
            final_file_path = strategy_data.get('file_path')
            if strategy_data['strategy_type'] == "internal":
                selected_file = strategy_data.get('selected_file')
                use_template = strategy_data.get('use_template', False)
                
                if selected_file and selected_file != "new":
                    # 选择了现有文件
                    final_file_path = selected_file
                    # 如果名称为空或使用默认名称，自动从文件名生成
                    if not name or name == selected_file:
                        name = Path(selected_file).stem  # 去掉.py扩展名
                        # 检查生成的名称是否已存在
                        counter = 1
                        original_name = name
                        while name in self.strategies:
                            name = f"{original_name}_{counter}"
                            counter += 1
                elif use_template or selected_file == "new":
                    # 创建新文件或使用模板
                    if selected_file == "new":
                        name = slugify_strategy_name(name)
                        counter = 1
                        original_name = name
                        while name in self.strategies:
                            name = f"{original_name}_{counter}"
                            counter += 1
                        final_file_path = f"{name}.py"
                    else:
                        final_file_path = f"{name}.py"
                
            strategy = Strategy(
                name=name,
                strategy_type=strategy_data['strategy_type'],
                enabled=strategy_data.get('enabled', True),
                api_endpoint=strategy_data.get('api_endpoint'),
                file_path=final_file_path,
                description=strategy_data.get('description'),
                created_at=now_local(),
                schedule=strategy_data.get('schedule')
            )
            
            # 保存到数据库
            db_strategy = DatabaseStrategy(
                name=strategy.name,
                strategy_type=strategy.strategy_type,
                enabled=strategy.enabled,
                api_endpoint=strategy.api_endpoint,
                file_path=strategy.file_path,
                description=strategy.description,
                created_at=to_local_iso(strategy.created_at)
            )
            self.db_manager.save_strategy(db_strategy)
            
            # 更新内存缓存
            self.strategies[name] = strategy
            
            # 如果是内部策略，处理文件创建（仅在新建文件时创建模板）
            if strategy.strategy_type == "internal":
                selected_file = strategy_data.get('selected_file')
                use_template = strategy_data.get('use_template', False)
                
                # 仅当用户选择“新建文件”时才创建模板，选择现有文件一律不写入
                if selected_file == "new" and strategy.file_path:
                    self._create_strategy_template(name, strategy.file_path)
            
            # 保存SL/TP配置到策略设置
            sl_tp_settings = {}
            if strategy_data.get('sl_enabled'):
                sl_tp_settings[f"{name}_sl_enabled"] = True
                sl_tp_settings[f"{name}_sl_price"] = strategy_data.get('sl_price')
                sl_tp_settings[f"{name}_sl_type"] = strategy_data.get('sl_type', 'amount')
            if strategy_data.get('tp_enabled'):
                sl_tp_settings[f"{name}_tp_enabled"] = True
                sl_tp_settings[f"{name}_tp_price"] = strategy_data.get('tp_price')
                sl_tp_settings[f"{name}_tp_type"] = strategy_data.get('tp_type', 'amount')
                
            if sl_tp_settings:
                self._persist_strategy_settings(sl_tp_settings)
                strategy_logger.info(f"Saved SL/TP settings for strategy {name}")

            # 保存schedule配置
            if strategy.schedule:
                schedule_key = f"{name}_schedule"
                self._persist_strategy_settings({schedule_key: strategy.schedule})

            # Position & subscription specific settings for internal strategies
            if strategy.strategy_type == "internal":
                position_updates: Dict[str, Any] = {}
                data_subscription = strategy_data.get('data_subscription')
                if data_subscription:
                    position_updates[f"{name}_data_subscription"] = data_subscription
                primary_symbol = strategy_data.get('primary_symbol')
                if primary_symbol:
                    position_updates[f"{name}_primary_symbol"] = primary_symbol
                    strategy.primary_symbol = primary_symbol
                cooldown = strategy_data.get('position_add_cooldown_minutes')
                if isinstance(cooldown, (int, float)) and cooldown > 0:
                    position_updates[f"{name}_position_add_cooldown_minutes"] = int(cooldown)
                close_minutes = strategy_data.get('close_before_close_minutes')
                if isinstance(close_minutes, (int, float)) and close_minutes > 0:
                    position_updates[f"{name}_close_before_close_minutes"] = int(close_minutes)
                if position_updates:
                    self._persist_strategy_settings(position_updates)

                if strategy.enabled:
                    stored_subscription = data_subscription or self._get_strategy_setting(
                        name, "data_subscription"
                    )
                    stored_symbol = (
                        primary_symbol
                        or getattr(strategy, "primary_symbol", None)
                        or self._get_strategy_setting(name, "primary_symbol")
                    )
                    if stored_subscription and stored_symbol:
                        self._request_data_push_activation(
                            name,
                            subscription=stored_subscription,
                            symbol=stored_symbol,
                        )

            strategy_logger.info(f"Strategy {name} added successfully")
            # 重新启动调度器以应用新的调度配置
            self._invalidate_strategies_cache()
            self.restart_scheduler()
            return True
            
        except Exception as e:
            strategy_logger.error(f"Failed to add strategy: {e}")
            return False

    def get_strategy(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a strategy by name"""
        if name in self.strategies:
            strategy = self.strategies[name]
            strategy_dict = asdict(strategy)
            order_stats = self.db_manager.get_strategy_order_stats([strategy.name], 'strategy')
            stats = order_stats.get(strategy.name, {})
            strategy_dict['trigger_count'] = int(stats.get('order_count', 0) or 0)
            last_created_at = stats.get('last_created_at')
            if last_created_at:
                parsed_time = parse_local_datetime(last_created_at)
                strategy_dict['last_triggered'] = parsed_time or last_created_at
            else:
                strategy_dict['last_triggered'] = None
            # Convert datetime objects to strings for JSON serialization
            if strategy_dict['created_at'] and isinstance(strategy_dict['created_at'], datetime):
                strategy_dict['created_at'] = to_local_iso(strategy_dict['created_at'])
            if strategy_dict['last_triggered'] and isinstance(strategy_dict['last_triggered'], datetime):
                strategy_dict['last_triggered'] = to_local_iso(strategy_dict['last_triggered'])
            # 合并SL/TP设置
            try:
                settings = self.db_manager.load_strategy_config()
            except Exception:
                settings = getattr(self, 'settings', {}) or {}
            sl_enabled = settings.get(f"{name}_sl_enabled", False)
            tp_enabled = settings.get(f"{name}_tp_enabled", False)
            strategy_dict['sl_enabled'] = sl_enabled
            strategy_dict['tp_enabled'] = tp_enabled
            stored_primary = settings.get(f"{name}_primary_symbol")
            if stored_primary:
                strategy_dict['primary_symbol'] = stored_primary
            elif getattr(strategy, 'primary_symbol', None):
                strategy_dict['primary_symbol'] = strategy.primary_symbol
            strategy_dict['sl_price'] = settings.get(f"{name}_sl_price") if sl_enabled else None
            strategy_dict['tp_price'] = settings.get(f"{name}_tp_price") if tp_enabled else None
            strategy_dict['sl_type'] = settings.get(f"{name}_sl_type", 'amount')
            strategy_dict['tp_type'] = settings.get(f"{name}_tp_type", 'amount')
            strategy_dict['schedule'] = settings.get(f"{name}_schedule")
            strategy_dict['data_subscription'] = settings.get(f"{name}_data_subscription")
            cooldown_val = settings.get(f"{name}_position_add_cooldown_minutes")
            try:
                strategy_dict['position_add_cooldown_minutes'] = int(cooldown_val) if cooldown_val is not None else None
            except (TypeError, ValueError):
                strategy_dict['position_add_cooldown_minutes'] = None
            close_val = settings.get(f"{name}_close_before_close_minutes")
            try:
                strategy_dict['close_before_close_minutes'] = int(close_val) if close_val is not None else None
            except (TypeError, ValueError):
                strategy_dict['close_before_close_minutes'] = None

            runtime = self.internal_strategies_runtime.get(name)
            if runtime:
                self._attach_parameter_schema(name, strategy_dict, runtime)
            else:
                strategy_dict.setdefault("parameters", [])
                strategy_dict.setdefault("configurable_parameters", [])
            return strategy_dict
        return None

    def get_strategy_snapshot(self, name: str) -> Optional[Dict[str, Any]]:
        """Return a lightweight strategy snapshot without hitting the database."""

        record = self.strategies.get(name)
        if not record:
            return None

        snapshot = asdict(record)

        created_at = snapshot.get("created_at")
        if isinstance(created_at, datetime):
            snapshot["created_at"] = to_local_iso(created_at)

        last_triggered = snapshot.get("last_triggered")
        if isinstance(last_triggered, datetime):
            snapshot["last_triggered"] = to_local_iso(last_triggered)

        settings = getattr(self, "settings", {}) or {}
        snapshot["sl_enabled"] = settings.get(f"{name}_sl_enabled", False)
        snapshot["tp_enabled"] = settings.get(f"{name}_tp_enabled", False)
        snapshot["sl_type"] = settings.get(f"{name}_sl_type", "amount")
        snapshot["tp_type"] = settings.get(f"{name}_tp_type", "amount")
        snapshot["sl_price"] = (
            settings.get(f"{name}_sl_price") if snapshot["sl_enabled"] else None
        )
        snapshot["tp_price"] = (
            settings.get(f"{name}_tp_price") if snapshot["tp_enabled"] else None
        )

        primary_symbol = snapshot.get("primary_symbol") or settings.get(
            f"{name}_primary_symbol"
        )
        if primary_symbol:
            snapshot["primary_symbol"] = primary_symbol

        data_subscription = settings.get(f"{name}_data_subscription")
        if data_subscription:
            snapshot["data_subscription"] = data_subscription

        schedule = settings.get(f"{name}_schedule")
        if schedule:
            snapshot["schedule"] = schedule

        snapshot["trigger_count"] = int(snapshot.get("trigger_count", 0) or 0)
        snapshot.setdefault("last_triggered", None)

        return snapshot

    def get_internal_strategy_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Return consolidated information for an internal strategy"""
        record = self.strategies.get(name)
        if record and record.strategy_type == "internal":
            self._load_internal_strategy_runtime(record)

        strategy = self.get_strategy(name)
        if not strategy or strategy.get("strategy_type") != "internal":
            return None

        saved_symbol = self._get_strategy_setting(name, "primary_symbol")
        if saved_symbol and not strategy.get("primary_symbol"):
            strategy["primary_symbol"] = saved_symbol

        symbols: Set[str] = set()
        schedule_items = strategy.get("schedule") or []
        if isinstance(schedule_items, list):
            for item in schedule_items:
                if isinstance(item, dict):
                    sym = item.get("symbol")
                    if sym:
                        symbols.add(sym)

        runtime = self.internal_strategies_runtime.get(name)
        if runtime is None:
            runtime = self._load_internal_strategy_runtime(self.strategies.get(name))
        runtime_risk_rules: Dict[str, Any] = {}
        persisted_file_path = strategy.get("file_path")
        runtime_file_path: Optional[str] = None

        if runtime:
            runtime_symbol = getattr(runtime, "symbol", None)
            if runtime_symbol:
                symbols.add(runtime_symbol)
            info_provider = getattr(runtime, "get_internal_info", None)
            if callable(info_provider):
                try:
                    runtime_info = info_provider() or {}
                except Exception as exc:  # pragma: no cover - defensive guard
                    strategy_logger.warning(f"Runtime info for {name} failed: {exc}")
                    runtime_info = {}
                if isinstance(runtime_info, dict):
                    runtime_file_path = runtime_info.get("file_path") or runtime_file_path
                    extracted_rules = runtime_info.pop("risk_rules", {})
                    if isinstance(extracted_rules, dict):
                        runtime_risk_rules.update(extracted_rules)
                        symbols.update(extracted_rules.keys())
                    for key, value in runtime_info.items():
                        if key in {"name", "strategy_type", "file_path"}:
                            continue
                        if value is None:
                            continue
                        strategy[key] = value

        display_file_path: Optional[str] = None

        if persisted_file_path:
            module_path = self._resolve_strategy_file_path(persisted_file_path)
            display_file_path = self._normalize_strategy_file_display(
                persisted_file_path, module_path
            )

        if not display_file_path and runtime_file_path:
            runtime_module_path = self._resolve_strategy_file_path(runtime_file_path)
            display_file_path = self._normalize_strategy_file_display(
                runtime_file_path, runtime_module_path
            )

        if display_file_path:
            strategy["file_path"] = display_file_path
            if runtime and getattr(runtime, "file_path", None) != display_file_path:
                try:
                    setattr(runtime, "file_path", display_file_path)
                except Exception:
                    pass

        self._attach_parameter_schema(name, strategy, runtime)

        combined_rules: Dict[str, Any] = {}
        if runtime_risk_rules:
            combined_rules.update(runtime_risk_rules)

        if self.risk_manager and hasattr(self.risk_manager, "risk_rules"):
            for sym in symbols:
                rule = self.risk_manager.risk_rules.get(sym)
                if rule:
                    combined_rules[sym] = asdict(rule)

        strategy["risk_rules"] = combined_rules

        position_settings: Dict[str, Any] = {}
        cooldown_value = strategy.get("position_add_cooldown_minutes")
        if cooldown_value:
            position_settings["position_add_cooldown_minutes"] = cooldown_value
        close_minutes = strategy.get("close_before_close_minutes")
        if close_minutes:
            position_settings["close_before_close_minutes"] = close_minutes
        if position_settings:
            strategy["position_settings"] = position_settings

        if self.strategy_risk_manager:
            try:
                strategy["strategy_risk_settings"] = self.strategy_risk_manager.get_settings(name)
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    "Failed to load strategy risk settings",
                    strategy=name,
                    error=str(exc),
                )

        return strategy

    def get_strategy_risk_settings(self, name: str) -> Dict[str, Any]:
        if name not in self.strategies:
            return {}
        if not self.strategy_risk_manager:
            return {}
        return self.strategy_risk_manager.get_settings(name)

    def update_strategy_risk_settings(self, name: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self.strategies:
            raise ValueError("Strategy not found")
        if not self.strategy_risk_manager:
            raise ValueError("Strategy risk manager not configured")
        return self.strategy_risk_manager.update_settings(name, updates)

    def update_strategy_parameter(self, name: str, parameter_name: str, value: Any) -> Dict[str, Any]:
        """Update and persist a single runtime strategy parameter."""

        if name not in self.strategies:
            raise ValueError("Strategy not found")

        runtime = self.internal_strategies_runtime.get(name)
        if runtime is None:
            runtime = self._load_internal_strategy_runtime(self.strategies.get(name))

        if runtime is None:
            raise ValueError("Strategy runtime unavailable")

        getter = getattr(runtime, "get_parameter_definitions", None)
        updater = getattr(runtime, "apply_parameter_updates", None)
        describer = getattr(runtime, "describe_parameters", None)

        if not callable(getter) or not callable(updater):
            raise ValueError("Strategy does not support parameter updates")

        definitions = getter() or {}
        if parameter_name not in definitions:
            raise ValueError(f"Unknown parameter '{parameter_name}'")

        applied = updater({parameter_name: value})

        settings_to_save: Dict[str, Any] = {}
        settings_to_remove: Set[str] = set()
        for key, applied_value in applied.items():
            storage_key = f"{name}_param_{key}"
            if applied_value is None:
                settings_to_remove.add(storage_key)
            else:
                settings_to_save[storage_key] = applied_value

        self._persist_strategy_settings(settings_to_save, settings_to_remove)

        parameter_payload: Dict[str, Any] = {}
        if callable(describer):
            try:
                schema = describer() or []
            except Exception:
                schema = []
            if isinstance(schema, list):
                for entry in schema:
                    if isinstance(entry, dict) and entry.get("name") == parameter_name:
                        parameter_payload = dict(entry)
                        break

        if not parameter_payload:
            parameter_payload = dict(definitions.get(parameter_name, {}))
            parameter_payload["name"] = parameter_name
            parameter_payload["value"] = applied.get(parameter_name)

        return parameter_payload

    def get_internal_strategy_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Return runtime status metrics for an internal strategy."""

        runtime = self.internal_strategies_runtime.get(name)
        if runtime is None:
            runtime = self._load_internal_strategy_runtime(self.strategies.get(name))
        status_provider = getattr(runtime, "get_runtime_status", None) if runtime else None
        default_status: Dict[str, Any] = {
            "is_receiving_data": False,
            "runtime_seconds": 0.0,
            "processed_count": 0,
            "threshold_hits": 0,
            "buy_signals": 0,
            "sell_signals": 0,
        }

        status: Dict[str, Any] = dict(default_status)

        market_open, market_reason = get_us_market_session()
        status["is_us_market_open"] = bool(market_open)
        if market_reason:
            status["market_status_reason"] = market_reason

        def _coerce_price(value: Any) -> Optional[float]:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        sl_enabled = bool(self._get_strategy_setting(name, "sl_enabled", False))
        tp_enabled = bool(self._get_strategy_setting(name, "tp_enabled", False))
        raw_sl = self._get_strategy_setting(name, "sl_price") if sl_enabled else None
        raw_tp = self._get_strategy_setting(name, "tp_price") if tp_enabled else None
        status["sl_enabled"] = sl_enabled
        status["tp_enabled"] = tp_enabled
        status["sl_price"] = _coerce_price(raw_sl) if sl_enabled else None
        status["tp_price"] = _coerce_price(raw_tp) if tp_enabled else None

        if callable(status_provider):
            try:
                runtime_status = status_provider()
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(f"Runtime status for {name} failed: {exc}")
            else:
                if isinstance(runtime_status, dict):
                    status.update(runtime_status)

        push_state = self.data_push_state.get(name)
        if push_state:
            now_ts = time.time()
            was_awaiting = bool(push_state.get("awaiting_data"))
            active_flag = bool(push_state.get("active"))
            subscription_value = push_state.get("subscription") or self._get_strategy_setting(
                name, "data_subscription"
            )
            symbol_value = push_state.get("symbol") or self._get_strategy_setting(
                name, "primary_symbol"
            )

            last_data_ts = status.get("last_data_timestamp")
            last_data_iso: Optional[str] = None
            numeric_last: Optional[float] = None
            if isinstance(last_data_ts, (int, float)) and last_data_ts > 0:
                numeric_last = float(last_data_ts)
                try:
                    last_data_iso = to_local_iso(ensure_local_timezone(datetime.fromtimestamp(numeric_last)))
                except Exception:  # pragma: no cover - defensive guard
                    last_data_iso = None
            elif isinstance(last_data_ts, str):
                try:
                    numeric_last = float(last_data_ts)
                except (TypeError, ValueError):
                    numeric_last = None

            processed_raw = status.get("processed_count")
            try:
                processed_count = int(processed_raw)
            except (TypeError, ValueError):
                processed_count = None

            last_processed_update = float(push_state.get("_last_processed_update", 0.0) or 0.0)
            prev_processed = push_state.get("_last_processed_count")
            awaiting_data = was_awaiting

            if processed_count is not None:
                if prev_processed is None or processed_count > prev_processed:
                    push_state["_last_processed_count"] = processed_count
                    last_processed_update = now_ts
                    push_state["_last_processed_update"] = last_processed_update
                    if awaiting_data:
                        push_state["awaiting_data"] = False
                        awaiting_data = False
                        push_state.pop("_last_gap_warning_at", None)
                elif last_processed_update == 0.0 and prev_processed is not None:
                    last_processed_update = now_ts
                    push_state["_last_processed_update"] = last_processed_update
            elif last_processed_update > 0:
                pass

            activity_candidates: List[float] = []
            if numeric_last and numeric_last > 0:
                activity_candidates.append(numeric_last)
            if last_processed_update > 0:
                activity_candidates.append(last_processed_update)
            activity_ts = max(activity_candidates) if activity_candidates else None

            receiving_grace = self.settings.get("data_push_receiving_grace", 12.0)
            try:
                receiving_grace = float(receiving_grace)
            except (TypeError, ValueError):
                receiving_grace = 12.0
            receiving_grace = max(5.0, receiving_grace)

            recent_activity = bool(activity_ts and (now_ts - activity_ts) <= receiving_grace)
            if recent_activity and not status.get("is_receiving_data"):
                status["is_receiving_data"] = True

            is_receiving = bool(status.get("is_receiving_data"))
            if not market_open:
                active_flag = False
                awaiting_data = False
                push_state["active"] = False
                push_state["awaiting_data"] = False
                closed_reason = market_reason or "美股休市"
                self._set_data_push_reason(name, closed_reason)
                status["data_push_reason"] = closed_reason
                status.setdefault("data_push", {})["status_reason"] = closed_reason
                status["data_push_market_closed"] = True
                if push_state.get("enabled"):
                    self._schedule_data_push_disable(
                        name,
                        subscription=subscription_value,
                        symbol=symbol_value,
                    )

            if is_receiving and was_awaiting:
                strategy_logger.info(
                    "Data push is now receiving data",
                    strategy=name,
                    subscription=push_state.get("subscription"),
                    symbol=push_state.get("symbol"),
                )
                self._record_data_push_event(
                    name,
                    "已开始接收DOM数据",
                    level="info",
                    reason="正在接收数据",
                )
            if is_receiving:
                push_state.pop("restart_requested", None)
                self._set_data_push_reason(name, "正在接收数据")
                awaiting_data = False
            elif awaiting_data and recent_activity:
                # Fallback: treat recent activity as receiving even if runtime delayed update
                awaiting_data = False
                push_state["awaiting_data"] = False

            if is_receiving or not active_flag:
                if awaiting_data:
                    push_state["awaiting_data"] = False
                awaiting_data = False
                push_state.pop("_last_gap_warning_at", None)
                if not active_flag:
                    self._set_data_push_reason(name, "推送已停止")

            if activity_ts:
                try:
                    candidate_iso = to_local_iso(
                        ensure_local_timezone(datetime.fromtimestamp(activity_ts))
                    )
                except Exception:  # pragma: no cover - defensive guard
                    candidate_iso = None
                if candidate_iso:
                    last_data_iso = candidate_iso
                numeric_last = activity_ts

            payload = {
                "enabled": bool(push_state.get("enabled")),
                "active": bool(push_state.get("active")),
                "subscription": push_state.get("subscription"),
                "symbol": push_state.get("symbol"),
                "last_started": push_state.get("last_started"),
                "awaiting_data": awaiting_data,
            }
            if last_data_iso:
                payload["last_data_timestamp"] = last_data_iso
                push_state["last_data_timestamp"] = last_data_iso
            elif "last_data_timestamp" not in push_state:
                push_state["last_data_timestamp"] = None

            if active_flag and not is_receiving:
                last_warning_at = float(push_state.get("_last_gap_warning_at", 0.0) or 0.0)
                if numeric_last is not None and numeric_last > 0:
                    elapsed = max(0.0, now_ts - numeric_last)
                    if elapsed >= 15.0:
                        if (now_ts - last_warning_at) >= 30.0:
                            strategy_logger.warning(
                                "Data push stalled",
                                strategy=name,
                                subscription=push_state.get("subscription"),
                                symbol=push_state.get("symbol"),
                                seconds_since_last=round(elapsed, 1),
                                processed=status.get("processed_count"),
                            )
                            push_state["_last_gap_warning_at"] = now_ts
                            self._record_data_push_event(
                                name,
                                "检测到数据中断",
                                level="warning",
                                reason="检测到数据中断，准备重试",
                                details={"seconds_since_last": round(elapsed, 1)},
                            )

                        restart_ts = float(push_state.get("_last_restart_attempt", 0.0) or 0.0)
                        restart_setting = self.settings.get("data_push_restart_cooldown", 90)
                        try:
                            restart_cooldown = float(restart_setting)
                        except (TypeError, ValueError):
                            restart_cooldown = 90.0
                        restart_cooldown = max(30.0, restart_cooldown)
                        if (
                            subscription_value == "dom_trend"
                            and symbol_value
                            and (now_ts - restart_ts) >= restart_cooldown
                        ):
                            strategy_logger.info(
                                "Scheduling DOM data push restart after stall",
                                strategy=name,
                                subscription=subscription_value,
                                symbol=symbol_value,
                                seconds_since_last=round(elapsed, 1),
                            )
                            push_state["_last_restart_attempt"] = now_ts
                            push_state["awaiting_data"] = True
                            push_state["restart_requested"] = True
                            self._record_data_push_event(
                                name,
                                "触发重新订阅以恢复数据",
                                level="info",
                                reason="重新订阅后等待数据",
                            )
                            self._set_data_push_reason(name, "重新订阅后等待数据")
                            self._request_data_push_activation(
                                name,
                                subscription=subscription_value,
                                symbol=symbol_value,
                                force_restart=True,
                            )
                elif not awaiting_data and (now_ts - last_warning_at) >= 30.0:
                    strategy_logger.warning(
                        "Data push active but no last data timestamp",
                        strategy=name,
                        subscription=push_state.get("subscription"),
                        symbol=push_state.get("symbol"),
                    )
                    push_state["_last_gap_warning_at"] = now_ts
                    self._record_data_push_event(
                        name,
                        "推送活跃但尚未收到时间戳",
                        level="warning",
                        reason="已启动推送，等待数据",
                    )
                    self._set_data_push_reason(name, "已启动推送，等待数据")

            status["data_push"] = payload
            if push_state.get("status_reason"):
                payload["status_reason"] = push_state.get("status_reason")
                status["data_push_reason"] = push_state.get("status_reason")
            if push_state.get("event_log"):
                status["data_push_logs"] = list(push_state.get("event_log", []))

        return status

    async def update_strategy(self, name: str, updates: Dict[str, Any]) -> bool:
        """Update a strategy"""
        try:
            if name not in self.strategies:
                return False

            strategy = self.strategies[name]
            runtime = self.internal_strategies_runtime.get(name)
            if runtime is None:
                runtime = self._load_internal_strategy_runtime(strategy)
            param_definitions = (
                runtime.get_parameter_definitions() if runtime else {}
            )
            # 将SL/TP更新与常规字段分离
            sl_tp_keys = {"sl_enabled", "sl_price", "sl_type", "tp_enabled", "tp_price", "tp_type"}
            position_setting_map = {
                "data_subscription": "data_subscription",
                "position_add_cooldown_minutes": "position_add_cooldown_minutes",
                "close_before_close_minutes": "close_before_close_minutes",
            }
            settings_to_save: Dict[str, Any] = {}
            settings_to_remove: Set[str] = set()
            position_settings_changed = False
            restart_data_push = False
            parameter_updates: Dict[str, Any] = {}
            for key, value in updates.items():
                if key in sl_tp_keys:
                    # 构造持久化键
                    if key.startswith('sl_'):
                        settings_key = f"{name}_{key}"
                        settings_to_save[settings_key] = value
                    elif key.startswith('tp_'):
                        settings_key = f"{name}_{key}"
                        settings_to_save[settings_key] = value
                elif key == 'primary_symbol':
                    storage_key = f"{name}_primary_symbol"
                    symbol_value = None
                    if isinstance(value, str):
                        symbol_value = value.strip().upper() or None
                    elif value:
                        symbol_value = str(value).strip().upper() or None
                    if symbol_value:
                        settings_to_save[storage_key] = symbol_value
                        setattr(strategy, 'primary_symbol', symbol_value)
                        updated_primary_symbol = symbol_value
                    else:
                        settings_to_remove.add(storage_key)
                        setattr(strategy, 'primary_symbol', None)
                        updated_primary_symbol = None
                    restart_data_push = True
                elif key == 'schedule':
                    setattr(strategy, key, value)
                    settings_to_save[f"{name}_schedule"] = value
                elif key in position_setting_map:
                    position_settings_changed = True
                    storage_key = f"{name}_{position_setting_map[key]}"
                    if key == 'data_subscription':
                        if value:
                            settings_to_save[storage_key] = value
                        else:
                            settings_to_remove.add(storage_key)
                        restart_data_push = True
                    else:
                        try:
                            minutes_value = int(value) if value is not None else None
                        except (TypeError, ValueError):
                            minutes_value = None
                        if minutes_value and minutes_value > 0:
                            settings_to_save[storage_key] = minutes_value
                        else:
                            settings_to_remove.add(storage_key)
                elif key == 'parameters' and isinstance(value, dict):
                    parameter_updates.update(value)
                elif runtime and key in param_definitions:
                    parameter_updates[key] = value
                elif hasattr(strategy, key):
                    setattr(strategy, key, value)

            # 先保存策略实体（常规字段）
            self._save_strategies()

            if parameter_updates and runtime:
                try:
                    applied = runtime.apply_parameter_updates(parameter_updates)
                except ValueError as exc:
                    strategy_logger.error(f"Parameter update failed for {name}: {exc}")
                    return False
                for param_name, param_value in applied.items():
                    storage_key = f"{name}_param_{param_name}"
                    if param_value is None:
                        settings_to_remove.add(storage_key)
                    else:
                        settings_to_save[storage_key] = param_value

            # 持久化SL/TP设置
            self._persist_strategy_settings(settings_to_save, settings_to_remove)

            if 'primary_symbol' in updates:
                runtime_instance = self.internal_strategies_runtime.get(name)
                if runtime_instance and hasattr(runtime_instance, 'symbol'):
                    try:
                        setattr(runtime_instance, 'symbol', updated_primary_symbol)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        strategy_logger.warning(
                            f"Failed to update runtime strategy symbol for {name}: {exc}"
                        )
                if runtime_instance and hasattr(runtime_instance, 'primary_symbol'):
                    try:
                        setattr(runtime_instance, 'primary_symbol', updated_primary_symbol)
                    except Exception:
                        pass
                if updated_primary_symbol and runtime_instance:
                    dom_service = getattr(runtime_instance, 'dom_service', None)
                    ensure = getattr(dom_service, 'ensure_stream_running', None)
                    if callable(ensure):
                        try:
                            ensure(updated_primary_symbol)
                        except Exception as exc:  # pragma: no cover - defensive guard
                            strategy_logger.warning(
                                f"Failed to auto-start DOM stream for {name}: {exc}"
                            )

            if settings_to_remove:
                strategy_logger.info(f"Removed strategy settings for {name}: {', '.join(sorted(settings_to_remove))}")

            if position_settings_changed:
                self._handle_position_setting_change(name)

            stored_subscription = self._get_strategy_setting(name, "data_subscription")
            stored_symbol = (
                self._get_strategy_setting(name, "primary_symbol")
                or getattr(strategy, "primary_symbol", None)
            )

            if restart_data_push:
                try:
                    push_state = self.data_push_state.get(name, {})
                    if push_state.get('enabled'):
                        await self.set_data_push(name, True, force_restart=True)
                    elif strategy.enabled and stored_subscription and stored_symbol:
                        self._request_data_push_activation(
                            name,
                            subscription=stored_subscription,
                            symbol=stored_symbol,
                            force_restart=True,
                        )
                except Exception as exc:  # pragma: no cover - defensive guard
                    strategy_logger.warning(f"Failed to restart data push for {name}: {exc}")

            strategy_logger.info(f"Updated strategy: {name}")
            if 'enabled' in updates:
                runtime = self.internal_strategies_runtime.get(name)
                if runtime:
                    if strategy.enabled:
                        runtime.enable()
                    else:
                        runtime.disable()
                if (
                    not strategy.enabled
                    and strategy.strategy_type == 'internal'
                ):
                    try:
                        result = await self.set_data_push(name, False)
                        if not result.get("success"):
                            error_msg = result.get("error") or "unknown error"
                            strategy_logger.warning(
                                f"Failed to disable data push while strategy {name} disabled: {error_msg}"
                            )
                    except Exception as exc:  # pragma: no cover - defensive guard
                        strategy_logger.warning(
                            f"Exception while disabling data push for {name}: {exc}"
                        )
            self._invalidate_strategies_cache()
            self.restart_scheduler()
            return True
        except Exception as e:
            strategy_logger.error(f"Failed to update strategy {name}: {e}")
            return False

    async def prepare_internal_order(self, strategy_name: str, symbol: str, action: str, quantity: float) -> Dict[str, Any]:
        """Validate and capture state before executing an internal strategy order."""

        record = self.strategies.get(strategy_name)
        if not record or record.strategy_type != "internal":
            return {}

        symbol_code = (symbol or "").upper()
        if not symbol_code:
            return {}

        cooldown_setting = self._get_strategy_setting(strategy_name, "position_add_cooldown_minutes")
        try:
            cooldown_minutes = int(cooldown_setting) if cooldown_setting is not None else 0
        except (TypeError, ValueError):
            cooldown_minutes = 0

        current_position = await self._fetch_symbol_position(symbol_code)
        direction_flag, new_position = self._analyze_order_effect(current_position, action, quantity)

        if cooldown_minutes > 0 and direction_flag:
            key = (strategy_name, symbol_code, direction_flag)
            last_time = self._last_position_add.get(key)
            if last_time and now_local() - last_time < timedelta(minutes=cooldown_minutes):
                raise ValueError(
                    f"Add-position cooldown active for {symbol_code}: remaining"
                    f" {round((timedelta(minutes=cooldown_minutes) - (now_local() - last_time)).total_seconds() / 60, 2)} min"
                )

        max_position: Optional[float] = None
        if self.risk_manager:
            limit_getter = getattr(self.risk_manager, "get_position_limit", None)
            if callable(limit_getter):
                try:
                    max_position = limit_getter(symbol_code)
                except Exception as exc:  # pragma: no cover - defensive guard
                    strategy_logger.warning(
                        f"Failed to obtain position limit for {symbol_code}: {exc}"
                    )
        if max_position is not None and max_position >= 0:
            try:
                numeric_new_position = float(new_position)
            except (TypeError, ValueError):
                numeric_new_position = None
            if (
                numeric_new_position is not None
                and abs(numeric_new_position) - max_position > 1e-9
            ):
                raise ValueError(
                    f"Position limit exceeded for {symbol_code}: {numeric_new_position} > {max_position}"
                )

        if self.strategy_risk_manager:
            try:
                allowed, reason = await self.strategy_risk_manager.evaluate_order(
                    strategy_name=strategy_name,
                    symbol=symbol_code,
                    action=action,
                    quantity=float(quantity or 0.0),
                    current_position=current_position,
                    new_position=new_position,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    "Strategy risk evaluation failed", strategy=strategy_name, symbol=symbol_code, error=str(exc)
                )
            else:
                if not allowed:
                    raise ValueError(reason or "Strategy risk control blocked the order")

        return {
            "symbol": symbol_code,
            "current_position": current_position,
            "new_position": new_position,
            "direction_flag": direction_flag,
        }

    async def finalize_internal_order(
        self,
        strategy_name: str,
        symbol: str,
        action: str,
        quantity: float,
        pre_trade: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update cooldown state and auto-close schedule after an internal order."""

        record = self.strategies.get(strategy_name)
        if not record or record.strategy_type != "internal":
            return

        if not pre_trade:
            return

        symbol_code = (pre_trade.get("symbol") or symbol or "").upper()
        if not symbol_code:
            return

        direction_flag = pre_trade.get("direction_flag")
        new_position = pre_trade.get("new_position")

        if direction_flag:
            key = (strategy_name, symbol_code, direction_flag)
            self._last_position_add[key] = now_local()

        try:
            numeric_position = float(new_position) if new_position is not None else None
        except (TypeError, ValueError):
            numeric_position = None

        await self._update_auto_close_schedule(strategy_name, symbol_code, numeric_position)

        if self.strategy_risk_manager and numeric_position is not None:
            try:
                await self.strategy_risk_manager.record_execution(
                    strategy_name=strategy_name,
                    symbol=symbol_code,
                    new_position=numeric_position,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_logger.warning(
                    "Failed to update strategy risk execution state",
                    strategy=strategy_name,
                    symbol=symbol_code,
                    error=str(exc),
                )

    async def _update_auto_close_schedule(self, strategy_name: str, symbol: str, position: Optional[float]) -> None:
        close_setting = self._get_strategy_setting(strategy_name, "close_before_close_minutes")
        try:
            close_minutes = int(close_setting) if close_setting is not None else 0
        except (TypeError, ValueError):
            close_minutes = 0

        key = (strategy_name, symbol)
        if position is None or abs(position) < 1e-6 or close_minutes <= 0:
            task = self._auto_close_tasks.pop(key, None)
            if task:
                task.cancel()
            return

        self._schedule_auto_close_task(strategy_name, symbol, close_minutes)

    async def _fetch_symbol_position(self, symbol: str) -> float:
        try:
            import core  # 延迟导入避免循环导入
            positions = await core.ibkr_client.get_positions(fast=True)
            for item in positions or []:
                if str(item.get('symbol', '')).upper() == symbol:
                    return float(item.get('position', 0.0) or 0.0)
        except Exception as exc:
            strategy_logger.warning(f"Failed to fetch position for {symbol}: {exc}")
        return 0.0

    def _analyze_order_effect(self, current_position: float, action: str, quantity: float) -> Tuple[Optional[str], float]:
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            qty = 0.0
        action_upper = (action or "SELL").upper()
        direction = 1.0 if action_upper == "BUY" else -1.0
        new_position = current_position + direction * qty
        direction_flag: Optional[str] = None
        if new_position > 0 and direction > 0:
            if current_position <= 0 or new_position > current_position:
                direction_flag = "long"
        elif new_position < 0 and direction < 0:
            if current_position >= 0 or new_position < current_position:
                direction_flag = "short"
        return direction_flag, new_position

    def _schedule_auto_close_task(self, strategy_name: str, symbol: str, minutes_before_close: int) -> None:
        key = (strategy_name, symbol)
        existing = self._auto_close_tasks.get(key)
        if existing and not existing.done():
            existing.cancel()

        async def worker():
            try:
                run_at = self._compute_next_close_time(minutes_before_close)
                now_eastern = datetime.now(ZoneInfo("America/New_York"))
                wait_seconds = max(0.0, (run_at - now_eastern).total_seconds())
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                await self._execute_auto_close(strategy_name, symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                strategy_logger.error(f"Auto close task error for {strategy_name}/{symbol}: {exc}")
            finally:
                current_task = asyncio.current_task()
                stored_task = self._auto_close_tasks.get(key)
                if stored_task is current_task:
                    self._auto_close_tasks.pop(key, None)

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(worker())
        except RuntimeError:
            task = asyncio.create_task(worker())

        self._auto_close_tasks[key] = task

    def _compute_next_close_time(self, minutes_before_close: int) -> datetime:
        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz)
        close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= close_time:
            close_time = self._next_trading_close(now)
        run_time = close_time - timedelta(minutes=minutes_before_close)
        if run_time < now:
            run_time = now
        return run_time

    def _next_trading_close(self, reference: datetime) -> datetime:
        tz = reference.tzinfo or ZoneInfo("America/New_York")
        next_day = reference + timedelta(days=1)
        while next_day.weekday() >= 5:  # Skip weekends
            next_day += timedelta(days=1)
        return next_day.replace(hour=16, minute=0, second=0, microsecond=0)

    async def _execute_auto_close(self, strategy_name: str, symbol: str) -> None:
        if not self.queue_manager:
            strategy_logger.warning("QueueManager not available; skip auto close order")
            return

        symbol_code = symbol.upper()
        try:
            import core
            positions = await core.ibkr_client.get_positions(fast=True)
        except Exception as exc:
            strategy_logger.error(f"Auto close position fetch failed for {symbol_code}: {exc}")
            return

        position_info = None
        for item in positions or []:
            if str(item.get('symbol', '')).upper() == symbol_code:
                position_info = item
                break

        if not position_info:
            return

        try:
            raw_qty = float(position_info.get('position') or 0.0)
        except (TypeError, ValueError):
            raw_qty = 0.0

        quantity = int(abs(raw_qty))
        if quantity <= 0:
            return

        action = 'SELL' if raw_qty > 0 else 'BUY'
        payload = {
            "strategy": strategy_name,
            "symbol": symbol_code,
            "action": action,
            "qty": quantity,
            "type": "MARKET",
        }

        self.queue_manager.add_task("strategy_order", payload)
        strategy_logger.info(
            f"Auto close triggered for strategy {strategy_name} on {symbol_code} with {action} {quantity}"
        )

    def rename_strategy(self, old_name: str, new_name: str, rename_file: bool = False) -> bool:
        """重命名策略，并迁移相关配置与历史记录
        - old_name: 原策略名
        - new_name: 新策略名（会进行slug化处理）
        - rename_file: 对于内部策略，是否同时重命名策略文件
        """
        try:
            if old_name not in self.strategies:
                return False

            # 规范化新名称
            normalized_new = slugify_strategy_name(new_name).strip()
            if not normalized_new:
                return False
            if normalized_new in self.strategies:
                # 冲突
                return False

            strategy = self.strategies[old_name]

            # 可选：重命名内部策略文件
            if rename_file and strategy.strategy_type == "internal":
                try:
                    # 计算相对与strategy_dir的旧/新路径（保留原相对路径的父目录与后缀）
                    rel_old = Path(strategy.file_path) if strategy.file_path else Path(f"{old_name}.py")
                    suffix = rel_old.suffix or ".py"
                    rel_new = rel_old.with_name(f"{normalized_new}{suffix}")
                    old_abs = self.strategy_dir / rel_old
                    new_abs = self.strategy_dir / rel_new
                    if old_abs.exists():
                        if new_abs.exists():
                            strategy_logger.warning(f"Target file already exists, skip file rename: {new_abs}")
                        else:
                            new_abs.parent.mkdir(parents=True, exist_ok=True)
                            old_abs.rename(new_abs)
                            strategy_logger.info(f"Strategy file renamed: {old_abs} -> {new_abs}")
                            strategy.file_path = rel_new.as_posix()
                    else:
                        strategy_logger.warning(f"Old strategy file not found, skip file rename: {old_abs}")
                except Exception as fe:
                    strategy_logger.warning(f"Rename file failed, continue without file rename: {fe}")

            # 迁移策略配置键前缀（{name}_sl_*, {name}_tp_*, {name}_schedule）
            try:
                settings = self.db_manager.load_strategy_config() or {}
            except Exception:
                settings = getattr(self, 'settings', {}) or {}

            updated_settings: Dict[str, Any] = dict(settings)
            prefix_old = f"{old_name}_"
            prefix_new = f"{normalized_new}_"
            for key in list(settings.keys()):
                if isinstance(key, str) and key.startswith(prefix_old):
                    suffix = key[len(prefix_old):]
                    value = settings[key]
                    # 删除旧键，写入新键
                    updated_settings.pop(key, None)
                    updated_settings[f"{prefix_new}{suffix}"] = value

            # 同步内存与持久化
            self.settings = updated_settings
            self.db_manager.save_strategy_config(updated_settings)

            # 更新内存结构与数据库中的策略实体
            del self.strategies[old_name]
            strategy.name = normalized_new
            self.strategies[normalized_new] = strategy

            self._rename_internal_state(old_name, normalized_new)

            # 保存所有策略到数据库（会写入新名称）
            self._save_strategies()
            # 删除旧策略行（保险）
            try:
                self.db_manager.delete_strategy(old_name)
            except Exception:
                pass

            # 迁移订单历史中的strategy_name
            try:
                self.db_manager.update_orders_strategy_name(old_name, normalized_new)
            except Exception as oe:
                strategy_logger.warning(f"Update orders.strategy_name failed: {oe}")

            strategy_logger.info(f"Renamed strategy: {old_name} -> {normalized_new}")
            self._invalidate_strategies_cache()
            self.restart_scheduler()
            return True
        except Exception as e:
            strategy_logger.error(f"Failed to rename strategy {old_name} -> {new_name}: {e}")
            return False

    def _create_strategy_template(self, strategy_name: str, file_path: Optional[str] = None):
        """Create a Python strategy template file"""
        template_content = f'''"""
Strategy: {strategy_name}
Created: {now_local().strftime('%Y-%m-%d %H:%M:%S')}
"""

from typing import Dict, Any, Optional

from src.utils import now_local


class {strategy_name.replace(' ', '').replace('-', '').replace('_', '')}Strategy:
    """
    {strategy_name} Strategy Implementation
    """

    def __init__(self):
        self.name = "{strategy_name}"
        self.description = "Strategy template for {strategy_name}"
        self.created_at = now_local()
        
    def initialize(self):
        """Initialize the strategy"""
        print(f"Initializing strategy: {{self.name}}")
        
    def on_data(self, symbol: str, data: Dict[str, Any]):
        """
        Handle market data updates
        
        Args:
            symbol: Stock symbol
            data: Market data dictionary
        """
        pass
        
    def generate_signal(self, symbol: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on market data
        Returns:
            A dict with signal details: { '{' }"action": "BUY"/"SELL", "quantity": int, "price": float, "order_type": "MARKET"/"LIMIT"{ '}' } or None if no signal
        """
        return None
        
    def on_order_filled(self, order_id: str, price: float, quantity: int):
        """Handle order filled events"""
        print(f"Order filled: id={{order_id}}, price={{price}}, qty={{quantity}}")
        
    def on_error(self, error: str):
        """Handle errors"""
        print(f"Strategy error: {{error}}")


# Strategy instance
strategy = {strategy_name.replace(' ', '').replace('-', '').replace('_', '')}Strategy()

# Entry point
if __name__ == "__main__":
    strategy.initialize()
    print(f"Strategy {{strategy.name}} is ready")
'''
        
        strategy_file = self.strategy_dir / (file_path if file_path else f"{strategy_name}.py")
        try:
            with open(strategy_file, 'w') as f:
                f.write(template_content)
            strategy_logger.info(f"Created strategy template: {strategy_file}")
        except Exception as e:
            strategy_logger.error(f"Failed to create strategy template: {e}")
            


    def get_strategies(self) -> List[Dict[str, Any]]:
        """Get all strategies"""
        strategy_names = tuple(sorted(self.strategies.keys()))
        now = time.monotonic()
        with self._strategies_cache_lock:
            cache = self._strategies_cache
            if (
                cache.get("data") is not None
                and cache.get("names") == strategy_names
                and now < cache.get("expires_at", 0.0)
            ):
                return deepcopy(cache["data"])

        if strategy_names:
            try:
                order_stats = self.db_manager.get_strategy_order_stats(
                    strategy_names, order_source='strategy'
                )
            except Exception as exc:
                strategy_logger.warning(
                    f"Failed to load strategy order stats: {exc}"
                )
                order_stats = {}
        else:
            order_stats = {}

        result: List[Dict[str, Any]] = []
        for strategy in self.strategies.values():
            strategy_dict = asdict(strategy)
            stats = order_stats.get(strategy.name, {})
            strategy_dict['trigger_count'] = int(stats.get('order_count', 0) or 0)
            last_created_at = stats.get('last_created_at')
            if last_created_at:
                parsed_time = parse_local_datetime(last_created_at)
                strategy_dict['last_triggered'] = parsed_time or last_created_at
            else:
                strategy_dict['last_triggered'] = None
            if strategy_dict['created_at'] and isinstance(strategy_dict['created_at'], datetime):
                strategy_dict['created_at'] = to_local_iso(strategy_dict['created_at'])
            if strategy_dict['last_triggered'] and isinstance(strategy_dict['last_triggered'], datetime):
                strategy_dict['last_triggered'] = to_local_iso(strategy_dict['last_triggered'])

            data_push_state = self.data_push_state.get(strategy.name, {}) or {}
            awaiting_data = bool(data_push_state.get('awaiting_data'))
            status_reason = data_push_state.get('status_reason')
            last_data_timestamp = data_push_state.get('last_data_timestamp')
            data_push_active = bool(data_push_state.get('active'))
            data_push_subscription = data_push_state.get('subscription')
            data_push_symbol = data_push_state.get('symbol')
            is_receiving_data = False

            if strategy.strategy_type == 'internal':
                try:
                    runtime_status = self.get_internal_strategy_status(strategy.name)
                except Exception as exc:  # pragma: no cover - defensive guard
                    strategy_logger.warning(
                        f"Failed to load runtime status for {strategy.name}: {exc}"
                    )
                    runtime_status = None
                if isinstance(runtime_status, dict):
                    is_receiving_data = bool(runtime_status.get('is_receiving_data'))
                    status_reason = runtime_status.get('data_push_reason') or status_reason
                    push_snapshot = runtime_status.get('data_push') or {}
                    if push_snapshot:
                        if 'awaiting_data' in push_snapshot:
                            awaiting_data = bool(push_snapshot.get('awaiting_data'))
                        if push_snapshot.get('last_data_timestamp'):
                            last_data_timestamp = push_snapshot.get('last_data_timestamp')
                        if 'active' in push_snapshot:
                            data_push_active = bool(push_snapshot.get('active'))
                        if push_snapshot.get('subscription'):
                            data_push_subscription = push_snapshot.get('subscription')
                        if push_snapshot.get('symbol'):
                            data_push_symbol = push_snapshot.get('symbol')
            elif status_reason is None and data_push_state:
                status_reason = data_push_state.get('status_reason')

            strategy_dict['is_receiving_data'] = is_receiving_data
            strategy_dict['data_push_reason'] = status_reason
            strategy_dict['data_reception'] = {
                'is_receiving': is_receiving_data,
                'status_reason': status_reason,
                'awaiting_data': awaiting_data,
                'active': data_push_active,
                'subscription': data_push_subscription,
                'symbol': data_push_symbol,
                'last_data_timestamp': last_data_timestamp,
            }

            result.append(strategy_dict)

        generated_at = time.monotonic()
        with self._strategies_cache_lock:
            self._strategies_cache = {
                "data": deepcopy(result),
                "expires_at": generated_at + self._strategies_cache_ttl,
                "names": strategy_names,
            }
        return result
        

            
    def delete_strategy(self, name: str) -> bool:
        """Delete a strategy"""
        try:
            if name not in self.strategies:
                return False

            # Delete from memory
            del self.strategies[name]
            self._cleanup_internal_state(name)

            # Delete from database directly
            self.db_manager.delete_strategy(name)

            # Also clean strategy orders from database
            self.db_manager.delete_strategy_orders(name)

            strategy_logger.info(f"Deleted strategy: {name}")
            self._invalidate_strategies_cache()
            return True
        except Exception as e:
            strategy_logger.error(f"Failed to delete strategy {name}: {e}")
            return False

    def reset_strategy_data(self, name: str) -> Dict[str, Any]:
        """清除指定策略的订单与统计缓存数据。"""
        if name not in self.strategies:
            raise ValueError(f"Strategy {name} not found")

        try:
            deleted = self.db_manager.delete_strategy_orders(name, None)
        except Exception as exc:
            strategy_logger.error(f"Failed to delete orders for strategy {name}: {exc}")
            raise

        strategy = self.strategies.get(name)
        if strategy:
            strategy.trigger_count = 0
            strategy.last_triggered = None

        strategy_logger.info(
            f"Strategy data reset: {name}, removed {deleted} persisted orders"
        )
        self._invalidate_strategies_cache()
        return {"deleted_orders": int(deleted or 0)}

            
            
            
            
    def get_executions(self, strategy_name: str, limit: int = 100):
        """Get strategy orders from database"""
        try:
            return self.db_manager.load_strategy_orders(strategy_name, 'strategy', limit)
        except Exception as e:
            strategy_logger.error(f"Failed to get executions for {strategy_name}: {e}")
            return []

    def get_strategy_performance(self, strategy_name: str, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> Dict[str, Any]:
        """Get strategy performance metrics based on orders"""
        try:
            orders = self.db_manager.load_strategy_orders(strategy_name, 'strategy', limit=1000)
            if start_date:
                orders = [o for o in orders if o.timestamp and parse_local_datetime(o.timestamp) >= start_date]
            if end_date:
                orders = [o for o in orders if o.timestamp and parse_local_datetime(o.timestamp) <= end_date]
            pnl_values = [o.pnl for o in orders if o.pnl is not None]
            commission_values: List[float] = []
            for order in orders:
                commission_raw = getattr(order, 'commission', None)
                if commission_raw is None:
                    continue
                try:
                    commission_values.append(float(commission_raw))
                except (TypeError, ValueError):
                    continue
            total_commission = sum(commission_values) if commission_values else 0
            total_executions = len(orders)
            if total_executions == 0:
                return {'total_executions': 0, 'total_pnl': 0, 'avg_pnl': 0,
                        'winning_trades': 0, 'losing_trades': 0, 'win_rate': 0,
                        'max_drawdown': 0, 'sharpe_ratio': 0, 'avg_trade_duration': 0,
                        'total_commission': 0}
            total_pnl = sum(pnl_values)
            avg_pnl = total_pnl / len(pnl_values) if pnl_values else 0
            winning_trades = len([p for p in pnl_values if p > 0])
            losing_trades = len([p for p in pnl_values if p < 0])
            win_rate = (winning_trades / len(pnl_values) * 100) if pnl_values else 0
            cumulative = []
            run_total = 0
            for p in pnl_values:
                run_total += p
                cumulative.append(run_total)
            max_drawdown = 0
            if cumulative:
                peak = cumulative[0]
                for value in cumulative:
                    if value > peak:
                        peak = value
                    drawdown = peak - value
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            sharpe_ratio = 0
            if pnl_values and len(pnl_values) > 1:
                import statistics
                std_dev = statistics.stdev(pnl_values)
                if std_dev != 0:
                    sharpe_ratio = (avg_pnl / std_dev) * (len(pnl_values) ** 0.5)
            durations: List[float] = []
            for order in orders:
                status = getattr(order, "status", "")
                if isinstance(status, str) and status.lower() != "filled":
                    continue

                created_raw = getattr(order, "timestamp", None)
                executed_raw = getattr(order, "executed_at", None)
                created_dt = parse_local_datetime(created_raw) if created_raw else None
                executed_dt = parse_local_datetime(executed_raw) if executed_raw else None

                # 对于缺失成交时间的订单，使用创建时间作为结束时间，确保不会产生负值
                if created_dt and not executed_dt:
                    executed_dt = created_dt

                if not created_dt or not executed_dt:
                    continue

                duration_seconds = (executed_dt - created_dt).total_seconds()
                if duration_seconds >= 0:
                    durations.append(duration_seconds)

            avg_trade_duration = sum(durations) / len(durations) if durations else 0

            return {'total_executions': total_executions,
                    'total_pnl': total_pnl,
                    'avg_pnl': avg_pnl,
                    'winning_trades': winning_trades,
                    'losing_trades': losing_trades,
                    'win_rate': win_rate,
                    'max_drawdown': max_drawdown,
                    'sharpe_ratio': sharpe_ratio,
                    'avg_trade_duration': avg_trade_duration,
                    'total_commission': total_commission}
        except Exception as e:
            strategy_logger.error(f"Failed to get strategy performance for {strategy_name}: {e}")
            return {}
    def generate_auth_token(self) -> str:
        """Generate a new authentication token"""
        timestamp = str(int(time.time()))
        secret = os.urandom(32).hex()
        token_data = f"{timestamp}:{secret}"
        token = hashlib.sha256(token_data.encode()).hexdigest()
        
        # Update settings with new token
        self.settings['auth_token'] = token
        self.auth_token = token
        self._save_settings()
        
        strategy_logger.info("Generated new auth token")
        return token
        
    def validate_auth_token(self, token: str) -> bool:
        """Validate an authentication token"""
        if not self.auth_token:
            return False
        return hmac.compare_digest(self.auth_token, token)
