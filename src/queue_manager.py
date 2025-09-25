import asyncio
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from loguru import logger
import json

# 为队列管理器绑定模块日志记录器
queue_logger = logger.bind(module="queue_manager", request_id="-")
from pathlib import Path

from .database import Order
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
        self.processing_tasks: Dict[str, asyncio.Task] = {}
        self.max_concurrent_tasks = max_concurrent_tasks
        self.is_running = False
        self._worker_task: Optional[asyncio.Task] = None
        
        # 依赖注入的对象
        self.risk_manager = None
        self.strategy_manager = None
        self.websocket_manager = None
        self.order_manager = None
        
        # 确保数据目录存在
        self.data_dir = Path("data")
        self.data_dir.mkdir(exist_ok=True)
        self.queue_file = self.data_dir / "queue_tasks.json"
        
        # 启动时加载持久化的任务
        self._load_tasks()
    
    def set_dependencies(self, risk_manager, strategy_manager, websocket_manager, order_manager=None):
        """设置依赖对象"""
        self.risk_manager = risk_manager
        self.strategy_manager = strategy_manager
        self.websocket_manager = websocket_manager
        self.order_manager = order_manager
        queue_logger.info("Dependencies set for QueueManager")
    
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
        
        self.is_running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        queue_logger.info("Queue worker started")
    
    async def stop_worker(self):
        """停止队列工作器"""
        self.is_running = False
        
        # 取消所有正在处理的任务
        for task in self.processing_tasks.values():
            task.cancel()
        
        # 等待工作器任务完成
        if self._worker_task:
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        queue_logger.info("Queue worker stopped")
    
    async def _worker_loop(self):
        """队列工作器主循环"""
        while self.is_running:
            try:
                # 检查是否有待处理的任务且未达到并发限制
                if (len(self.processing_tasks) < self.max_concurrent_tasks and 
                    len(self.pending_queue) > 0):
                    
                    # 从队列头部取出任务
                    task_id = self.pending_queue.pop(0)
                    task = self.tasks.get(task_id)
                    
                    import core
                    if task and task.status == "pending":
                        # 当任务需要IBKR连接但当前未连接时，延后处理，避免无意义的失败与重试
                        if task.task_type in ("strategy_order",) and not core.ibkr_client.is_connected():
                            # 重新放回队列尾部，并短暂休眠以避免紧密轮询
                            self.pending_queue.append(task_id)
                            queue_logger.debug(f"Deferring task {task_id} due to IBKR not connected")
                            await asyncio.sleep(0.5)
                            continue
                        
                        # 启动任务处理
                        processing_task = asyncio.create_task(
                            self._process_task(task)
                        )
                        self.processing_tasks[task_id] = processing_task
                
                # 清理已完成的任务
                completed_task_ids = []
                for task_id, processing_task in self.processing_tasks.items():
                    if processing_task.done():
                        completed_task_ids.append(task_id)
                
                for task_id in completed_task_ids:
                    del self.processing_tasks[task_id]
                
                # 短暂休眠避免CPU占用过高
                await asyncio.sleep(0.1)
                
            except Exception as e:
                queue_logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(1)

    async def _process_task(self, task: QueueTask):
        """处理单个任务"""
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
            import core
            if ("Not connected to IBKR" in error_msg) or (not core.ibkr_client.is_connected()):
                task.status = "pending"
                task.started_at = None
                # 重新排队等待连接恢复
                self.pending_queue.append(task.task_id)
                queue_logger.warning(f"Task {task.task_id} deferred: {error_msg}; will retry when connected")
                # 小幅休眠避免紧密轮询
                await asyncio.sleep(0.5)
            elif "Strategy disabled" in error_msg or "Strategy not found" in error_msg:
                # 对于禁用或不存在的策略，不进行重试，直接标记为失败
                task.status = "failed"
                task.completed_at = now_local()
                queue_logger.error(f"Task {task.task_id} failed permanently: {error_msg}")
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
            self._save_tasks()
    
    async def _process_strategy_order(self, task: QueueTask):
        """处理策略订单任务"""
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
        if not core.ibkr_client.is_connected():
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
        if payload['symbol'] in ['MNQ', 'NQ', 'ES', 'MES']:
            sec_type = 'FUT'
            exchange = 'CME'
        else:
            exchange = payload.get('exchange', 'SMART')
        
        pre_trade_state: Optional[Dict[str, Any]] = None
        if strategy.get('strategy_type') == 'internal' and self.strategy_manager:
            try:
                pre_trade_state = await self.strategy_manager.prepare_internal_order(
                    strategy_name=strategy['name'],
                    symbol=payload['symbol'],
                    action=action,
                    quantity=payload['qty'],
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

        if order_type == "MARKET":
            trade = await core.ibkr_client.place_market_order(
                symbol=payload['symbol'],
                quantity=payload['qty'],
                action=action,
                exchange=exchange,
                sec_type=sec_type
            )
        elif order_type == "LIMIT":
            if not payload.get('price'):
                raise Exception("Price required for limit order")
            trade = core.ibkr_client.place_limit_order(
                symbol=payload['symbol'],
                quantity=payload['qty'],
                price=payload['price'],
                action=action,
                exchange=exchange,
                sec_type=sec_type
            )
        else:
            raise Exception("Invalid order type")
        
        if not trade:
            raise Exception("Failed to place strategy order")
        order_id = str(trade.order.orderId)
        
        if not order_id:
            raise Exception("Failed to obtain order id")

        if strategy.get('strategy_type') == 'internal' and self.strategy_manager:
            try:
                await self.strategy_manager.finalize_internal_order(
                    strategy_name=strategy['name'],
                    symbol=payload['symbol'],
                    action=action,
                    quantity=payload['qty'],
                    pre_trade=pre_trade_state,
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
    
    def _save_tasks(self):
        """持久化任务到文件"""
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
    
    def _load_tasks(self):
        """从文件加载任务"""
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

# 全局队列管理器实例
queue_manager = QueueManager()

