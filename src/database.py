"""
SQLite数据库模块
用于替代CSV和JSON文件存储，提供统一的数据访问接口
"""

import sqlite3
import json
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path
from dataclasses import dataclass, asdict
from loguru import logger

# 为数据库模块绑定日志记录器
database_logger = logger.bind(module="database", request_id="-")


@dataclass
class Order:
    """订单数据类（由原 AutoOrder 重命名）"""
    order_id: str
    symbol: str
    quantity: int
    action: str
    order_type: str
    price: Optional[float]
    timestamp: str
    exchange: str = "SMART"
    sec_type: str = "STK"
    status: str = "pending"
    executed_at: Optional[str] = None
    rule_id: Optional[str] = None
    order_source: str = "auto"

# 兼容旧代码：保留 AutoOrder 名称
AutoOrder = Order


@dataclass
class RiskRule:
    """风险规则数据类"""
    symbol: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_loss_percent: Optional[float] = None
    enabled: bool = True
    timestamp: str = None


@dataclass
class Strategy:
    """策略数据类"""
    name: str
    strategy_type: str
    enabled: bool = True
    api_endpoint: Optional[str] = None
    file_path: Optional[str] = None
    description: Optional[str] = None
    created_at: str = None
    updated_at: str = None


@dataclass
class StrategyExecution:
    """策略执行数据类"""
    name: str
    symbol: str
    action: str
    quantity: int
    price: Optional[float]
    timestamp: str
    order_id: Optional[str] = None
    profit: Optional[float] = None
    status: str = "pending"


class DatabaseManager:
    """数据库管理器"""
    
    def __init__(self, db_path: str = "data/trading_bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)
        self._init_database()
        database_logger.info(f"Database initialized at: {self.db_path}")
    
    def _init_database(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 创建订单表（替代原 auto_orders）——与 OrderManager 保持一致
            cursor.execute(
                """
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
                """
            )
            
            # 兼容老库：缺失 is_deleted 列则补齐
            cursor.execute("PRAGMA table_info('orders')")
            cols = [row[1] for row in cursor.fetchall()]
            if 'is_deleted' not in cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN is_deleted INTEGER DEFAULT 0")
                database_logger.info("Added is_deleted column to orders table")
            # 兼容旧库：缺失 created_at/updated_at 列则补齐并回填
            if 'created_at' not in cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN created_at TEXT")
                if 'timestamp' in cols:
                    cursor.execute("UPDATE orders SET created_at = COALESCE(timestamp, datetime('now'))")
                else:
                    cursor.execute("UPDATE orders SET created_at = datetime('now')")
                database_logger.info("Added created_at column to orders table and backfilled")
            if 'updated_at' not in cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN updated_at TEXT")
                cursor.execute("UPDATE orders SET updated_at = COALESCE(updated_at, created_at)")
                database_logger.info("Added updated_at column to orders table and backfilled")

            # 如存在旧表 auto_orders，则迁移数据至 orders（当 orders 为空时）
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_orders'")
            if cursor.fetchone():
                cursor.execute("SELECT COUNT(*) FROM orders")
                orders_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM auto_orders")
                legacy_count = cursor.fetchone()[0]
                if legacy_count > 0:
                    if orders_count == 0:
                        cursor.execute(
                            """
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
                            """
                        )
                        database_logger.info("Migrated legacy auto_orders data into orders table")
                    else:
                        database_logger.warning("orders table already has data; skipped migrating from auto_orders")

            # 创建风险规则表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT UNIQUE NOT NULL,
                    stop_loss_price REAL,
                    take_profit_price REAL,
                    max_loss_percent REAL,
                    enabled BOOLEAN DEFAULT 1,
                    timestamp TEXT NOT NULL
                )
                """
            )
            # 迁移：为已有表添加 enabled 列
            try:
                cursor.execute("SELECT enabled FROM risk_rules LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute("ALTER TABLE risk_rules ADD COLUMN enabled BOOLEAN DEFAULT 1")
                database_logger.info("Added enabled column to risk_rules table")
            
            # 创建策略表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    strategy_type TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    api_endpoint TEXT,
                    file_path TEXT,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            
            # 创建策略执行表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL,
                    timestamp TEXT NOT NULL,
                    order_id TEXT,
                    profit REAL,
                    status TEXT DEFAULT 'pending'
                )
            """)
            
            # 创建历史数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS historical_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    bar_size TEXT NOT NULL,
                    UNIQUE(symbol, timestamp, bar_size)
                )
            """)
            
            # 创建策略设置表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            
            conn.commit()

    # 自动订单相关方法 -> 重命名为订单相关方法（保留兼容包装）
    def save_order(self, order: Order):
        """保存订单（新）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            created = order.timestamp or datetime.now().isoformat()
            status = order.status or 'Submitted'
            cursor.execute(
                """
                INSERT INTO orders 
                (order_id, symbol, action, quantity, order_type, price, status, created_at, updated_at, executed_at, order_source, rule_id, exchange, sec_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id, order.symbol, order.action, order.quantity,
                    order.order_type, order.price, status, created, created, order.executed_at,
                    order.order_source, order.rule_id, order.exchange, order.sec_type,
                ),
            )
            conn.commit()
            database_logger.debug(f"Order saved: {order.order_id}")


    def load_orders_legacy(self) -> List[Order]:
        """[兼容] 旧实现，已弃用，请使用 load_orders"""
        database_logger.warning("load_orders_legacy() is deprecated; use load_orders()")
        return self.load_orders()

    def save_auto_order(self, order: AutoOrder):
        """[兼容] 保存自动订单，已弃用，请使用 save_order"""
        database_logger.warning("save_auto_order() is deprecated; use save_order()")
        return self.save_order(order)  # type: ignore[arg-type]

    def update_order(self, order_id: str, status: str = None, price: float = None, executed_at: str = None):
        """更新订单状态和价格（新）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            update_fields = []
            values = []
            
            if status is not None:
                update_fields.append("status = ?")
                values.append(status)
            if price is not None:
                update_fields.append("price = ?")
                values.append(price)
            if executed_at is not None:
                update_fields.append("executed_at = ?")
                values.append(executed_at)
            
            if update_fields:
                # 总是更新 updated_at
                update_fields.append("updated_at = ?")
                values.append(datetime.now().isoformat())
                values.append(order_id)
                update_sql = f"UPDATE orders SET {', '.join(update_fields)} WHERE order_id = ?"
                cursor.execute(update_sql, values)
                conn.commit()
                database_logger.debug(
                    f"Order {order_id} updated: {dict(zip([f.split(' = ')[0] for f in update_fields], values[:-1]))}"
                )

    def update_auto_order(self, order_id: str, status: str = None, price: float = None, executed_at: str = None):
        """[兼容] 更新自动订单，已弃用，请使用 update_order"""
        database_logger.warning("update_auto_order() is deprecated; use update_order()")
        return self.update_order(order_id, status=status, price=price, executed_at=executed_at)

    def load_orders(self) -> List[Order]:
        """加载所有订单（新）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT order_id, symbol, quantity, action, order_type, price, 
                       created_at as timestamp, exchange, sec_type, 
                       COALESCE(status, 'Submitted') as status,
                       executed_at,
                       rule_id,
                       COALESCE(order_source, 'auto') as order_source
                FROM orders ORDER BY created_at DESC
                """
            )
            rows = cursor.fetchall()
            orders: List[Order] = []
            for row in rows:
                orders.append(Order(*row))
            database_logger.debug(f"Loaded {len(orders)} orders")
            return orders

    def load_auto_orders(self) -> List[AutoOrder]:
        """[兼容] 加载所有自动订单，已弃用，请使用 load_orders"""
        database_logger.warning("load_auto_orders() is deprecated; use load_orders()")
        return self.load_orders()  # type: ignore[return-value]

    def load_risk_rules(self) -> List[RiskRule]:
        """加载所有风险规则"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, stop_loss_price, take_profit_price, max_loss_percent, COALESCE(enabled, 1) as enabled, timestamp FROM risk_rules")
            rows = cursor.fetchall()
            
            rules = []
            for row in rows:
                # row: (symbol, sl, tp, max_loss_percent, enabled, timestamp)
                rule = RiskRule(
                    symbol=row[0],
                    stop_loss_price=row[1],
                    take_profit_price=row[2],
                    max_loss_percent=row[3],
                    enabled=bool(row[4]),
                    timestamp=row[5]
                )
                rules.append(rule)
            
            database_logger.debug(f"Loaded {len(rules)} risk rules")
            return rules

    def save_risk_rule(self, rule: RiskRule):
        """保存风险规则（按 symbol 幂等：存在则更新，不存在则插入）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            ts = rule.timestamp or datetime.now().isoformat()
            enabled_val = 1 if getattr(rule, 'enabled', True) else 0
            cursor.execute(
                """
                INSERT INTO risk_rules (symbol, stop_loss_price, take_profit_price, max_loss_percent, enabled, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    stop_loss_price=excluded.stop_loss_price,
                    take_profit_price=excluded.take_profit_price,
                    max_loss_percent=excluded.max_loss_percent,
                    enabled=excluded.enabled,
                    timestamp=excluded.timestamp
                """,
                (
                    rule.symbol,
                    rule.stop_loss_price,
                    rule.take_profit_price,
                    rule.max_loss_percent,
                    enabled_val,
                    ts,
                ),
            )
            conn.commit()
            database_logger.debug(f"Risk rule saved: {rule.symbol}")
    
    def delete_risk_rule(self, symbol: str):
        """删除风险规则"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM risk_rules WHERE symbol = ?", (symbol,))
            conn.commit()
            database_logger.debug(f"Risk rule deleted for symbol: {symbol}")
    
    # 策略相关方法
    def save_strategy(self, strategy: Strategy):
        """保存策略"""
        now = datetime.now().isoformat()
        if not strategy.created_at:
            strategy.created_at = now
        strategy.updated_at = now
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO strategies 
                (name, strategy_type, enabled, api_endpoint, file_path, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                strategy.name, strategy.strategy_type, strategy.enabled,
                strategy.api_endpoint, strategy.file_path, strategy.description,
                strategy.created_at, strategy.updated_at
            ))
            conn.commit()
            database_logger.debug(f"Strategy saved: {strategy.name}")
    
    def load_strategies(self) -> List[Strategy]:
        """加载所有策略"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, strategy_type, enabled, api_endpoint, file_path, description, created_at, updated_at FROM strategies")
            rows = cursor.fetchall()
            
            strategies = []
            for row in rows:
                strategy = Strategy(*row)
                strategies.append(strategy)
            
            database_logger.debug(f"Loaded {len(strategies)} strategies")
            return strategies
    
    def get_strategy(self, name: str) -> Optional[Strategy]:
        """获取指定策略"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, strategy_type, enabled, api_endpoint, file_path, description, created_at, updated_at FROM strategies WHERE name = ?", (name,))
            row = cursor.fetchone()
            
            if row:
                return Strategy(*row)
            return None
    
    def delete_strategy(self, name: str):
        """删除策略"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM strategies WHERE name = ?", (name,))
            conn.commit()
            database_logger.debug(f"Strategy deleted: {name}")
    
    # 策略执行相关方法
    def save_strategy_execution(self, execution: StrategyExecution):
        """保存策略执行记录（按 order_id 幂等：存在则更新，不存在则插入）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # 如果带有 order_id，先检查是否已存在该记录
            if execution.order_id:
                cursor.execute("SELECT 1 FROM strategy_executions WHERE order_id = ?", (execution.order_id,))
                exists = cursor.fetchone() is not None
                if exists:
                    # 按需更新可变字段
                    update_fields = []
                    params = []
                    if execution.price is not None:
                        update_fields.append("price = ?")
                        params.append(execution.price)
                    if execution.profit is not None:
                        update_fields.append("profit = ?")
                        params.append(execution.profit)
                    if execution.status is not None:
                        update_fields.append("status = ?")
                        params.append(execution.status)

                    # 如果没有需要更新的字段，则直接返回
                    if not update_fields:
                        database_logger.debug(f"Strategy execution exists (no changes) for: {execution.name}")
                        return

                    params.append(execution.order_id)
                    cursor.execute(f"""
                        UPDATE strategy_executions
                        SET {', '.join(update_fields)}
                        WHERE order_id = ?
                    """, params)
                    conn.commit()
                    database_logger.debug(f"Strategy execution updated for: {execution.name}")
                    return

            # 默认插入
            cursor.execute("""
                INSERT INTO strategy_executions 
                (name, symbol, action, quantity, price, timestamp, order_id, profit, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution.name, execution.symbol, execution.action, execution.quantity,
                execution.price, execution.timestamp, execution.order_id, execution.profit, execution.status
            ))
            conn.commit()
            database_logger.debug(f"Strategy execution saved for: {execution.name}")
    
    def update_strategy_execution(self, order_id: str, price: Optional[float] = None, profit: Optional[float] = None, status: Optional[str] = None):
        """更新策略执行记录"""
        if not order_id:
            database_logger.warning("Cannot update strategy execution without order_id")
            return False
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 构建动态更新语句
            update_fields = []
            params = []
            
            if price is not None:
                update_fields.append("price = ?")
                params.append(price)
                
            if profit is not None:
                update_fields.append("profit = ?")
                params.append(profit)
                
            if status is not None:
                update_fields.append("status = ?")
                params.append(status)
                
            if not update_fields:
                database_logger.warning("No fields to update for strategy execution")
                return False
                
            params.append(order_id)
            
            cursor.execute(f"""
                UPDATE strategy_executions 
                SET {', '.join(update_fields)}
                WHERE order_id = ?
            """, params)
            
            updated_rows = cursor.rowcount
            conn.commit()
            
            if updated_rows > 0:
                database_logger.debug(f"Strategy execution updated for order_id: {order_id}")
                return True
            else:
                database_logger.warning(f"No strategy execution found with order_id: {order_id}")
                return False
    
    def load_strategy_executions(self, strategy_name: Optional[str] = None, limit: int = 100) -> List[StrategyExecution]:
        """加载策略执行记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if strategy_name:
                cursor.execute("""
                    SELECT name, symbol, action, quantity, price, timestamp, order_id, profit, status 
                    FROM strategy_executions 
                    WHERE name = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (strategy_name, limit))
            else:
                cursor.execute("""
                    SELECT name, symbol, action, quantity, price, timestamp, order_id, profit, status 
                    FROM strategy_executions 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (limit,))
            
            rows = cursor.fetchall()
            
            executions = []
            for row in rows:
                execution = StrategyExecution(*row)
                executions.append(execution)
            
            database_logger.debug(f"Loaded {len(executions)} strategy executions")
            return executions

    def delete_strategy_executions(self, strategy_name: str) -> int:
        """按策略名删除所有执行记录，返回删除的行数"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM strategy_executions WHERE name = ?", (strategy_name,))
                deleted = cursor.rowcount
                conn.commit()
                database_logger.info(f"Deleted {deleted} executions for strategy: {strategy_name}")
                return deleted or 0
        except Exception as e:
            database_logger.error(f"Failed to delete executions for {strategy_name}: {e}")
            return 0
    
    # 历史数据相关方法
    def save_historical_data(self, symbol: str, data: pd.DataFrame, bar_size: str = "1 day"):
        """保存历史数据"""
        with sqlite3.connect(self.db_path) as conn:
            # 重置索引确保timestamp列存在
            if 'timestamp' not in data.columns and data.index.name == 'timestamp':
                data = data.reset_index()
            
            # 添加symbol和bar_size列
            data['symbol'] = symbol
            data['bar_size'] = bar_size
            
            # 确保列名正确
            required_columns = ['symbol', 'timestamp', 'open', 'high', 'low', 'close', 'volume', 'bar_size']
            if not all(col in data.columns for col in required_columns):
                database_logger.error(f"Historical data missing required columns: {required_columns}")
                return
            
            # 使用INSERT OR IGNORE避免重复数据
            data_to_insert = data[required_columns].copy()
            data_to_insert.to_sql('historical_data', conn, if_exists='append', index=False, method='multi')
            
            # 手动处理UNIQUE约束冲突
            cursor = conn.cursor()
            for _, row in data_to_insert.iterrows():
                cursor.execute("""
                    INSERT OR IGNORE INTO historical_data 
                    (symbol, timestamp, open, high, low, close, volume, bar_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row['symbol'], row['timestamp'], row['open'], row['high'],
                    row['low'], row['close'], row['volume'], row['bar_size']
                ))
            
            conn.commit()
            database_logger.debug(f"Historical data saved for {symbol}: {len(data)} records")
    
    def load_historical_data(self, symbol: str, bar_size: str = "1 day") -> pd.DataFrame:
        """加载历史数据"""
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT timestamp, open, high, low, close, volume 
                FROM historical_data 
                WHERE symbol = ? AND bar_size = ? 
                ORDER BY timestamp
            """
            df = pd.read_sql_query(query, conn, params=(symbol, bar_size))
            
            if not df.empty:
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                df.set_index('timestamp', inplace=True)
            
            database_logger.debug(f"Loaded historical data for {symbol}: {len(df)} records")
            return df
    
    # 策略设置相关方法
    def save_strategy_settings(self, settings: Dict[str, Any]):
        """保存策略设置"""
        now = datetime.now().isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            for key, value in settings.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO strategy_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, (key, json.dumps(value), now))
            
            conn.commit()
            database_logger.debug(f"Strategy settings saved: {list(settings.keys())}")
    
    def load_strategy_settings(self) -> Dict[str, Any]:
        """加载策略设置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM strategy_settings")
            rows = cursor.fetchall()
            
            settings = {}
            for key, value in rows:
                try:
                    settings[key] = json.loads(value)
                except json.JSONDecodeError:
                    settings[key] = value
            
            database_logger.debug(f"Loaded strategy settings: {list(settings.keys())}")
            return settings
    
    # 数据迁移方法
    def migrate_from_csv_json(self, data_dir: str = "data"):
        """从CSV和JSON文件迁移数据到SQLite"""
        data_path = Path(data_dir)
        if not data_path.exists():
            database_logger.warning(f"Data directory not found: {data_dir}")
            return
        
        database_logger.info("Starting data migration from CSV/JSON to SQLite...")
        
        # 迁移订单数据
        orders_file = data_path / "orders.csv"
        if orders_file.exists():
            self._migrate_orders_csv(orders_file)
        
        # 迁移风险规则
        risk_rules_file = data_path / "risk_rules.csv"
        if risk_rules_file.exists():
            self._migrate_risk_rules_csv(risk_rules_file)
        
        # 迁移策略数据
        strategies_file = data_path / "strategies.csv"
        if strategies_file.exists():
            self._migrate_strategies_csv(strategies_file)
        
        # 迁移策略执行数据
        executions_file = data_path / "strategy_executions.csv"
        if executions_file.exists():
            self._migrate_strategy_executions_csv(executions_file)
        
        # 迁移策略设置
        settings_file = data_path / "strategy_settings.json"
        if settings_file.exists():
            self._migrate_strategy_settings_json(settings_file)
        
        # 迁移历史数据
        for csv_file in data_path.glob("*_historical.csv"):
            symbol = csv_file.name.replace("_historical.csv", "")
            self._migrate_historical_csv(csv_file, symbol)
        
        database_logger.info("Data migration completed successfully!")
    
    def _migrate_orders_csv(self, file_path: Path):
        """迁移订单CSV文件"""
        try:
            df = pd.read_csv(file_path)
            for _, row in df.iterrows():
                order = Order(
                    order_id=str(row.get('order_id', '')),
                    symbol=row.get('symbol', ''),
                    quantity=int(row.get('quantity', 0)),
                    action=row.get('action', ''),
                    order_type=row.get('order_type', ''),
                    price=row.get('price'),
                    timestamp=row.get('timestamp', datetime.now().isoformat()),
                    exchange=row.get('exchange', 'SMART'),
                    sec_type=row.get('sec_type', 'STK')
                )
                self.save_order(order)
            database_logger.info(f"Migrated {len(df)} orders from {file_path}")
        except Exception as e:
            database_logger.error(f"Failed to migrate orders from {file_path}: {e}")
    
    def _migrate_risk_rules_csv(self, file_path: Path):
        """迁移风险规则CSV文件"""
        try:
            df = pd.read_csv(file_path)
            for _, row in df.iterrows():
                rule = RiskRule(
                    symbol=row.get('symbol', ''),
                    stop_loss_price=row.get('stop_loss_price'),
                    take_profit_price=row.get('take_profit_price'),
                    max_loss_percent=row.get('max_loss_percent'),
                    timestamp=row.get('timestamp', datetime.now().isoformat())
                )
                self.save_risk_rule(rule)
            database_logger.info(f"Migrated {len(df)} risk rules from {file_path}")
        except Exception as e:
            database_logger.error(f"Failed to migrate risk rules from {file_path}: {e}")
    
    def _migrate_strategies_csv(self, file_path: Path):
        """迁移策略CSV文件"""
        try:
            df = pd.read_csv(file_path)
            for _, row in df.iterrows():
                strategy = Strategy(
                    name=row.get('name', ''),
                    strategy_type=row.get('strategy_type', ''),
                    enabled=bool(row.get('enabled', True)),
                    api_endpoint=row.get('api_endpoint'),
                    file_path=row.get('file_path'),
                    description=row.get('description'),
                    created_at=row.get('created_at', datetime.now().isoformat()),
                    updated_at=row.get('updated_at', datetime.now().isoformat())
                )
                self.save_strategy(strategy)
            database_logger.info(f"Migrated {len(df)} strategies from {file_path}")
        except Exception as e:
            database_logger.error(f"Failed to migrate strategies from {file_path}: {e}")
    
    def _migrate_strategy_executions_csv(self, file_path: Path):
        """迁移策略执行CSV文件"""
        try:
            df = pd.read_csv(file_path)
            for _, row in df.iterrows():
                execution = StrategyExecution(
                    name=row.get('name', ''),
                    symbol=row.get('symbol', ''),
                    action=row.get('action', ''),
                    quantity=int(row.get('quantity', 0)),
                    price=row.get('price'),
                    timestamp=row.get('timestamp', datetime.now().isoformat()),
                    order_id=row.get('order_id'),
                    profit=row.get('profit'),
                    status=row.get('status', 'pending')
                )
                self.save_strategy_execution(execution)
            database_logger.info(f"Migrated {len(df)} strategy executions from {file_path}")
        except Exception as e:
            database_logger.error(f"Failed to migrate strategy executions from {file_path}: {e}")
    
    def _migrate_strategy_settings_json(self, file_path: Path):
        """迁移策略设置JSON文件"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            self.save_strategy_settings(settings)
            database_logger.info(f"Migrated strategy settings from {file_path}")
        except Exception as e:
            database_logger.error(f"Failed to migrate strategy settings from {file_path}: {e}")
    
    def _migrate_historical_csv(self, file_path: Path, symbol: str):
        """迁移历史数据CSV文件"""
        try:
            df = pd.read_csv(file_path)
            
            # 确保列名正确
            required_columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            if not all(col in df.columns for col in required_columns):
                # 尝试常见的列名映射
                column_mapping = {
                    'date': 'timestamp',
                    'datetime': 'timestamp',
                    'time': 'timestamp',
                    'Open': 'open',
                    'High': 'high',
                    'Low': 'low',
                    'Close': 'close',
                    'Volume': 'volume'
                }
                df = df.rename(columns=column_mapping)
            
            if all(col in df.columns for col in required_columns):
                self.save_historical_data(symbol, df)
                database_logger.info(f"Migrated {len(df)} historical records for {symbol} from {file_path}")
            else:
                database_logger.warning(f"Historical data file {file_path} missing required columns")
        except Exception as e:
            database_logger.error(f"Failed to migrate historical data from {file_path}: {e}")


# 全局数据库实例
db_manager = None

def get_db_manager(db_path: str = "data/trading_bot.db") -> DatabaseManager:
    """获取数据库管理器单例"""
    global db_manager
    if db_manager is None:
        db_manager = DatabaseManager(db_path)
    return db_manager