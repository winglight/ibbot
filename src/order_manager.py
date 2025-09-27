"""订单管理器 - 统一管理所有订单的创建、状态更新、取消和修改
将原有的 auto_orders 表重构为 orders 表，支持完整的订单生命周期管理
"""

import asyncio
import sqlite3
from typing import Dict, List, Optional, Any, Awaitable
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, fields
from enum import Enum
from loguru import logger
import re
# Module-specific logger for order_manager
order_logger = logger.bind(module="order_manager", request_id="-")

from .database import get_db_manager
from .utils import (
    ensure_local_timezone,
    now_local,
    parse_local_datetime,
    to_local_iso,
)
from .utils.strategy_cache import record_filled_order as cache_record_filled_order

class OrderStatus(Enum):
    """订单状态枚举"""
    SUBMITTED = "Submitted"
    PENDING = "PendingSubmit"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    PARTIALLY_FILLED = "PartiallyFilled"
    UNKNOWN = "Unknown"


class OrderSource(Enum):
    """订单来源枚举"""
    MANUAL = "manual"      # 手动下单
    STRATEGY = "strategy"  # 策略生成
    STOP_LOSS = "sl"       # 止损
    TAKE_PROFIT = "tp"     # 止盈
    AUTO = "auto"          # 其他自动订单
    BOT = "bot"            # 机器人订单


@dataclass
class Order:
    """订单数据模型"""
    order_id: str
    symbol: str
    action: str              # BUY, SELL
    quantity: int
    order_type: str          # MKT, LMT, STP, etc.
    price: Optional[float] = None
    status: str = OrderStatus.SUBMITTED.value
    created_at: datetime = None
    updated_at: datetime = None
    executed_at: Optional[datetime] = None
    fill_price: Optional[float] = None
    
    # 订单元数据
    order_source: str = OrderSource.MANUAL.value
    strategy_name: Optional[str] = None
    rule_id: Optional[str] = None
    parent_order_id: Optional[str] = None  # 父订单ID，用于跟踪订单关系
    
    # 交易所信息
    exchange: str = "SMART"
    sec_type: str = "STK"
    
    # 附加字段
    notes: Optional[str] = None
    commission: Optional[float] = None
    pnl: Optional[float] = None  # 新增：订单盈亏（来自IB API）
    # 新增：软删除标记，防止被 IBKR 同步重新创建
    is_deleted: int = 0

    def __post_init__(self):
        self.created_at = ensure_local_timezone(self.created_at) or now_local()
        self.updated_at = ensure_local_timezone(self.updated_at) or self.created_at
        self.executed_at = ensure_local_timezone(self.executed_at)


class OrderManager:
    """订单管理器 - 统一管理所有订单操作"""
    
    def __init__(self):
        self.db_manager = get_db_manager()
        self.orders: Dict[str, Order] = {}
        # 新增：记录已软删除的订单ID集合（避免IBKR重新创建）
        self.deleted_order_ids: set[str] = set()
        
        # 初始化数据库表结构
        self._initialize_database()
        
        # 从数据库加载现有订单
        self._load_orders_from_database()
        
        # 初始化完成日志
        order_logger.info(f"OrderManager initialized with {len(self.orders)} orders")

    def set_dependencies(self, websocket_manager, notification_manager, risk_manager=None):
        """设置依赖注入"""
        self.websocket_manager = websocket_manager
        self.notification_manager = notification_manager
        self.risk_manager = risk_manager
        order_logger.info("OrderManager dependencies set")

    def _initialize_database(self):
        """初始化数据库表结构"""
        try:
            with self.db_manager.cursor() as cursor:

                # 创建新的 orders 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        order_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        action TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        order_type TEXT NOT NULL,
                        price REAL,
                        status TEXT DEFAULT 'Submitted',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        executed_at TEXT,
                        fill_price REAL,
                        order_source TEXT DEFAULT 'manual',
                        strategy_name TEXT,
                        rule_id TEXT,
                        parent_order_id TEXT,
                        exchange TEXT DEFAULT 'SMART',
                        sec_type TEXT DEFAULT 'STK',
                        notes TEXT,
                        commission REAL,
                        pnl REAL,
                        is_deleted INTEGER DEFAULT 0
                    )
                """)
                
                # 兼容老库：缺失列则补齐
                cursor.execute("PRAGMA table_info('orders')")
                cols = [row[1] for row in cursor.fetchall()]

                # 需要确保存在的字段（旧库可能缺失）
                required_columns = {
                    'executed_at': "TEXT",
                    'fill_price': "REAL",
                    'order_source': "TEXT DEFAULT 'manual'",
                    'strategy_name': "TEXT",
                    'rule_id': "TEXT",
                    'parent_order_id': "TEXT",
                    'exchange': "TEXT DEFAULT 'SMART'",
                    'sec_type': "TEXT DEFAULT 'STK'",
                    'notes': "TEXT",
                    'commission': "REAL",
                    'pnl': "REAL",
                    'is_deleted': "INTEGER DEFAULT 0",
                }

                for column_name, column_def in required_columns.items():
                    if column_name not in cols:
                        cursor.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {column_def}")
                        order_logger.info(f"Added missing column '{column_name}' to orders table")

                # 检查是否需要从 auto_orders 迁移数据
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_orders'")
                auto_orders_exists = cursor.fetchone() is not None
                
                if auto_orders_exists:
                    # 检查 orders 表是否为空
                    cursor.execute("SELECT COUNT(*) FROM orders")
                    orders_count = cursor.fetchone()[0]
                    
                    if orders_count == 0:
                        # 从 auto_orders 迁移数据到 orders
                        order_logger.info("Migrating data from auto_orders to orders table...")
                        cursor.execute("""
                            INSERT INTO orders (
                                order_id, symbol, action, quantity, order_type, price,
                                status, created_at, updated_at, executed_at, order_source, rule_id, exchange, sec_type
                            )
                            SELECT 
                                order_id, symbol, action, quantity, order_type, price,
                                COALESCE(status, 'Submitted') as status,
                                timestamp as created_at,
                                timestamp as updated_at,
                                executed_at,
                                COALESCE(order_source, 'auto') as order_source,
                                rule_id,
                                COALESCE(exchange, 'SMART') as exchange,
                                COALESCE(sec_type, 'STK') as sec_type
                            FROM auto_orders
                        """)
                        migrated_count = cursor.rowcount
                        order_logger.info(f"Migrated {migrated_count} orders from auto_orders to orders")
                
                # 检查并添加缺失的列（用于数据库迁移）
                cursor.execute("PRAGMA table_info(orders)")
                columns = [column[1] for column in cursor.fetchall()]
                
                if 'pnl' not in columns:
                    cursor.execute("ALTER TABLE orders ADD COLUMN pnl REAL")
                    order_logger.info("Added pnl column to orders table")
                
                order_logger.info("Orders table initialized successfully")
                
        except Exception as e:
            order_logger.error(f"Error initializing orders database: {e}")
            raise

    def _row_to_order(self, row: tuple) -> Optional[Order]:
        """将数据库行转换为 ``Order`` 对象并更新内存缓存。"""
        if row[20]:
            # 软删除的订单，记录其ID用于去重，避免IBKR重新创建
            self.deleted_order_ids.add(row[0])
            return None

        order = Order(
            order_id=row[0],
            symbol=row[1],
            action=row[2],
            quantity=row[3],
            order_type=row[4],
            price=row[5],
            status=row[6] or OrderStatus.SUBMITTED.value,
            created_at=parse_local_datetime(row[7]) or now_local(),
            updated_at=parse_local_datetime(row[8]) or None,
            executed_at=parse_local_datetime(row[9]),
            fill_price=row[10],
            order_source=row[11] or OrderSource.MANUAL.value,
            strategy_name=row[12],
            rule_id=row[13],
            parent_order_id=row[14],
            exchange=row[15] or "SMART",
            sec_type=row[16] or "STK",
            notes=row[17],
            commission=row[18],
            pnl=row[19],
            is_deleted=0
        )
        self.orders[order.order_id] = order
        return order

    def _ensure_order_cached(self, order_id: str) -> Optional[Order]:
        """确保指定订单已加载到内存缓存。"""

        existing = self.orders.get(order_id)
        if existing is not None:
            return existing

        try:
            with self.db_manager.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT order_id, symbol, action, quantity, order_type, price,
                           status, created_at, updated_at, executed_at, fill_price,
                           order_source, strategy_name, rule_id, parent_order_id,
                           exchange, sec_type, notes, commission, pnl, is_deleted
                    FROM orders
                    WHERE order_id = ?
                    LIMIT 1
                    """,
                    (order_id,),
                )
                row = cursor.fetchone()
            if row:
                return self._row_to_order(row)
        except Exception as exc:
            order_logger.error(f"Failed to load order {order_id} from database: {exc}")
        return None

    def _fetch_orders_from_database(self,
                                    limit: Optional[int] = None,
                                    start_date: Optional[datetime] = None,
                                    offset: Optional[int] = None,
                                    return_total: bool = False) -> Any:
        """按需从数据库获取订单，避免全量加载导致的性能问题。

        当 ``return_total`` 为 ``True`` 时，同时返回符合条件的总记录数。
        """
        try:
            with self.db_manager.cursor() as cursor:
                base_query = """
                    FROM orders
                    WHERE is_deleted = 0
                """
                params: List[Any] = []

                if start_date:
                    start_dt = ensure_local_timezone(start_date)
                    base_query += " AND created_at >= ?"
                    params.append(start_dt.isoformat())

                count_total: Optional[int] = None
                if return_total:
                    count_query = "SELECT COUNT(*) " + base_query
                    cursor.execute(count_query, list(params))
                    row = cursor.fetchone()
                    count_total = int(row[0]) if row and row[0] is not None else 0

                query = "SELECT order_id, symbol, action, quantity, order_type, price, " \
                        "status, created_at, updated_at, executed_at, fill_price, " \
                        "order_source, strategy_name, rule_id, parent_order_id, " \
                        "exchange, sec_type, notes, commission, pnl, is_deleted " + base_query + " ORDER BY created_at DESC"

                query_params: List[Any] = list(params)

                if limit:
                    query += " LIMIT ?"
                    query_params.append(limit)
                elif offset and offset > 0:
                    query += " LIMIT -1"

                if offset and offset > 0:
                    query += " OFFSET ?"
                    query_params.append(offset)

                cursor.execute(query, query_params)
                rows = cursor.fetchall()

            orders: List[Order] = []
            for row in rows:
                order = self._row_to_order(row)
                if order:
                    orders.append(order)
            if return_total:
                return orders, (count_total or 0)
            return orders
        except Exception as e:
            order_logger.error(f"Error fetching orders from database: {e}")
            raise

    def _load_orders_from_database(self):
        """从数据库加载订单到内存"""
        try:
            with self.db_manager.cursor() as cursor:
                cursor.execute("""
                    SELECT order_id, symbol, action, quantity, order_type, price,
                           status, created_at, updated_at, executed_at, fill_price,
                           order_source, strategy_name, rule_id, parent_order_id,
                           exchange, sec_type, notes, commission, pnl, is_deleted
                    FROM orders
                    ORDER BY created_at DESC
                """)

                rows = cursor.fetchall()

                for row in rows:
                    self._row_to_order(row)

                order_logger.info(f"Loaded {len(self.orders)} orders from database")

        except Exception as e:
            order_logger.error(f"Error loading orders from database: {e}")

    def create_order(self,
                    order_id: str,
                    symbol: str,
                    action: str,
                    quantity: int,
                    order_type: str,
                    price: Optional[float] = None,
                    order_source: str = OrderSource.MANUAL.value,
                    strategy_name: Optional[str] = None,
                    rule_id: Optional[str] = None,
                    parent_order_id: Optional[str] = None,
                    exchange: str = "SMART",
                    sec_type: str = "STK",
                    notes: Optional[str] = None) -> Order:
        """创建新订单"""
        try:
            # 检查订单是否已存在，避免覆盖非手工订单
            if order_id in self.orders:
                existing_order = self.orders[order_id]
                # 如果现有订单不是手工订单，且新订单是手工订单，则不应覆盖
                if (existing_order.order_source != OrderSource.MANUAL.value and 
                    order_source == OrderSource.MANUAL.value):
                    order_logger.warning(
                        f"⚠️ 拒绝覆盖非手工订单 {order_id}: 现有来源={existing_order.order_source}, "
                        f"尝试覆盖为={order_source}. 保持现有订单不变。"
                    )
                    return existing_order
                # 如果现有订单是自动订单（tp/sl等），且新订单也是自动订单，保留原有order_source
                elif (existing_order.order_source in ["tp", "sl", "auto", "strategy"] and 
                      order_source == OrderSource.MANUAL.value):
                    order_logger.warning(
                        f"⚠️ 保护自动订单 {order_id}: 保持原有来源={existing_order.order_source}, "
                        f"拒绝覆盖为={order_source}"
                    )
                    # 只更新状态和价格，保持原有order_source
                    existing_order.status = OrderStatus.SUBMITTED.value
                    if price is not None:
                        existing_order.price = price
                    existing_order.updated_at = now_local()
                    self._update_order_in_database(existing_order)
                    return existing_order
            
            # 创建订单对象
            order = Order(
                order_id=order_id,
                symbol=symbol,
                action=action,
                quantity=quantity,
                order_type=order_type,
                price=price,
                status=OrderStatus.SUBMITTED.value,
                order_source=order_source,
                strategy_name=strategy_name,
                rule_id=rule_id,
                parent_order_id=parent_order_id,
                exchange=exchange,
                sec_type=sec_type,
                notes=notes
            )
            
            # 保存到内存
            self.orders[order_id] = order
            
            # 保存到数据库
            self._save_order_to_database(order)
            
            # 记录日志
            order_logger.info(f"📋 订单创建: {order_id} | {symbol} {action} {quantity} @ {price or 'MKT'} | 来源: {order_source}")
            
            return order
            
        except Exception as e:
            order_logger.error(f"Error creating order {order_id}: {e}")
            raise

    def add_order(self, order: "Order" | Dict[str, Any]) -> "Order":
        """将外部生成的订单同步到订单管理器

        该方法会在订单已存在时更新基础信息；如果是新订单，则写入内存与数据库。
        """
        try:
            if not isinstance(order, Order):
                if isinstance(order, dict):
                    order = Order(**order)
                else:
                    raise TypeError("order must be an Order instance or dict")

            # 统一字符串格式的订单ID
            order_id = str(order.order_id)
            order.order_id = order_id

            # 确保关键字段有默认值
            if not order.status:
                order.status = OrderStatus.SUBMITTED.value
            if not order.order_source:
                order.order_source = OrderSource.MANUAL.value
            if order.created_at is None:
                order.created_at = now_local()
            order.created_at = ensure_local_timezone(order.created_at)
            order.updated_at = ensure_local_timezone(order.updated_at) or now_local()
            order.executed_at = ensure_local_timezone(order.executed_at)
            order.is_deleted = 0

            # 如果之前被软删除，重新激活
            if order_id in self.deleted_order_ids:
                self.deleted_order_ids.discard(order_id)

            existing_order = self.orders.get(order_id)
            if existing_order:
                status_priority = {
                    "PendingSubmit": 1,
                    "Submitted": 2,
                    "PartiallyFilled": 3,
                    "Filled": 4,
                    "Cancelled": 4,
                    "Rejected": 4,
                }

                new_status = order.status or existing_order.status
                if new_status:
                    old_priority = status_priority.get(existing_order.status, 0)
                    new_priority = status_priority.get(new_status, 0)
                    if new_priority >= old_priority:
                        existing_order.status = new_status

                for field in fields(Order):
                    name = field.name
                    if name in {"order_id", "status"}:
                        continue
                    value = getattr(order, name)
                    if value is None:
                        continue
                    if name == "created_at" and existing_order.created_at is not None:
                        continue
                    if name == "updated_at":
                        continue
                    setattr(existing_order, name, value)

                existing_order.updated_at = now_local()
                existing_order.is_deleted = 0
                self._update_order_in_database(existing_order)
                order_logger.info(f"🔄 更新已有订单信息: {order_id}")
                return existing_order

            # 新订单写入内存与数据库
            self.orders[order_id] = order
            self._save_order_to_database(order)
            order_logger.info(f"📥 新订单已添加到管理器: {order_id}")
            return order

        except Exception as e:
            order_logger.error(f"Error adding order {getattr(order, 'order_id', 'unknown')}: {e}")
            raise

    def update_order_status(self,
                          order_id: str,
                          status: str,
                          fill_price: Optional[float] = None,
                          executed_at: Optional[datetime] = None,
                          commission: Optional[float] = None,
                          pnl: Optional[float] = None,
                          notes: Optional[str] = None) -> bool:
        """更新订单状态"""
        try:
            # 统一标准化为字符串，避免传入 int 导致无法匹配内存订单
            order_id = str(order_id)
            order = self._ensure_order_cached(order_id)
            if order is None:
                order_logger.warning(f"Order {order_id} not found for status update")
                return False

            old_status = order.status
            
            # 防止订单状态回退：定义状态优先级
            status_priority = {
                "PendingSubmit": 1,
                "Submitted": 2,
                "PartiallyFilled": 3,
                "Filled": 4,
                "Cancelled": 4,
                "Rejected": 4,
                "Inactive": 4  # IBKR的Inactive状态映射为相同优先级，让后续处理决定具体映射
            }
            
            # 检查是否为状态回退
            old_priority = status_priority.get(old_status, 0)
            new_priority = status_priority.get(status, 0)
            
            if new_priority < old_priority:
                # order_logger.warning(f"🚫 防止订单状态回退: {order_id} | {old_status} (优先级{old_priority}) → {status} (优先级{new_priority}) | 跳过更新")
                return False
            
            # 更新订单状态
            order.status = status
            order.updated_at = now_local()

            if fill_price is not None:
                order.fill_price = fill_price
                if order.price is None or order.price == 0:
                    order.price = fill_price

            if executed_at is not None:
                order.executed_at = ensure_local_timezone(executed_at)
            elif status == OrderStatus.FILLED.value:
                order.executed_at = now_local()

            if notes:
                order.notes = f"{order.notes} | {notes}" if order.notes else notes
            
            # 只有在新值不为None时才更新commission和pnl，避免覆盖已有的有效值
            if commission is not None:
                order.commission = commission
                order_logger.debug(f"Updated order {order_id} commission: {commission}")
            
            if pnl is not None:
                order.pnl = pnl
                order_logger.debug(f"Updated order {order_id} PnL: {pnl}")
            
            # 更新数据库
            self._update_order_in_database(order)

            # 记录日志
            order_logger.info(f"📊 订单状态更新: {order_id} | {old_status} → {status} | 成交价: {fill_price or 'N/A'}")

            if status == OrderStatus.FILLED.value:
                try:
                    self._schedule_async(
                        cache_record_filled_order(order),
                        "Strategy cache update",
                    )
                except Exception as exc:
                    order_logger.debug(
                        f"Failed to enqueue strategy cache update for order {order_id}: {exc}"
                    )

            # 如果订单状态变为Filled，发送通知
            if status == OrderStatus.FILLED.value and old_status != OrderStatus.FILLED.value:
                self._send_filled_notifications(order)
            
            return True
            
        except Exception as e:
            order_logger.error(f"Error updating order {order_id} status: {e}")
            return False

    def _schedule_async(self, coro: Awaitable, context: str):
        """Run an async coroutine safely whether or not a loop is active."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            task = loop.create_task(coro)

            def _log_task_result(task: "asyncio.Task"):
                try:
                    task.result()
                except Exception as exc:  # pragma: no cover - defensive logging
                    order_logger.error(f"{context} failed: {exc}")

            task.add_done_callback(_log_task_result)
        else:
            try:
                asyncio.run(coro)
            except Exception as exc:  # pragma: no cover - defensive logging
                order_logger.error(f"{context} failed: {exc}")

    def _send_filled_notifications(self, order: Order):
        """发送订单成交通知（浏览器通知和Telegram消息）"""
        try:
            # 构建通知消息
            message = {
                "type": "order_filled",
                "order_id": order.order_id,
                "symbol": order.symbol,
                "action": order.action,
                "quantity": order.quantity,
                "fill_price": order.fill_price,
                "order_type": order.order_type,
                "executed_at": to_local_iso(order.executed_at),
                "commission": order.commission,
                "pnl": order.pnl,
                "message": f"订单已成交: {order.symbol} {order.action} {order.quantity}股 @ {order.fill_price or 'N/A'}"
            }
            
            order_logger.info(f"Sending WebSocket message for order {order.order_id}: commission={order.commission}, pnl={order.pnl}")
            
            # 发送浏览器通知
            if hasattr(self, 'websocket_manager') and self.websocket_manager:
                try:
                    async def _broadcast():
                        await self.websocket_manager.broadcast(message)
                        try:
                            import core  # 延迟导入避免循环导入
                            positions = await core.ibkr_client.get_positions()
                        except Exception:
                            positions = []
                        await self.websocket_manager.broadcast(
                            {"type": "portfolio_update", "portfolio": positions}
                        )

                    self._schedule_async(_broadcast(), "WebSocket broadcast")
                    order_logger.info(f"🌐 已发送浏览器通知: 订单 {order.order_id} 成交")
                except Exception as e:
                    order_logger.error(f"发送浏览器通知失败: {e}")

            # 发送通知消息
            if hasattr(self, 'notification_manager') and self.notification_manager:
                try:
                    # 使用统一的通知管理器发送订单通知
                    self._schedule_async(
                        self.notification_manager.send_order_notification(
                            order_type=order.order_type,
                            symbol=order.symbol,
                            quantity=order.quantity,
                            price=order.fill_price,
                            status="Filled",
                            order_id=order.order_id
                        ),
                        "Order notification dispatch"
                    )
                    order_logger.info(f"📱 已发送Telegram通知: 订单 {order.order_id} 成交")
                except Exception as e:
                    order_logger.error(f"发送Telegram通知失败: {e}")

            # 检查并停用已平仓资产的风险规则
            self._schedule_async(
                self._check_and_deactivate_risk_rules(order.symbol, order.order_source),
                "Risk rule deactivation"
            )

            # 异步刷新账户与持仓快照，确保API能够快速返回最新数据
            try:
                import core  # 延迟导入避免循环依赖

                snapshot_service = getattr(core, "account_snapshot_service", None)
                if snapshot_service:
                    snapshot_service.notify_fill()
                    self._schedule_async(
                        snapshot_service.refresh_both(
                            reason="fill_event", fast_positions=True
                        ),
                        "Account snapshot refresh after fill",
                    )
                else:
                    async def _refresh_fallback():
                        ib_client = getattr(core, "ibkr_client", None)
                        if ib_client is None:
                            return
                        from .account_snapshot_cache import (
                            write_account_info_snapshot,
                            write_positions_snapshot,
                        )

                        account_info = await ib_client.get_account_info()
                        positions = await ib_client.get_positions(fast=True)
                        await write_account_info_snapshot(
                            account_info or {}, "fill_event"
                        )
                        await write_positions_snapshot(positions or [], "fill_event")

                    self._schedule_async(
                        _refresh_fallback(),
                        "Account snapshot refresh after fill (fallback)",
                    )
            except Exception as refresh_exc:
                order_logger.warning(
                    f"Failed to refresh account snapshots after fill: {refresh_exc}"
                )

        except Exception as e:
            order_logger.error(f"发送订单成交通知失败: {e}")
    
    async def _check_and_deactivate_risk_rules(self, symbol: str, order_source: str = None):
        """检查指定资产的持仓状态和订单来源，决定是否停用相关的风险规则"""
        try:
            if not hasattr(self, 'risk_manager') or not self.risk_manager:
                return
            
            # 检查是否有激活的风险规则
            if symbol not in self.risk_manager.risk_rules:
                return
                
            rule = self.risk_manager.risk_rules[symbol]
            if not rule.enabled:
                return
            
            should_deactivate = False
            deactivate_reason = ""
            
            # 如果是手动订单，立即反激活风险规则
            if order_source == "manual":
                should_deactivate = True
                deactivate_reason = "手动下单"
            else:
                # 获取当前持仓信息
                import core  # 延迟导入避免循环导入
                positions = await core.ibkr_client.get_positions()
                
                # 查找指定资产的持仓
                position_qty = 0
                for position in positions:
                    if position.get('symbol') == symbol:
                        position_qty = int(position.get('position', 0))
                        break
                
                # 如果持仓为0，停用风险规则
                if position_qty == 0:
                    should_deactivate = True
                    deactivate_reason = "持仓已平仓"
            
            # 执行反激活操作
            if should_deactivate:
                # 停用风险规则
                rule.enabled = False
                
                # 更新数据库中的风险规则状态
                from .database import RiskRule as DatabaseRiskRule
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    timestamp=to_local_iso(rule.created_at) or now_local().isoformat()
                )
                self.risk_manager.db_manager.save_risk_rule(db_rule)
                
                order_logger.info(f"🔒 已停用风险规则: {symbol} ({deactivate_reason})")
                
                # 发送通知
                if hasattr(self, 'notification_manager') and self.notification_manager:
                    try:
                        message = f"🔒 风险规则已自动停用\n资产: {symbol}\n原因: {deactivate_reason}"
                        import asyncio
                        try:
                            loop = asyncio.get_running_loop()
                            asyncio.create_task(self.notification_manager.send_notification(
                                message=message,
                                event_type="risk_rule_deactivated",
                                symbol=symbol,
                                category="risk"
                            ))
                        except RuntimeError:
                            asyncio.run(self.notification_manager.send_notification(
                                message=message,
                                event_type="risk_rule_deactivated",
                                symbol=symbol,
                                category="risk"
                            ))
                    except Exception as e:
                        order_logger.error(f"发送风险规则停用通知失败: {e}")
                
        except Exception as e:
            order_logger.error(f"检查和停用风险规则失败 ({symbol}): {e}")

    def cancel_order(self, order_id: str, reason: Optional[str] = None) -> bool:
        """取消订单"""
        try:
            if order_id not in self.orders:
                order_logger.warning(f"Order {order_id} not found for cancellation")
                return False
            
            order = self.orders[order_id]
            
            # 检查订单是否可以取消
            if order.status in [OrderStatus.FILLED.value, OrderStatus.CANCELLED.value]:
                order_logger.warning(f"Order {order_id} cannot be cancelled (status: {order.status})")
                return False
            
            # 更新状态
            order.status = OrderStatus.CANCELLED.value
            order.updated_at = now_local()
            if reason:
                order.notes = (order.notes or "") + f" | 取消原因: {reason}"
            
            # 更新数据库
            self._update_order_in_database(order)
            
            # 记录日志
            order_logger.info(f"❌ 订单取消: {order_id} | 原因: {reason or '用户请求'}")
            
            return True
            
        except Exception as e:
            order_logger.error(f"Error cancelling order {order_id}: {e}")
            return False

    def modify_order(self, 
                    order_id: str,
                    new_price: Optional[float] = None,
                    new_quantity: Optional[int] = None) -> bool:
        """修改订单"""
        try:
            if order_id not in self.orders:
                order_logger.warning(f"Order {order_id} not found for modification")
                return False
            
            order = self.orders[order_id]
            
            # 检查订单是否可以修改
            if order.status not in [OrderStatus.SUBMITTED.value, OrderStatus.PENDING.value]:
                order_logger.warning(f"Order {order_id} cannot be modified (status: {order.status})")
                return False
            
            # 记录修改前的值
            old_price = order.price
            old_quantity = order.quantity
            
            # 更新字段
            if new_price is not None:
                order.price = new_price
            if new_quantity is not None:
                order.quantity = new_quantity
            
            order.updated_at = now_local()
            order.notes = (order.notes or "") + f" | 修改: 价格 {old_price} → {order.price}, 数量 {old_quantity} → {order.quantity}"
            
            # 更新数据库
            self._update_order_in_database(order)
            
            # 记录日志
            order_logger.info(f"✏️ 订单修改: {order_id} | 价格: {old_price} → {order.price}, 数量: {old_quantity} → {order.quantity}")
            
            return True
            
        except Exception as e:
            order_logger.error(f"Error modifying order {order_id}: {e}")
            return False

    def get_order(self, order_id: str) -> Optional[Order]:
        """获取单个订单"""
        return self.orders.get(order_id)

    def get_orders(self,
                  symbol: Optional[str] = None,
                  status: Optional[str] = None,
                  order_source: Optional[str] = None,
                  strategy_name: Optional[str] = None,
                  limit: Optional[int] = None,
                  start_date: Optional[datetime] = None,
                  offset: Optional[int] = None,
                  return_total: bool = False) -> Any:
        """获取订单列表"""
        simple_filter = not any([symbol, status, order_source, strategy_name])

        # 对于常见的“近期订单”请求（仅限制数量或起始时间）直接从数据库分页读取，避免全量加载。
        if simple_filter and (limit is not None or start_date is not None or offset):
            return self._fetch_orders_from_database(
                limit=limit,
                start_date=start_date,
                offset=offset,
                return_total=return_total,
            )

        self._load_orders_from_database()
        orders = list(self.orders.values())

        if start_date:
            start_dt = ensure_local_timezone(start_date)
            orders = [o for o in orders if ensure_local_timezone(o.created_at) >= start_dt]

        # 过滤条件
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        if status:
            orders = [o for o in orders if o.status == status]
        if order_source:
            orders = [o for o in orders if o.order_source == order_source]
        if strategy_name:
            orders = [o for o in orders if o.strategy_name == strategy_name]

        # 按创建时间降序排列
        orders.sort(key=lambda x: x.created_at, reverse=True)

        total_count = len(orders)

        if offset and offset > 0:
            orders = orders[offset:]

        # 限制数量
        if limit:
            orders = orders[:limit]

        if return_total:
            return orders, total_count

        return orders

    def get_active_orders(self) -> List[Order]:
        """获取活跃订单（未完成状态）"""
        active_statuses = [
            OrderStatus.SUBMITTED.value,
            OrderStatus.PENDING.value,
            OrderStatus.PARTIALLY_FILLED.value
        ]
        return [order for order in self.orders.values() if order.status in active_statuses]

    def get_orders_by_strategy(self, strategy_name: str) -> List[Order]:
        """获取特定策略的所有订单"""
        return [order for order in self.orders.values() if order.strategy_name == strategy_name]

    def find_potential_match(self, symbol, sec_type, action, quantity, time_window_seconds=300):
        """根据合约符号、证券类型、操作和数量在指定时间窗口内查找潜在匹配的订单
        
        Args:
            symbol: 合约符号
            sec_type: 证券类型
            action: 操作（BUY/SELL）
            quantity: 数量
            time_window_seconds: 时间窗口（秒），默认300秒
            
        Returns:
            匹配的订单对象，如果没有找到则返回None
        """
        try:
            current_time = now_local()
            time_threshold = current_time - timedelta(seconds=time_window_seconds)
            
            # 在内存中查找匹配的订单
            for order in self.orders.values():
                if (order.symbol == symbol and 
                    order.sec_type == sec_type and 
                    order.action == action and 
                    abs(order.quantity - quantity) < 1e-6 and  # 浮点数比较
                    order.created_at >= time_threshold and
                    order.status in [OrderStatus.SUBMITTED, OrderStatus.PENDING_SUBMIT]):
                    
                    order_logger.info(f"找到潜在匹配订单: {order.order_id}, symbol={symbol}, action={action}, quantity={quantity}")
                    return order
            
            order_logger.debug(f"未找到匹配订单: symbol={symbol}, action={action}, quantity={quantity}, time_window={time_window_seconds}s")
            return None
            
        except Exception as e:
            order_logger.error(f"查找潜在匹配订单时出错: {e}")
            return None
    
    def rekey_order_id(self, old_order_id, new_order_id):
        """更新订单ID，同时更新内存和数据库
        
        Args:
            old_order_id: 旧的订单ID
            new_order_id: 新的订单ID
            
        Returns:
            bool: 是否成功更新
        """
        try:
            if old_order_id not in self.orders:
                order_logger.warning(f"订单ID {old_order_id} 不存在，无法更新为 {new_order_id}")
                return False
            
            if new_order_id in self.orders:
                order_logger.warning(f"新订单ID {new_order_id} 已存在，无法更新")
                return False
            
            # 获取订单对象
            order = self.orders[old_order_id]
            
            # 更新内存中的订单ID
            del self.orders[old_order_id]
            order.order_id = new_order_id
            self.orders[new_order_id] = order
            
            # 更新数据库中的订单ID
            try:
                with self.db_manager.cursor() as cursor:
                    cursor.execute(
                        "UPDATE orders SET order_id = ? WHERE order_id = ?",
                        (new_order_id, old_order_id)
                    )

                order_logger.info(f"成功更新订单ID: {old_order_id} -> {new_order_id}")
                return True
                
            except Exception as db_error:
                # 如果数据库更新失败，回滚内存中的更改
                del self.orders[new_order_id]
                order.order_id = old_order_id
                self.orders[old_order_id] = order
                order_logger.error(f"数据库更新订单ID失败，已回滚: {db_error}")
                return False
                
        except Exception as e:
            order_logger.error(f"更新订单ID时出错: {e}")
            return False
    
    def _save_order_to_database(self, order: Order):
        """保存订单到数据库"""
        try:
            with self.db_manager.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO orders
                    (order_id, symbol, action, quantity, order_type, price, status,
                     created_at, updated_at, executed_at, fill_price, order_source,
                     strategy_name, rule_id, parent_order_id, exchange, sec_type,
                     notes, commission, is_deleted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.order_id, order.symbol, order.action, order.quantity,
                        order.order_type, order.price, order.status,
                        to_local_iso(order.created_at) or now_local().isoformat(),
                        to_local_iso(order.updated_at) or now_local().isoformat(),
                        to_local_iso(order.executed_at),
                        order.fill_price, order.order_source, order.strategy_name,
                        order.rule_id, order.parent_order_id, order.exchange,
                        order.sec_type, order.notes, order.commission, order.is_deleted
                    )
                )
                order_logger.debug(f"Order {order.order_id} saved to database")
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: orders.order_id" in str(e):
                order_logger.warning(
                    f"Order {order.order_id} already exists in database. Falling back to update."
                )
                self._update_order_in_database(order)
            else:
                order_logger.error(f"Error saving order {order.order_id} to database: {e}")
                raise
        except Exception as e:
            order_logger.error(f"Error saving order {order.order_id} to database: {e}")
            raise
    
    def _update_order_in_database(self, order: Order):
        """更新数据库中的订单"""
        try:
            with self.db_manager.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE orders SET
                        symbol=?, action=?, quantity=?, order_type=?, price=?, status=?,
                        updated_at=?, executed_at=?, fill_price=?, order_source=?,
                        strategy_name=?, rule_id=?, parent_order_id=?, exchange=?,
                        sec_type=?, notes=?, commission=COALESCE(?, commission), pnl=COALESCE(?, pnl), is_deleted=?
                    WHERE order_id=?
                    """,
                    (
                        order.symbol, order.action, order.quantity, order.order_type,
                        order.price, order.status, 
                        to_local_iso(order.updated_at) or now_local().isoformat(),
                        to_local_iso(order.executed_at),
                        order.fill_price, order.order_source, order.strategy_name,
                        order.rule_id, order.parent_order_id, order.exchange,
                        order.sec_type, order.notes, order.commission, order.pnl, order.is_deleted,
                        order.order_id
                    )
                )
                order_logger.debug(f"Order {order.order_id} updated in database (commission/pnl preserved when None)")
        except Exception as e:
            order_logger.error(f"Error updating order {order.order_id} in database: {e}")
            raise

# 全局 OrderManager 单例
_order_manager = None


def get_order_manager():
    """获取 OrderManager 单例实例"""
    global _order_manager
    if _order_manager is None:
        _order_manager = OrderManager()
    return _order_manager