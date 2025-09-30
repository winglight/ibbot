import asyncio
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from loguru import logger
import json
import anyio
from anyio import CancelScope
from anyio.abc import TaskGroup as AnyIOTaskGroup

# 为队列管理器绑定模块日志记录器
queue_logger = logger.bind(module="queue_manager", request_id="-")
from pathlib import Path

from .database import Order
from .ibkr_client import INDEX_FUTURE_SYMBOLS
from .utils import ensure_local_timezone, now_local, parse_local_datetime, to_local_iso

@dataclass
class QueueTask:
    """队列任务数据结构"""
    task_id: str
    task_type: str
    payload: Dict[str, Any]
    status: str  # pending, processing, completed, failed
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3

    def __post_init__(self):
        self.created_at = ensure_local_timezone(self.created_at) or now_local()
        self.started_at = ensure_local_timezone(self.started_at)
        self.completed_at = ensure_local_timezone(self.completed_at)

class QueueManager:
    """简单的FIFO队列管理器"""
    
    def __init__(self, max_concurrent_tasks: int = 5):
        self.tasks: Dict[str, QueueTask] = {}
        self.pending_queue: List[str] = []  # 存储task_id的FIFO队列
        self.processing_tasks: Dict[str, Any] = {}
        self.max_concurrent_tasks = max_concurrent_tasks
        self.is_running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._task_group = None
        self._capacity_limiter = None
        self._cancel_scope: Optional[CancelScope] = None
        self._worker_ready: Optional[asyncio.Event] = None
        
        # 依赖注入的对象
        self.risk_manager = None
        self.strategy_manager = None
        self.websocket_manager = None
        self.order_manager = None
        self.notification_manager = None

        # 确保数据目录存在
        self.data_dir = Path("data")
        self.data_dir.mkdir(exist_ok=True)
        self.queue_file = self.data_dir / "queue_tasks.json"

        # 运行时监控指标
        self._last_queue_heartbeat: float = 0.0
        self._last_backlog_alert: float = 0.0
        self._queue_heartbeat_interval: float = 60.0
        self._backlog_alert_interval: float = 300.0
        self._backlog_pending_threshold: int = 20
        self._backlog_age_threshold: float = 120.0

        # 记录最近一次延迟日志，避免IB未连接时疯狂刷屏
        self._last_deferral_notice: Dict[str, float] = {}
        self._ib_disconnect_backoff: float = 0.5

        # 运行时性能监控配置
        self._worker_idle_sleep: float = 0.1
        self._worker_loop_warning_threshold: float = 1.0
        self._task_processing_warning_threshold: float = 2.0
        self._io_warning_threshold: float = 0.5
        self._offthread_warning_threshold: float = 1.5

        # 启动时加载持久化的任务（需要在性能阈值配置完成后调用）
        self._load_tasks()

    def set_dependencies(self, risk_manager, strategy_manager, websocket_manager, order_manager=None, notification_manager=None):
        """设置依赖对象"""
        self.risk_manager = risk_manager
        self.strategy_manager = strategy_manager
        self.websocket_manager = websocket_manager
        self.order_manager = order_manager
        if notification_manager:
            self.notification_manager = notification_manager
        queue_logger.info("Dependencies set for QueueManager")

    def _should_log_deferral(self, task_id: str, interval: float = 2.0) -> bool:
        """节流IB未连接时的延迟日志"""

        now = time.time()
        last = self._last_deferral_notice.get(task_id)
        if last is None or (now - last) >= interval:
            self._last_deferral_notice[task_id] = now
            return True
        return False

    def _log_operation_duration(
        self,
        *,
        event: str,
        start: float,
        threshold: float,
        level: str = "warning",
        message: str = "Slow queue operation detected",
        **context: Any,
    ) -> float:
        """记录耗时操作，便于排查阻塞问题。"""

        elapsed = time.monotonic() - start
        if elapsed >= threshold:
            bound_logger = queue_logger.bind(event=event, duration=f"{elapsed:.3f}s", **context)
            log_fn = getattr(bound_logger, level, bound_logger.warning)
            log_fn(message)
        return elapsed

    def _ib_connected(self) -> bool:
        """安全地检测IB连接状态，容忍client缺失或异常"""

        try:
            import core  # 延迟导入避免循环依赖
        except ImportError:
            return False

        client = getattr(core, "ibkr_client", None)
        if client is None:
            return False

        try:
            return bool(client.is_connected())
        except Exception as exc:
            if self._should_log_deferral("ib-connection-check", interval=10.0):
                queue_logger.debug(f"IB connection check failed: {exc}")
            return False
    
    def add_task(self, task_type: str, payload: Dict[str, Any], max_retries: int = 3) -> str:
        """添加任务到队列"""
        task_id = str(uuid.uuid4())
        task = QueueTask(
            task_id=task_id,
            task_type=task_type,
            payload=payload,
            status="pending",
            created_at=now_local(),
            max_retries=max_retries
        )
        
        self.tasks[task_id] = task
        self.pending_queue.append(task_id)
        
        # 持久化任务
        self._save_tasks()
        
        queue_logger.bind(task_type=task_type, payload=payload).info(
            f"Task {task_id} added to queue, type: {task_type}"
        )
        return task_id
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        task = self.tasks.get(task_id)
        if not task:
            return None
        
        return {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "status": task.status,
            "created_at": to_local_iso(task.created_at),
            "started_at": to_local_iso(task.started_at),
            "completed_at": to_local_iso(task.completed_at),
            "error_message": task.error_message,
            "retry_count": task.retry_count
        }
    
    def get_queue_status(self) -> Dict[str, Any]:
        """获取队列整体状态"""
        pending_count = len(self.pending_queue)
        processing_count = len(self.processing_tasks)
        completed_count = len([t for t in self.tasks.values() if t.status == "completed"])
        failed_count = len([t for t in self.tasks.values() if t.status == "failed"])

        return {
            "is_running": self.is_running,
            "pending_count": pending_count,
            "processing_count": processing_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "total_tasks": len(self.tasks),
            "max_concurrent_tasks": self.max_concurrent_tasks
        }

    def get_oldest_pending_task_info(self) -> Optional[Dict[str, Any]]:
        """返回最久等待的任务信息，包括等待时间与重试次数"""
        oldest_task: Optional[QueueTask] = None
        oldest_created_at: Optional[datetime] = None
        fallback_now = now_local()
        for task_id in self.pending_queue:
            task = self.tasks.get(task_id)
            if not task:
                continue
            task_created = ensure_local_timezone(task.created_at) or fallback_now
            if oldest_task is None or (oldest_created_at is None or task_created < oldest_created_at):
                oldest_task = task
                oldest_created_at = task_created

        if not oldest_task or oldest_created_at is None:
            return None

        current_time = now_local()
        age_seconds = max((current_time - oldest_created_at).total_seconds(), 0)
        return {
            "task_id": oldest_task.task_id,
            "age_seconds": age_seconds,
            "retry_count": oldest_task.retry_count,
            "task_type": oldest_task.task_type,
        }
    
    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """获取所有任务列表"""
        tasks_list = []
        for task in self.tasks.values():
            tasks_list.append({
                "task_id": task.task_id,
                "task_type": task.task_type,
                "status": task.status,
                "created_at": to_local_iso(task.created_at),
                "started_at": to_local_iso(task.started_at),
                "completed_at": to_local_iso(task.completed_at),
                "error_message": task.error_message,
                "retry_count": task.retry_count,
                "payload": task.payload
            })
        
        # 按创建时间倒序排列
        tasks_list.sort(key=lambda x: x["created_at"], reverse=True)
        return tasks_list
    
    async def start_worker(self):
        """启动队列工作器"""
        if self.is_running:
            return

        try:
            limiter = anyio.CapacityLimiter(self.max_concurrent_tasks)
            task_group = await anyio.create_task_group().__aenter__()
        except Exception as exc:
            queue_logger.error(f"Failed to initialize queue task group: {exc}")
            raise

        self._capacity_limiter = limiter
        self._task_group = task_group

        self.is_running = True
        recovered_tasks: List[str] = []
        for task_id, task in self.tasks.items():
            if task.status == "processing":
                task.status = "pending"
                task.started_at = None
                if task_id not in self.pending_queue:
                    recovered_tasks.append(task_id)
        if recovered_tasks:
            self.pending_queue.extend(recovered_tasks)
            queue_logger.info(
                f"Recovered {len(recovered_tasks)} in-flight tasks back to pending queue"
            )
            self._save_tasks()

        ready_event = asyncio.Event()
        self._worker_ready = ready_event
        worker_task = asyncio.create_task(self._worker_main(), name="queue-worker")
        self._worker_task = worker_task

        await ready_event.wait()

        if worker_task.done():
            exc = worker_task.exception()
            if exc:
                raise exc
        queue_logger.info("Queue worker started with AnyIO task group")
        self._last_queue_heartbeat = 0.0
        self._last_backlog_alert = 0.0
        try:
            await self._notify_queue_state("started")
        except Exception as exc:
            queue_logger.warning(f"Queue worker start notification failed: {exc}")

    async def stop_worker(self):
        """停止队列工作器"""
        self.is_running = False
        cancel_scope = self._cancel_scope
        if cancel_scope:
            try:
                cancel_scope.cancel()
            except Exception:
                pass

        # 等待工作器任务完成
        worker_task = self._worker_task
        if worker_task:
            if not worker_task.done():
                worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        self._worker_task = None
        self._task_group = None
        self._capacity_limiter = None
        self._cancel_scope = None
        self._worker_ready = None

        self.processing_tasks.clear()

        queue_logger.info("Queue worker stopped")
        try:
            await self._notify_queue_state("stopped")
        except Exception as exc:
            queue_logger.warning(f"Queue worker stop notification failed: {exc}")

    async def _worker_main(self) -> None:
        limiter = anyio.CapacityLimiter(self.max_concurrent_tasks)
        ready_event = self._worker_ready
        try:
            async with anyio.create_task_group() as task_group:
                try:
                    with CancelScope() as cancel_scope:
                        self._capacity_limiter = limiter
                        self._task_group = task_group
                        self._cancel_scope = cancel_scope
                        if ready_event and not ready_event.is_set():
                            ready_event.set()
                        await self._queue_consumer(task_group, limiter, cancel_scope)
                finally:
                    # Ensure the ready event is set even if CancelScope exits early
                    if ready_event and not ready_event.is_set():
                        ready_event.set()
        except asyncio.CancelledError:
            queue_logger.info("Queue worker task cancelled")
            raise
        except Exception as exc:
            queue_logger.error(f"Queue worker terminated due to error: {exc}")
            raise
        finally:
            if ready_event and not ready_event.is_set():
                ready_event.set()
            self._task_group = None
            self._capacity_limiter = None
            self._cancel_scope = None
            self._worker_ready = None
            self._worker_task = None
            self.is_running = False

    async def _queue_consumer(
        self,
        task_group: AnyIOTaskGroup,
        limiter: anyio.CapacityLimiter,
        cancel_scope: CancelScope,
    ):
        """队列工作器主循环，基于 AnyIO 任务组"""
        try:
            while self.is_running:
                iteration_start = time.monotonic()
                try:
                    if cancel_scope.cancel_called:
                        break

                    if self.pending_queue:
                        task_id = self.pending_queue.pop(0)
                        task = self.tasks.get(task_id)

                        if task and task.status == "pending":
                            if task.task_type in ("strategy_order",) and not self._ib_connected():
                                self.pending_queue.append(task_id)
                                if self._should_log_deferral(task_id):
                                    queue_logger.debug(
                                        f"Deferring task {task_id} due to IBKR not connected"
                                    )
                                await asyncio.sleep(self._ib_disconnect_backoff)
                            else:
                                await limiter.acquire()
                                try:
                                    task_group.start_soon(self._run_task_wrapper, task, limiter)
                                    self.processing_tasks[task_id] = True
                                except Exception:
                                    limiter.release()
                                    raise
                        else:
                            queue_logger.debug(
                                "Discarding unavailable task", task_id=task_id
                            )
                    else:
                        self._last_deferral_notice.clear()

                except asyncio.CancelledError:
                    queue_logger.info("Queue worker loop cancellation requested")
                    raise
                except Exception as exc:
                    queue_logger.error(f"Worker loop error: {exc}")
                    await asyncio.sleep(1)

                try:
                    await self._monitor_queue_health()
                except Exception as exc:
                    queue_logger.warning(f"Queue health monitor failed: {exc}")

                loop_elapsed = self._log_operation_duration(
                    event="worker_loop_iteration",
                    start=iteration_start,
                    threshold=self._worker_loop_warning_threshold,
                    message="Queue worker iteration slower than expected",
                    pending=len(self.pending_queue),
                    processing=len(self.processing_tasks),
                )
                queue_logger.bind(duration=f"{loop_elapsed:.3f}s").debug("Queue worker iteration completed")

                await asyncio.sleep(self._worker_idle_sleep)
        except asyncio.CancelledError:
            queue_logger.info("Queue worker loop cancelled")
            raise

    async def _run_task_wrapper(
        self,
        task: QueueTask,
        limiter: Optional[anyio.CapacityLimiter] = None,
    ):
        """包装任务处理，确保调度器任务正确清理"""
        try:
            await self._process_task(task)
        except asyncio.CancelledError:
            queue_logger.info(f"Task {task.task_id} cancelled; returning to pending queue")
            if task.status == "processing":
                task.status = "pending"
                task.started_at = None
                self.pending_queue.insert(0, task.task_id)
                self._save_tasks()
            raise
        finally:
            self.processing_tasks.pop(task.task_id, None)
            self._last_deferral_notice.pop(task.task_id, None)
            if limiter is not None:
                limiter.release()

    async def _run_task_wrapper(
        self,
        task: QueueTask,
        limiter: Optional[anyio.CapacityLimiter] = None,
    ):
        """包装任务处理，确保调度器任务正确清理"""
        try:
            await self._process_task(task)
        except asyncio.CancelledError:
            queue_logger.info(f"Task {task.task_id} cancelled; returning to pending queue")
            if task.status == "processing":
                task.status = "pending"
                task.started_at = None
                self.pending_queue.insert(0, task.task_id)
                self._save_tasks()
            raise
        finally:
            self.processing_tasks.pop(task.task_id, None)
            self._last_deferral_notice.pop(task.task_id, None)
            if limiter is not None:
                limiter.release()

    async def _process_task(self, task: QueueTask):
        """处理单个任务"""
        task_start = time.monotonic()
        self._last_deferral_notice.pop(task.task_id, None)
        task.status = "processing"
        task.started_at = now_local()
        self._save_tasks()

        queue_logger.bind(payload=task.payload).info(
            f"Processing task {task.task_id}, type: {task.task_type}"
        )

        try:
            # 根据任务类型调用相应的处理器
            if task.task_type == "strategy_order":
                await self._process_strategy_order(task)
            else:
                raise ValueError(f"Unknown task type: {task.task_type}")

            task.status = "completed"
            task.completed_at = now_local()
            queue_logger.info(f"Task {task.task_id} completed successfully")

        except Exception as e:
            error_msg = str(e)
            task.error_message = error_msg

            # 针对未连接IBKR的情况进行延后处理而非计为失败/重试
            if ("Not connected to IBKR" in error_msg) or (not self._ib_connected()):
                task.status = "pending"
                task.started_at = None
                # 重新排队等待连接恢复
                self.pending_queue.append(task.task_id)
                queue_logger.warning(
                    f"Task {task.task_id} deferred: {error_msg}; will retry when connected"
                )
                # 小幅休眠避免紧密轮询
                await asyncio.sleep(self._ib_disconnect_backoff)
            elif "Strategy disabled" in error_msg or "Strategy not found" in error_msg:
                # 对于禁用或不存在的策略，不进行重试，直接标记为失败
                task.status = "failed"
                task.completed_at = now_local()
                queue_logger.error(
                    f"Task {task.task_id} failed permanently: {error_msg}"
                )
            else:
                task.retry_count += 1
                queue_logger.error(
                    f"Task {task.task_id} failed: {error_msg}, retry {task.retry_count}/{task.max_retries}"
                )

                if task.retry_count < task.max_retries:
                    # 重新加入队列
                    task.status = "pending"
                    task.started_at = None
                    self.pending_queue.append(task.task_id)
                    queue_logger.info(f"Task {task.task_id} requeued for retry")
                else:
                    task.status = "failed"
                    task.completed_at = now_local()
                    queue_logger.error(
                        f"Task {task.task_id} failed permanently after {task.max_retries} retries"
                    )

        finally:
            total_elapsed = self._log_operation_duration(
                event="process_task",
                start=task_start,
                threshold=self._task_processing_warning_threshold,
                message="Queue task processing took longer than expected",
                task_id=task.task_id,
                task_type=task.task_type,
                status=task.status,
                retries=task.retry_count,
            )
            queue_logger.bind(
                task_id=task.task_id,
                task_type=task.task_type,
                status=task.status,
                retries=task.retry_count,
                duration=f"{total_elapsed:.3f}s",
            ).debug("Queue task processing finished")
            self._save_tasks()

    def _resolve_notification_manager(self):
        if self.notification_manager:
            return self.notification_manager
        try:
            import core as core_module  # type: ignore

            return getattr(core_module, "notification_manager", None)
        except Exception:
            return None

    async def _notify_queue_state(self, state: str) -> None:
        manager = self._resolve_notification_manager()
        if not manager:
            return

        status = self.get_queue_status()
        message = (
            "队列工作器已启动" if state == "started" else "队列工作器已停止"
        )
        detail_line = (
            f"挂起: {status['pending_count']} | 处理中: {status['processing_count']} | 失败: {status['failed_count']}"
        )

        await manager.send_notification(
            message=f"{message}\n{detail_line}",
            event_type=f"queue_{state}",
            queue_state=state,
            pending=status["pending_count"],
            processing=status["processing_count"],
            failed=status["failed_count"],
        )

    async def _send_queue_backlog_alert(
        self,
        status: Dict[str, Any],
        oldest: Dict[str, Any],
        max_retry: int,
    ) -> None:
        manager = self._resolve_notification_manager()
        if not manager:
            return

        message = (
            "⚠️ 队列挂起告警 ⚠️\n\n"
            f"挂起任务: {status['pending_count']}\n"
            f"最久等待: {int(oldest['age_seconds'])} 秒 (任务 {oldest['task_id']})\n"
            f"最大重试次数: {max_retry}"
        )

        await manager.send_notification(
            message=message,
            event_type="queue_warning",
            pending=status["pending_count"],
            processing=status["processing_count"],
            oldest_task=oldest.get("task_id"),
            oldest_wait_seconds=oldest.get("age_seconds"),
            max_retry=max_retry,
        )

    async def _monitor_queue_health(self) -> None:
        monitor_start = time.monotonic()
        now = time.time()
        status = self.get_queue_status()
        oldest = self.get_oldest_pending_task_info()
        pending_retry_counts = [
            task.retry_count for task in self.tasks.values() if task.status == "pending"
        ]
        max_retry = max(pending_retry_counts, default=0)

        if now - self._last_queue_heartbeat >= self._queue_heartbeat_interval:
            heartbeat_logger = queue_logger.bind(event="queue_heartbeat")
            heartbeat_logger.info(
                "Queue heartbeat",
                pending=status["pending_count"],
                processing=status["processing_count"],
                failed=status["failed_count"],
                oldest_task=oldest["task_id"] if oldest else None,
                oldest_wait_seconds=int(oldest["age_seconds"]) if oldest else None,
                max_retry=max_retry,
            )
            self._last_queue_heartbeat = now

        backlog_condition = (
            status["pending_count"] >= self._backlog_pending_threshold
            and oldest is not None
            and oldest["age_seconds"] >= self._backlog_age_threshold
        )

        if backlog_condition:
            if now - self._last_backlog_alert >= self._backlog_alert_interval:
                queue_logger.warning(
                    "Queue backlog detected",
                    pending=status["pending_count"],
                    processing=status["processing_count"],
                    oldest_task=oldest["task_id"],
                    oldest_wait_seconds=int(oldest["age_seconds"]),
                    max_retry=max_retry,
                )
                try:
                    await self._send_queue_backlog_alert(status, oldest, max_retry)
                except Exception as exc:
                    queue_logger.warning(f"Queue backlog alert notification failed: {exc}")
                self._last_backlog_alert = now
        elif status["pending_count"] == 0:
            self._last_backlog_alert = 0.0

        self._log_operation_duration(
            event="monitor_queue_health",
            start=monitor_start,
            threshold=self._io_warning_threshold,
            message="Queue health monitor took longer than expected",
            pending=status["pending_count"],
            processing=status["processing_count"],
        )

    async def _process_strategy_order(self, task: QueueTask):
        """处理策略订单任务"""
        strategy_start = time.monotonic()
        from datetime import datetime

        # 检查依赖对象是否已设置
        if not all([self.risk_manager, self.strategy_manager, self.websocket_manager]):
            raise Exception("Dependencies not properly set")
        
        payload = task.payload
        queue_logger.bind(task_id=task.task_id, payload=payload).debug(
            "Start processing strategy order payload"
        )
        
        # 检查连接状态（无需 ibkr_client）
        import core
        if not self._ib_connected():
            raise Exception("Not connected to IBKR")
        
        # 验证策略
        strategy = self.strategy_manager.get_strategy(payload['strategy'])
        if not strategy:
            raise Exception("Strategy not found")
        if not strategy.get('enabled', False):
            raise Exception("Strategy disabled")

        # 统一使用策略真实名称用于记录和持久化
        strategy_name_for_record = (
            payload.get('strategy_name')
            or strategy.get('name')
            or payload['strategy']
        )
        
        # 处理订单参数
        order_type = payload['type'].upper()
        action = payload['action'].upper()
        
        # 自动检测期货
        sec_type = payload.get('sec_type', 'STK')
        symbol = payload['symbol'].upper()
        if symbol in INDEX_FUTURE_SYMBOLS:
            sec_type = 'FUT'
            exchange = 'CME'
        else:
            exchange = payload.get('exchange', 'SMART')
        
        pre_trade_state: Optional[Dict[str, Any]] = None
        if strategy.get('strategy_type') == 'internal' and self.strategy_manager:
            try:
                pre_trade_start = time.monotonic()
                pre_trade_state = await self.strategy_manager.prepare_internal_order(
                    strategy_name=strategy['name'],
                    symbol=payload['symbol'],
                    action=action,
                    quantity=payload['qty'],
                )
                self._log_operation_duration(
                    event="prepare_internal_order",
                    start=pre_trade_start,
                    threshold=self._io_warning_threshold,
                    message="Internal strategy pre-trade check slower than expected",
                    strategy=strategy['name'],
                    symbol=payload['symbol'],
                    action=action,
                )
                queue_logger.bind(
                    task_id=task.task_id,
                    strategy=strategy['name'],
                    pre_trade_state=pre_trade_state,
                ).debug("Internal strategy pre-trade state prepared")
            except ValueError as exc:
                raise Exception(str(exc))
            except Exception as exc:
                queue_logger.error(f"Internal order pre-check failed: {exc}")
                raise

        # 下单
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 如果不在事件循环中，创建一个
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        order_id = None
        order_type_for_record = "MKT" if order_type == "MARKET" else "LMT"
        
        # 真实IBKR路径：使用 ibkr_client
        import core  # 延迟导入避免循环导入
        queue_logger.bind(
            task_id=task.task_id,
            strategy=strategy['name'],
            order_type=order_type,
            action=action,
            symbol=payload['symbol'],
            quantity=payload['qty'],
            exchange=exchange,
            sec_type=sec_type,
        ).info("Placing strategy order via IBKR")

        if order_type not in ("MARKET", "LIMIT"):
            raise Exception("Invalid order type")

        if order_type == "LIMIT" and not payload.get('price'):
            raise Exception("Price required for limit order")

        trade = await self._place_order_off_thread(
            symbol=payload['symbol'],
            quantity=payload['qty'],
            action=action,
            order_type=order_type,
            exchange=exchange,
            sec_type=sec_type,
            price=payload.get('price'),
        )

        if not trade:
            raise Exception("Failed to place strategy order")
        order_id = str(trade.order.orderId)
        
        if not order_id:
            raise Exception("Failed to obtain order id")

        if strategy.get('strategy_type') == 'internal' and self.strategy_manager:
            try:
                finalize_start = time.monotonic()
                await self.strategy_manager.finalize_internal_order(
                    strategy_name=strategy['name'],
                    symbol=payload['symbol'],
                    action=action,
                    quantity=payload['qty'],
                    pre_trade=pre_trade_state,
                )
                self._log_operation_duration(
                    event="finalize_internal_order",
                    start=finalize_start,
                    threshold=self._io_warning_threshold,
                    message="Internal strategy finalize step slower than expected",
                    strategy=strategy['name'],
                    symbol=payload['symbol'],
                    action=action,
                )
            except Exception as exc:
                queue_logger.warning(f"Finalize internal order state failed: {exc}")

        # 记录订单（优先使用 OrderManager）
        if getattr(self, 'order_manager', None):
            try:
                self.order_manager.create_order(
                    order_id=order_id,
                    symbol=payload['symbol'],
                    action=action,
                    quantity=payload['qty'],
                    order_type=order_type_for_record,
                    price=payload.get('price', 0.0),
                    order_source="strategy",
                    strategy_name=strategy_name_for_record,
                    rule_id=strategy_name_for_record,
                    exchange=exchange,
                    sec_type=sec_type
                )
            except Exception as e:
                queue_logger.warning(f"Failed to create order via OrderManager: {e}, falling back to RiskManager")
                if self.risk_manager:
                    created_at = now_local()
                    auto_order = Order(
                        order_id=order_id,
                        symbol=payload['symbol'],
                        action=action,
                        quantity=payload['qty'],
                        order_type=order_type_for_record,
                        price=payload.get('price', 0.0),
                        timestamp=to_local_iso(created_at) or created_at.isoformat(),
                        status="Submitted",
                        executed_at=None,
                        rule_id=strategy_name_for_record,
                        order_source="strategy",
                        strategy_name=strategy_name_for_record,
                    )
                    auto_order.created_at = created_at
                    auto_order.executed_at = None
                    self.risk_manager.auto_orders[order_id] = auto_order
                    self.risk_manager.save_auto_order_to_db(auto_order)
        elif self.risk_manager:
            created_at = now_local()
            auto_order = Order(
                order_id=order_id,
                symbol=payload['symbol'],
                action=action,
                quantity=payload['qty'],
                order_type=order_type_for_record,
                price=payload.get('price', 0.0),
                timestamp=to_local_iso(created_at) or created_at.isoformat(),
                status="Submitted",
                executed_at=None,
                rule_id=strategy_name_for_record,
                order_source="strategy",
                strategy_name=strategy_name_for_record,
            )
            auto_order.created_at = created_at
            auto_order.executed_at = None
            self.risk_manager.auto_orders[order_id] = auto_order
            self.risk_manager.save_auto_order_to_db(auto_order)
        
        # 注意：不在此处记录策略执行，而是等订单实际成交时通过IBKR客户端的状态更新机制
        # 使用实际成交数量来记录执行，避免下单数量与成交数量不一致导致的持仓计算错误
        # 策略执行记录将在ibkr_client的订单状态更新中创建
        
        # 广播消息
        if self.websocket_manager:
            await self.websocket_manager.broadcast({
                "type": "order_placed",
                "order_id": order_id,
                "symbol": payload['symbol'],
                "action": action,
                "quantity": payload['qty']
            })
        else:
            queue_logger.debug("websocket_manager 未注入，跳过广播")
        
        queue_logger.bind(
            task_id=task.task_id,
            strategy=strategy['name'],
            symbol=payload['symbol'],
            action=action,
            quantity=payload['qty'],
        ).info(f"Strategy order placed successfully: {order_id}")
        self._log_operation_duration(
            event="process_strategy_order",
            start=strategy_start,
            threshold=self._task_processing_warning_threshold,
            message="Strategy order processing slower than expected",
            task_id=task.task_id,
            strategy=strategy['name'],
            symbol=payload['symbol'],
            action=action,
        )

    async def _place_order_off_thread(
        self,
        *,
        symbol: str,
        quantity: int,
        action: str,
        order_type: str,
        exchange: str,
        sec_type: str,
        price: Optional[float],
    ):
        """在后台线程中执行下单流程，避免阻塞事件循环。"""

        dispatch_start = time.monotonic()
        queue_logger.bind(
            symbol=symbol,
            action=action,
            order_type=order_type,
            exchange=exchange,
            sec_type=sec_type,
            quantity=quantity,
        ).debug("Dispatching order placement to background thread")
        try:
            result = await asyncio.to_thread(
                self._place_order_blocking,
                symbol,
                quantity,
                action,
                order_type,
                exchange,
                sec_type,
                price,
            )
            self._log_operation_duration(
                event="place_order_off_thread",
                start=dispatch_start,
                threshold=self._offthread_warning_threshold,
                message="Background order placement slower than expected",
                symbol=symbol,
                action=action,
                order_type=order_type,
                exchange=exchange,
                sec_type=sec_type,
                quantity=quantity,
            )
            return result
        except Exception as exc:
            queue_logger.error(
                "Failed to place order off thread",
                symbol=symbol,
                action=action,
                order_type=order_type,
                exchange=exchange,
                sec_type=sec_type,
                quantity=quantity,
                error=str(exc),
            )
            raise

    def _place_order_blocking(
        self,
        symbol: str,
        quantity: int,
        action: str,
        order_type: str,
        exchange: str,
        sec_type: str,
        price: Optional[float],
    ):
        """在后台线程中调用 IBKR 客户端的下单接口。"""

        import core  # 延迟导入避免循环导入

        if order_type == "MARKET":
            return asyncio.run(
                core.ibkr_client.place_market_order(
                    symbol=symbol,
                    quantity=quantity,
                    action=action,
                    exchange=exchange,
                    sec_type=sec_type,
                )
            )
        elif order_type == "LIMIT":
            return core.ibkr_client.place_limit_order(
                symbol=symbol,
                quantity=quantity,
                price=price,
                action=action,
                exchange=exchange,
                sec_type=sec_type,
            )

        raise ValueError(f"Unsupported order type: {order_type}")

    def _save_tasks(self):
        """持久化任务到文件"""
        save_start = time.monotonic()
        try:
            tasks_data = {}
            for task_id, task in self.tasks.items():
                task_dict = asdict(task)
                # 转换datetime为字符串
                task_dict['created_at'] = to_local_iso(task.created_at)
                if task.started_at:
                    task_dict['started_at'] = to_local_iso(task.started_at)
                if task.completed_at:
                    task_dict['completed_at'] = to_local_iso(task.completed_at)
                tasks_data[task_id] = task_dict
            
            data = {
                'tasks': tasks_data,
                'pending_queue': self.pending_queue
            }
            
            with open(self.queue_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        except Exception as e:
            queue_logger.error(f"Failed to save tasks: {e}")
        else:
            self._log_operation_duration(
                event="save_tasks",
                start=save_start,
                threshold=self._io_warning_threshold,
                message="Persisting queue tasks took longer than expected",
                file=str(self.queue_file),
                pending=len(self.pending_queue),
                total=len(self.tasks),
            )

    def _load_tasks(self):
        """从文件加载任务"""
        load_start = time.monotonic()
        try:
            if not self.queue_file.exists():
                return

            with open(self.queue_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            tasks_data = data.get('tasks', {})
            for task_id, task_dict in tasks_data.items():
                # 转换字符串为datetime
                task_dict['created_at'] = parse_local_datetime(task_dict['created_at'])
                if task_dict.get('started_at'):
                    task_dict['started_at'] = parse_local_datetime(task_dict['started_at'])
                if task_dict.get('completed_at'):
                    task_dict['completed_at'] = parse_local_datetime(task_dict['completed_at'])
                
                task = QueueTask(**task_dict)
                self.tasks[task_id] = task
            
            # 只加载pending状态的任务到队列
            self.pending_queue = [
                task_id for task_id in data.get('pending_queue', [])
                if task_id in self.tasks and self.tasks[task_id].status == 'pending'
            ]
            
            queue_logger.info(f"Loaded {len(self.tasks)} tasks from persistence, {len(self.pending_queue)} pending")

        except Exception as e:
            queue_logger.error(f"Failed to load tasks: {e}")
        else:
            self._log_operation_duration(
                event="load_tasks",
                start=load_start,
                threshold=self._io_warning_threshold,
                message="Loading queue tasks took longer than expected",
                file=str(self.queue_file),
                pending=len(self.pending_queue),
                total=len(self.tasks),
            )

# 全局队列管理器实例
queue_manager = QueueManager()

