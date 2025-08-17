"""
订单管理器 - 统一管理所有订单的创建、状态更新、取消和修改
将原有的 auto_orders 表重构为 orders 表，支持完整的订单生命周期管理
"""

import sqlite3
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, asdict
from enum import Enum
from loguru import logger

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
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = self.created_at


class OrderManager:
    """订单管理器 - 统一管理所有订单操作"""
    
    def __init__(self):
        self.db_manager = get_db_manager()
        
        # 内存缓存订单数据
        self.orders: Dict[str, Order] = {}
        
        # 初始化数据库表结构
        self._initialize_database()
        
        # 从数据库加载现有订单
        self._load_orders_from_database()
        
        logger.info(f"OrderManager initialized with {len(self.orders)} orders")

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
                        commission REAL
                    )
                """)
                
                # 检查是否需要从 auto_orders 迁移数据
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_orders'")
                auto_orders_exists = cursor.fetchone() is not None
                
                if auto_orders_exists:
                    # 检查 orders 表是否为空
                    cursor.execute("SELECT COUNT(*) FROM orders")
                    orders_count = cursor.fetchone()[0]
                    
                    if orders_count == 0:
                        # 从 auto_orders 迁移数据到 orders
                        logger.info("Migrating data from auto_orders to orders table...")
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
                        logger.info(f"Migrated {migrated_count} orders from auto_orders to orders")
                
                conn.commit()
                logger.info("Orders table initialized successfully")
                
        except Exception as e:
            logger.error(f"Error initializing orders database: {e}")
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
                           exchange, sec_type, notes, commission
                    FROM orders
                    ORDER BY created_at DESC
                """)
                
                rows = cursor.fetchall()
                
                for row in rows:
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
                        commission=row[18]
                    )
                    self.orders[order.order_id] = order
                
                logger.info(f"Loaded {len(self.orders)} orders from database")
                
        except Exception as e:
            logger.error(f"Error loading orders from database: {e}")

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
            logger.info(f"📋 订单创建: {order_id} | {symbol} {action} {quantity} @ {price or 'MKT'} | 来源: {order_source}")
            
            return order
            
        except Exception as e:
            logger.error(f"Error creating order {order_id}: {e}")
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
                logger.warning(f"Order {order_id} not found for status update")
                return False
            
            order = self.orders[order_id]
            old_status = order.status
            
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
            logger.info(f"📊 订单状态更新: {order_id} | {old_status} → {status} | 成交价: {fill_price or 'N/A'}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating order {order_id} status: {e}")
            return False

    def cancel_order(self, order_id: str, reason: Optional[str] = None) -> bool:
        """取消订单"""
        try:
            if order_id not in self.orders:
                logger.warning(f"Order {order_id} not found for cancellation")
                return False
            
            order = self.orders[order_id]
            
            # 检查订单是否可以取消
            if order.status in [OrderStatus.FILLED.value, OrderStatus.CANCELLED.value]:
                logger.warning(f"Order {order_id} cannot be cancelled (status: {order.status})")
                return False
            
            # 更新状态
            order.status = OrderStatus.CANCELLED.value
            order.updated_at = datetime.now()
            if reason:
                order.notes = (order.notes or "") + f" | 取消原因: {reason}"
            
            # 更新数据库
            self._update_order_in_database(order)
            
            # 记录日志
            logger.info(f"❌ 订单取消: {order_id} | 原因: {reason or '用户请求'}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error cancelling order {order_id}: {e}")
            return False

    def modify_order(self, 
                    order_id: str,
                    new_price: Optional[float] = None,
                    new_quantity: Optional[int] = None) -> bool:
        """修改订单"""
        try:
            if order_id not in self.orders:
                logger.warning(f"Order {order_id} not found for modification")
                return False
            
            order = self.orders[order_id]
            
            # 检查订单是否可以修改
            if order.status not in [OrderStatus.SUBMITTED.value, OrderStatus.PENDING.value]:
                logger.warning(f"Order {order_id} cannot be modified (status: {order.status})")
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
            logger.info(f"✏️ 订单修改: {order_id} | 价格: {old_price} → {order.price}, 数量: {old_quantity} → {order.quantity}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error modifying order {order_id}: {e}")
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
                        exchange, sec_type, notes, commission
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order.order_id, order.symbol, order.action, order.quantity,
                    order.order_type, order.price, order.status,
                    order.created_at.isoformat(), order.updated_at.isoformat(),
                    order.executed_at.isoformat() if order.executed_at else None,
                    order.fill_price, order.order_source, order.strategy_name,
                    order.rule_id, order.parent_order_id, order.exchange,
                    order.sec_type, order.notes, order.commission
                ))
                conn.commit()
                
        except Exception as e:
            logger.error(f"Error saving order {order.order_id} to database: {e}")
            raise

    def _update_order_in_database(self, order: Order):
        """更新数据库中的订单"""
        try:
            with sqlite3.connect(self.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE orders SET
                        symbol = ?, action = ?, quantity = ?, order_type = ?, price = ?,
                        status = ?, updated_at = ?, executed_at = ?, fill_price = ?,
                        order_source = ?, strategy_name = ?, rule_id = ?, parent_order_id = ?,
                        exchange = ?, sec_type = ?, notes = ?, commission = ?
                    WHERE order_id = ?
                """, (
                    order.symbol, order.action, order.quantity, order.order_type, order.price,
                    order.status, order.updated_at.isoformat(),
                    order.executed_at.isoformat() if order.executed_at else None,
                    order.fill_price, order.order_source, order.strategy_name,
                    order.rule_id, order.parent_order_id, order.exchange,
                    order.sec_type, order.notes, order.commission, order.order_id
                ))
                conn.commit()
                
        except Exception as e:
            logger.error(f"Error updating order {order.order_id} in database: {e}")
            raise

    def get_order_statistics(self) -> Dict[str, Any]:
        """获取订单统计信息"""
        total_orders = len(self.orders)
        
        status_counts = {}
        source_counts = {}
        symbol_counts = {}
        
        for order in self.orders.values():
            # 状态统计
            status_counts[order.status] = status_counts.get(order.status, 0) + 1
            
            # 来源统计
            source_counts[order.order_source] = source_counts.get(order.order_source, 0) + 1
            
            # 标的统计
            symbol_counts[order.symbol] = symbol_counts.get(order.symbol, 0) + 1
        
        return {
            "total_orders": total_orders,
            "status_distribution": status_counts,
            "source_distribution": source_counts,
            "symbol_distribution": symbol_counts,
            "active_orders": len(self.get_active_orders())
        }

    def cleanup_old_orders(self, days: int = 30):
        """清理超过指定天数的已完成订单"""
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            orders_to_remove = []
            for order_id, order in self.orders.items():
                if (order.status in [OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value] 
                    and order.updated_at < cutoff_date):
                    orders_to_remove.append(order_id)
            
            # 从内存中移除
            for order_id in orders_to_remove:
                del self.orders[order_id]
            
            # 从数据库中删除
            if orders_to_remove:
                with sqlite3.connect(self.db_manager.db_path) as conn:
                    cursor = conn.cursor()
                    placeholders = ','.join(['?'] * len(orders_to_remove))
                    cursor.execute(f"""
                        DELETE FROM orders 
                        WHERE order_id IN ({placeholders})
                        AND status IN ('Filled', 'Cancelled', 'Rejected')
                        AND updated_at < ?
                    """, orders_to_remove + [cutoff_date.isoformat()])
                    conn.commit()
                
                logger.info(f"Cleaned up {len(orders_to_remove)} old orders")
                
        except Exception as e:
            logger.error(f"Error cleaning up old orders: {e}")