"""订单管理器 - 统一管理所有订单的创建、状态更新、取消和修改
将原有的 auto_orders 表重构为 orders 表，支持完整的订单生命周期管理
"""

import sqlite3
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from enum import Enum
from loguru import logger
import re
# Module-specific logger for order_manager
order_logger = logger.bind(module="order_manager", request_id="-")

from .database import get_db_manager


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
    # 新增：软删除标记，防止被 IBKR 同步重新创建
    is_deleted: int = 0
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = self.created_at


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

    def set_dependencies(self, websocket_manager, telegram_notifier, risk_manager=None):
        """设置依赖注入"""
        self.websocket_manager = websocket_manager
        self.telegram_notifier = telegram_notifier
        self.risk_manager = risk_manager
        order_logger.info("OrderManager dependencies set")

    def _initialize_database(self):
        """初始化数据库表结构"""
        try:
            with sqlite3.connect(self.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
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
                        is_deleted INTEGER DEFAULT 0
                    )
                """)
                
                # 兼容老库：缺失 is_deleted 列则补齐
                cursor.execute("PRAGMA table_info('orders')")
                cols = [row[1] for row in cursor.fetchall()]
                if 'is_deleted' not in cols:
                    cursor.execute("ALTER TABLE orders ADD COLUMN is_deleted INTEGER DEFAULT 0")
                    order_logger.info("Added is_deleted column to orders table")
                
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
                
                conn.commit()
                order_logger.info("Orders table initialized successfully")
                
        except Exception as e:
            order_logger.error(f"Error initializing orders database: {e}")
            raise

    def _load_orders_from_database(self):
        """从数据库加载订单到内存"""
        try:
            with sqlite3.connect(self.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT order_id, symbol, action, quantity, order_type, price,
                           status, created_at, updated_at, executed_at, fill_price,
                           order_source, strategy_name, rule_id, parent_order_id,
                           exchange, sec_type, notes, commission, is_deleted
                    FROM orders
                    ORDER BY created_at DESC
                """)
                
                rows = cursor.fetchall()
                
                for row in rows:
                    if row[19]:
                        # 软删除的订单，记录其ID用于去重，避免IBKR重新创建
                        self.deleted_order_ids.add(row[0])
                        continue
                    order = Order(
                        order_id=row[0],
                        symbol=row[1],
                        action=row[2],
                        quantity=row[3],
                        order_type=row[4],
                        price=row[5],
                        status=row[6] or OrderStatus.SUBMITTED.value,
                        created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
                        updated_at=datetime.fromisoformat(row[8]) if row[8] else datetime.now(),
                        executed_at=datetime.fromisoformat(row[9]) if row[9] else None,
                        fill_price=row[10],
                        order_source=row[11] or OrderSource.MANUAL.value,
                        strategy_name=row[12],
                        rule_id=row[13],
                        parent_order_id=row[14],
                        exchange=row[15] or "SMART",
                        sec_type=row[16] or "STK",
                        notes=row[17],
                        commission=row[18],
                        is_deleted=0
                    )
                    self.orders[order.order_id] = order
                
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
                    existing_order.updated_at = datetime.now()
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

    def update_order_status(self, 
                          order_id: str, 
                          status: str,
                          fill_price: Optional[float] = None,
                          executed_at: Optional[datetime] = None,
                          commission: Optional[float] = None) -> bool:
        """更新订单状态"""
        try:
            if order_id not in self.orders:
                order_logger.warning(f"Order {order_id} not found for status update")
                return False
            
            order = self.orders[order_id]
            old_status = order.status
            
            # 防止订单状态回退：定义状态优先级
            status_priority = {
                "PendingSubmit": 1,
                "Submitted": 2,
                "PartiallyFilled": 3,
                "Filled": 4,
                "Cancelled": 4,
                "Rejected": 4
            }
            
            # 检查是否为状态回退
            old_priority = status_priority.get(old_status, 0)
            new_priority = status_priority.get(status, 0)
            
            if new_priority < old_priority:
                # order_logger.warning(f"🚫 防止订单状态回退: {order_id} | {old_status} (优先级{old_priority}) → {status} (优先级{new_priority}) | 跳过更新")
                return False
            
            # 更新订单状态
            order.status = status
            order.updated_at = datetime.now()
            
            if fill_price is not None:
                order.fill_price = fill_price
                if order.price is None or order.price == 0:
                    order.price = fill_price
            
            if executed_at is not None:
                order.executed_at = executed_at
            elif status == OrderStatus.FILLED.value:
                order.executed_at = datetime.now()
            
            if commission is not None:
                order.commission = commission
            
            # 更新数据库
            self._update_order_in_database(order)
            
            # 记录日志
            order_logger.info(f"📊 订单状态更新: {order_id} | {old_status} → {status} | 成交价: {fill_price or 'N/A'}")
            
            # 如果订单状态变为Filled，发送通知
            if status == OrderStatus.FILLED.value and old_status != OrderStatus.FILLED.value:
                self._send_filled_notifications(order)
            
            return True
            
        except Exception as e:
            order_logger.error(f"Error updating order {order_id} status: {e}")
            return False

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
                "executed_at": order.executed_at.isoformat() if order.executed_at else None,
                "message": f"订单已成交: {order.symbol} {order.action} {order.quantity}股 @ {order.fill_price or 'N/A'}"
            }
            
            # 发送浏览器通知
            if hasattr(self, 'websocket_manager') and self.websocket_manager:
                try:
                    import asyncio
                    # 在事件循环中发送WebSocket消息
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(self.websocket_manager.broadcast(message))
                    else:
                        asyncio.run(self.websocket_manager.broadcast(message))
                    order_logger.info(f"🌐 已发送浏览器通知: 订单 {order.order_id} 成交")
                except Exception as e:
                    order_logger.error(f"发送浏览器通知失败: {e}")
            
            # 发送Telegram消息
            if hasattr(self, 'telegram_notifier') and self.telegram_notifier:
                try:
                    self.telegram_notifier.send_order_alert(order)
                    order_logger.info(f"📱 已发送Telegram通知: 订单 {order.order_id} 成交")
                except Exception as e:
                    order_logger.error(f"发送Telegram通知失败: {e}")
            
            # 检查并停用已平仓资产的风险规则
            self._check_and_deactivate_risk_rules(order.symbol)
                    
        except Exception as e:
            order_logger.error(f"发送订单成交通知失败: {e}")
    
    def _check_and_deactivate_risk_rules(self, symbol: str):
        """检查指定资产的持仓状态，如果已平仓则停用相关的风险规则"""
        try:
            if not hasattr(self, 'risk_manager') or not self.risk_manager:
                return
            
            # 获取当前持仓信息
            from .ibkr_client import IBKRClient
            ibkr_client = IBKRClient()
            positions = ibkr_client.get_positions()
            
            # 查找指定资产的持仓
            position_qty = 0
            for position in positions:
                if position.get('symbol') == symbol:
                    position_qty = int(position.get('position', 0))
                    break
            
            # 如果持仓为0，检查是否有激活的风险规则需要停用
            if position_qty == 0:
                if symbol in self.risk_manager.risk_rules:
                    rule = self.risk_manager.risk_rules[symbol]
                    if rule.enabled:
                        # 停用风险规则
                        rule.enabled = False
                        
                        # 更新数据库中的风险规则状态
                        from .database import DatabaseRiskRule
                        from datetime import datetime
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.risk_manager.db_manager.save_risk_rule(db_rule)
                        
                        order_logger.info(f"🔒 已停用风险规则: {symbol} (持仓已平仓)")
                        
                        # 发送通知
                        if hasattr(self, 'telegram_notifier') and self.telegram_notifier:
                            try:
                                message = f"🔒 风险规则已自动停用\n资产: {symbol}\n原因: 持仓已平仓"
                                self.telegram_notifier.send_message(message)
                            except Exception as e:
                                order_logger.error(f"发送风险规则停用通知失败: {e}")
                        
                        # 发送浏览器通知
                        if hasattr(self, 'websocket_manager') and self.websocket_manager:
                            try:
                                notification = {
                                    "type": "risk_rule_deactivated",
                                    "symbol": symbol,
                                    "message": f"风险规则已自动停用: {symbol} (持仓已平仓)"
                                }
                                import asyncio
                                loop = asyncio.get_event_loop()
                                if loop.is_running():
                                    asyncio.create_task(self.websocket_manager.broadcast(notification))
                                else:
                                    asyncio.run(self.websocket_manager.broadcast(notification))
                            except Exception as e:
                                order_logger.error(f"发送风险规则停用浏览器通知失败: {e}")
                    else:
                        order_logger.debug(f"风险规则已经是停用状态: {symbol}")
                else:
                    order_logger.debug(f"未找到风险规则: {symbol}")
            else:
                order_logger.debug(f"资产 {symbol} 仍有持仓 ({position_qty})，保持风险规则激活状态")
                
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
            order.updated_at = datetime.now()
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
            
            order.updated_at = datetime.now()
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
                  limit: Optional[int] = None) -> List[Order]:
        """获取订单列表"""
        orders = list(self.orders.values())
        
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
        
        # 限制数量
        if limit:
            orders = orders[:limit]
        
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

    # 其余方法保持不变...
    def _save_order_to_database(self, order: Order):
        """保存订单到数据库"""
        try:
            with sqlite3.connect(self.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO orders (
                        order_id, symbol, action, quantity, order_type, price,
                        status, created_at, updated_at, executed_at, fill_price,
                        order_source, strategy_name, rule_id, parent_order_id,
                        exchange, sec_type, notes, commission, is_deleted
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order.order_id, order.symbol, order.action, order.quantity,
                    order.order_type, order.price, order.status,
                    order.created_at.isoformat(), order.updated_at.isoformat(),
                    order.executed_at.isoformat() if order.executed_at else None,
                    order.fill_price, order.order_source, order.strategy_name,
                    order.rule_id, order.parent_order_id, order.exchange,
                    order.sec_type, order.notes, order.commission, order.is_deleted
                ))
                conn.commit()
        except Exception as e:
            order_logger.error(f"Error saving order {order.order_id} to database: {e}")

    def _update_order_in_database(self, order: Order):
        """更新数据库中的订单"""
        try:
            with sqlite3.connect(self.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE orders SET
                        symbol=?, action=?, quantity=?, order_type=?, price=?,
                        status=?, updated_at=?, executed_at=?, fill_price=?,
                        order_source=?, strategy_name=?, rule_id=?, parent_order_id=?,
                        exchange=?, sec_type=?, notes=?, commission=?, is_deleted=?
                    WHERE order_id=?
                """, (
                    order.symbol, order.action, order.quantity, order.order_type,
                    order.price, order.status, order.updated_at.isoformat(),
                    order.executed_at.isoformat() if order.executed_at else None,
                    order.fill_price, order.order_source, order.strategy_name,
                    order.rule_id, order.parent_order_id, order.exchange,
                    order.sec_type, order.notes, order.commission, order.is_deleted,
                    order.order_id
                ))
                conn.commit()
        except Exception as e:
            order_logger.error(f"Error updating order {order.order_id} in database: {e}")
    
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
            current_time = datetime.now()
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
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "UPDATE orders SET order_id = ? WHERE order_id = ?",
                        (new_order_id, old_order_id)
                    )
                    conn.commit()
                    
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