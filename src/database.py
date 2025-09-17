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

from src.utils import now_local, parse_local_datetime, to_local_iso

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
    commission: Optional[float] = None
    pnl: Optional[float] = None

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
    max_time_span: Optional[int] = None  # 以分钟为单位的最大持仓时间
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

def _normalise_iso_timestamp(value: Optional[Any]) -> Optional[str]:
    """Convert values to ISO strings in the local timezone."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_local_iso(value)
    if isinstance(value, str):
        dt = parse_local_datetime(value)
        return to_local_iso(dt) if dt else value
    return str(value)


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
                    pnl REAL,
                    is_deleted INTEGER DEFAULT 0
                )
                """
            )

            # 兼容旧库：补齐缺失列
            cursor.execute("PRAGMA table_info('orders')")
            existing_columns = {row[1] for row in cursor.fetchall()}
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
                if column_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {column_def}")
                    database_logger.info(f"Added missing column '{column_name}' to orders table")
            conn.commit()
            

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
                    max_time_span INTEGER,
                    timestamp TEXT NOT NULL
                )
                """
            )

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
            # 创建历史数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS historical_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    time TEXT,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    interval TEXT NOT NULL,
                    adj_close REAL,
                    UNIQUE(symbol, date, time, interval)
                )
            """)
            
            # 检查是否需要迁移旧数据和添加新字段
            cursor.execute("PRAGMA table_info('historical_data')")
            cols = [row[1] for row in cursor.fetchall()]
            
            # 如果没有date字段，表示需要迁移
            if 'date' not in cols:
                # 添加新字段
                for field in ['date', 'time', 'interval', 'adj_close']:
                    if field not in cols:
                        field_type = 'TEXT' if field in ['date', 'time', 'interval'] else 'REAL'
                        cursor.execute(f"ALTER TABLE historical_data ADD COLUMN {field} {field_type}")
                
                # 迁移现有数据：从timestamp提取date，设置默认interval
                cursor.execute("""
                    UPDATE historical_data 
                    SET date = date(timestamp),
                        time = CASE 
                            WHEN bar_size LIKE '%min%' OR bar_size LIKE '%hour%' THEN time(timestamp)
                            ELSE NULL 
                        END,
                        interval = COALESCE(bar_size, '1d'),
                        adj_close = close
                    WHERE date IS NULL
                """)
                database_logger.info("Migrated existing historical_data to new schema")
            
            # 创建配置表（用于存储系统配置参数）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key TEXT UNIQUE NOT NULL,
                    config_value TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            
            # 创建仓位历史表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sizing_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    suggested_quantity INTEGER NOT NULL,
                    current_price REAL,
                    account_value REAL,
                    position_size REAL,
                    risk_percent REAL,
                    sizing_method TEXT NOT NULL,
                    calculation_details TEXT,
                    g_rules_checked TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            
            # 添加g_rules_checked字段（如果不存在）
            try:
                cursor.execute("ALTER TABLE sizing_history ADD COLUMN g_rules_checked TEXT")
            except sqlite3.OperationalError:
                # 字段已存在，忽略错误
                pass
            
            # 创建通知历史表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            
            conn.commit()

    # 自动订单相关方法 -> 重命名为订单相关方法（保留兼容包装）
    def save_order(self, order: Order):
        """保存订单（新）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            created = _normalise_iso_timestamp(order.timestamp) or now_local().isoformat()
            status = order.status or 'Submitted'
            cursor.execute(
                """
                INSERT INTO orders 
                (order_id, symbol, action, quantity, order_type, price, status, created_at, updated_at, executed_at, order_source, rule_id, exchange, sec_type, commission, pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id, order.symbol, order.action, order.quantity,
                    order.order_type, order.price, status, created, created, _normalise_iso_timestamp(order.executed_at),
                    order.order_source, order.rule_id, order.exchange, order.sec_type,
                    getattr(order, 'commission', None), getattr(order, 'pnl', None),
                ),
            )
            conn.commit()
            database_logger.debug(f"Order saved: {order.order_id}")


    def update_order(self, order_id: str, status: str = None, price: float = None, executed_at: str = None, commission: float = None, pnl: float = None):
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
                values.append(_normalise_iso_timestamp(executed_at))
            if commission is not None:
                update_fields.append("commission = ?")
                values.append(commission)
                database_logger.debug(f"Adding commission update: {commission}")
            if pnl is not None:
                update_fields.append("pnl = ?")
                values.append(pnl)
                database_logger.debug(f"Adding pnl update: {pnl}")
            
            if update_fields:
                # 总是更新 updated_at
                update_fields.append("updated_at = ?")
                values.append(now_local().isoformat())
                values.append(order_id)
                update_sql = f"UPDATE orders SET {', '.join(update_fields)} WHERE order_id = ?"
                database_logger.debug(f"Executing SQL: {update_sql} with values: {values}")
                cursor.execute(update_sql, values)
                conn.commit()
                database_logger.debug(f"SQL executed successfully, rows affected: {cursor.rowcount}")
                database_logger.debug(
                    f"Order {order_id} updated: {dict(zip([f.split(' = ')[0] for f in update_fields], values[:-1]))}"
                )
    
    async def update_order_commission_pnl(self, order_id: str, commission: float, pnl: float):
        """异步更新订单的佣金和PnL"""
        try:
            self.update_order(order_id, commission=commission, pnl=pnl)
            database_logger.info(f"Updated order {order_id} commission={commission}, pnl={pnl}")
        except Exception as e:
            database_logger.error(f"Error updating order {order_id} commission/pnl: {e}")
            raise

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
                       COALESCE(order_source, 'auto') as order_source,
                       commission,
                       pnl
                FROM orders ORDER BY created_at DESC
                """
            )
            rows = cursor.fetchall()
            orders: List[Order] = []
            for row in rows:
                orders.append(Order(*row))
            database_logger.debug(f"Loaded {len(orders)} orders")
            return orders

    def load_risk_rules(self) -> List[RiskRule]:
        """加载所有风险规则"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, stop_loss_price, take_profit_price, max_loss_percent, COALESCE(enabled, 1) as enabled, max_time_span, timestamp FROM risk_rules")
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
                    max_time_span=row[5],
                    timestamp=row[6]
                )
                rules.append(rule)
            
            database_logger.debug(f"Loaded {len(rules)} risk rules")
            return rules

    def save_risk_rule(self, rule: RiskRule):
        """保存风险规则（按 symbol 幂等：存在则更新，不存在则插入）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            ts = _normalise_iso_timestamp(rule.timestamp) or now_local().isoformat()
            enabled_val = 1 if getattr(rule, 'enabled', True) else 0
            cursor.execute(
                """
                INSERT INTO risk_rules (symbol, stop_loss_price, take_profit_price, max_loss_percent, enabled, max_time_span, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    stop_loss_price=excluded.stop_loss_price,
                    take_profit_price=excluded.take_profit_price,
                    max_loss_percent=excluded.max_loss_percent,
                    enabled=excluded.enabled,
                    max_time_span=excluded.max_time_span,
                    timestamp=excluded.timestamp
                """,
                (
                    rule.symbol,
                    rule.stop_loss_price,
                    rule.take_profit_price,
                    rule.max_loss_percent,
                    enabled_val,
                    rule.max_time_span,
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
        now = now_local().isoformat()
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

    def update_orders_strategy_name(self, old_name: str, new_name: str) -> int:
        """将orders表中所有旧策略名迁移为新策略名，返回受影响的行数"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE orders SET strategy_name = ? WHERE strategy_name = ?",
                (new_name, old_name),
            )
            conn.commit()
            affected = cursor.rowcount if hasattr(cursor, 'rowcount') else 0
            database_logger.debug(
                f"Orders strategy_name updated: {old_name} -> {new_name}, rows={affected}"
            )
            return affected
    
    # 策略执行表已移除；保留占位符以保持向后兼容接口
    def load_strategy_orders(self, strategy_name: str, order_source: str = 'strategy', limit: int = 100) -> List[Order]:
        """加载指定策略的订单记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT order_id, symbol, quantity, action, order_type, price,
                       created_at as timestamp, exchange, sec_type,
                       COALESCE(status, 'Submitted') as status,
                       executed_at,
                       rule_id,
                       COALESCE(order_source, 'auto') as order_source,
                       commission,
                       pnl
                FROM orders
                WHERE strategy_name = ? AND order_source = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (strategy_name, order_source, limit),
            )
            rows = cursor.fetchall()
            orders: List[Order] = []
            for row in rows:
                orders.append(Order(*row))
            database_logger.debug(f"Loaded {len(orders)} orders for strategy {strategy_name}")
            return orders

    def delete_strategy_orders(self, strategy_name: str, order_source: str = 'strategy') -> int:
        """删除指定策略的订单记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM orders WHERE strategy_name = ? AND order_source = ?",
                (strategy_name, order_source),
            )
            deleted = cursor.rowcount
            conn.commit()
            database_logger.info(f"Deleted {deleted} orders for strategy: {strategy_name}")
            return deleted
    
    # 历史数据相关方法
    def save_historical_data(self, symbol: str, data: pd.DataFrame, bar_size: str = "1 day"):
        """保存历史数据"""
        with sqlite3.connect(self.db_path) as conn:
            # 重置索引确保存在 date/time 或 timestamp
            if 'date' not in data.columns and 'timestamp' in data.columns:
                try:
                    data['date'] = pd.to_datetime(data['timestamp']).dt.date.astype(str)
                    data['time'] = pd.to_datetime(data['timestamp']).dt.time.astype(str)
                except Exception:
                    data = data.reset_index()
            elif 'date' in data.columns and 'time' not in data.columns and 'timestamp' in data.columns:
                try:
                    data['time'] = pd.to_datetime(data['timestamp']).dt.time.astype(str)
                except Exception:
                    pass
            elif 'date' not in data.columns and 'timestamp' not in data.columns and data.index.name in ('date', 'timestamp'):
                data = data.reset_index()
                if 'date' in data.columns and 'time' not in data.columns:
                    data['time'] = None
            
            # 规范列：将 bar_size 映射为 interval（如 '1 day' -> '1d'，'1 min' -> '1m' 等）
            interval = self._normalize_interval(bar_size)
            
            # 确保date字段只包含日期，time字段只包含时间
            if 'date' in data.columns:
                try:
                    # 如果date字段包含时间信息，分离出日期和时间
                    dt_series = pd.to_datetime(data['date'])
                    data['date'] = dt_series.dt.date.astype(str)
                    if 'time' not in data.columns or data['time'].isna().all():
                        data['time'] = dt_series.dt.time.astype(str)
                        # 如果是日级数据（没有具体时间），time设为None
                        if interval.endswith('d'):
                            data['time'] = None
                except Exception:
                    # 如果转换失败，保持原样
                    pass
            data['symbol'] = symbol
            data['interval'] = interval
            if 'adj_close' not in data.columns:
                data['adj_close'] = data.get('close')

            # 兼容来源列名
            rename_map = {}
            if 'Date' in data.columns:
                rename_map['Date'] = 'date'
            if 'Open' in data.columns:
                rename_map['Open'] = 'open'
            if 'High' in data.columns:
                rename_map['High'] = 'high'
            if 'Low' in data.columns:
                rename_map['Low'] = 'low'
            if 'Close' in data.columns:
                rename_map['Close'] = 'close'
            if 'Volume' in data.columns:
                rename_map['Volume'] = 'volume'
            data = data.rename(columns=rename_map)

            # 若仅有 'date' 字段而非 'time'，则置空 time
            if 'time' not in data.columns:
                data['time'] = None

            required_columns = ['symbol', 'date', 'time', 'open', 'high', 'low', 'close', 'volume', 'interval', 'adj_close']
            for col in required_columns:
                if col not in data.columns:
                    data[col] = None

            cols = required_columns
            cur = conn.cursor()
            cur.execute("PRAGMA table_info('historical_data')")
            table_cols = [row[1] for row in cur.fetchall()]
            missing = [c for c in cols if c not in table_cols]
            if missing:
                # 在极端情况下，按需添加缺失列
                for c in missing:
                    coltype = 'TEXT' if c in ['symbol','date','time','interval'] else 'REAL'
                    if c in ['volume']:
                        coltype = 'INTEGER'
                    cur.execute(f"ALTER TABLE historical_data ADD COLUMN {c} {coltype}")

            # 使用插入或忽略的方式写入，利用UNIQUE(symbol,date,time,interval)
            insert_sql = (
                "INSERT OR IGNORE INTO historical_data "
                "(symbol, date, time, open, high, low, close, volume, interval, adj_close) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            rows = [(
                row['symbol'], str(row['date']), (None if pd.isna(row['time']) else str(row['time'])),
                float(row['open']) if pd.notna(row['open']) else None,
                float(row['high']) if pd.notna(row['high']) else None,
                float(row['low']) if pd.notna(row['low']) else None,
                float(row['close']) if pd.notna(row['close']) else None,
                int(row['volume']) if pd.notna(row['volume']) else 0,
                row['interval'],
                float(row['adj_close']) if pd.notna(row['adj_close']) else None
            ) for _, row in data.iterrows()]
            cur.executemany(insert_sql, rows)
            conn.commit()
            database_logger.debug(f"Historical data saved for {symbol}: {len(rows)} records (interval={interval})")

    def _normalize_interval(self, bar_size: str) -> str:
        bs = (bar_size or '').lower().strip()
        if 'day' in bs:
            n = bs.split()[0]
            return f"{n}d" if n.isdigit() else '1d'
        if 'min' in bs:
            n = bs.split()[0]
            return f"{n}m" if n.replace('.', '').isdigit() else '1m'
        if 'hour' in bs:
            n = bs.split()[0]
            try:
                n = int(float(n) * 60)
            except Exception:
                n = 60
            return f"{n}m"
        return '1d'

    def query_historical_cache(self, symbol: str, interval: str, start_dt: Optional[str] = None, end_dt: Optional[str] = None) -> pd.DataFrame:
        """按interval查询缓存。interval: '1d'或'1m'等。start_dt/end_dt为ISO字符串。"""
        with sqlite3.connect(self.db_path) as conn:
            where = ["symbol = ?", "interval = ?"]
            params: list = [symbol, interval]
            if interval.endswith('d'):
                if start_dt:
                    where.append("date >= ?")
                    params.append(start_dt[:10])
                if end_dt:
                    where.append("date <= ?")
                    params.append(end_dt[:10])
                order_by = "date"
                select_cols = "date, open, high, low, close, volume, adj_close"
            else:
                # 分钟级，使用date+time
                if start_dt:
                    where.append("(date || 'T' || COALESCE(time,'')) >= ?")
                    params.append(start_dt[:19])
                if end_dt:
                    where.append("(date || 'T' || COALESCE(time,'')) <= ?")
                    params.append(end_dt[:19])
                order_by = "date, time"
                select_cols = "date, time, open, high, low, close, volume, adj_close"
            query = f"SELECT {select_cols} FROM historical_data WHERE {' AND '.join(where)} ORDER BY {order_by}"
            df = pd.read_sql_query(query, conn, params=params)
            return df

    def detect_missing_ranges(self, have_df: pd.DataFrame, start_dt: str, end_dt: str, interval: str) -> bool:
        """简单判断是否有数据缺口：为空或范围不足，则视为缺失。可后续细化。"""
        if have_df is None or have_df.empty:
            return True
        # 粗略检查首尾
        if interval.endswith('d'):
            have_start = pd.to_datetime(have_df['date'].iloc[0])
            have_end = pd.to_datetime(have_df['date'].iloc[-1])
        else:
            have_start = pd.to_datetime(have_df['date'].iloc[0] + 'T' + (have_df['time'].iloc[0] or '00:00:00'))
            have_end = pd.to_datetime(have_df['date'].iloc[-1] + 'T' + (have_df['time'].iloc[-1] or '23:59:59'))
        need_start = pd.to_datetime(start_dt)
        need_end = pd.to_datetime(end_dt)
        # 若需求范围不在已有范围内，则认为缺失
        return need_start < have_start or need_end > have_end
    
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
    # 旧的策略设置方法已删除，请使用 save_strategy_config 和 load_strategy_config

    def save_sizing_history(self, symbol: str, action: str, suggested_quantity: int, 
                           current_price: float = None, account_value: float = None,
                           position_size: float = None, risk_percent: float = None,
                           sizing_method: str = "default", calculation_details: str = None,
                           g_rules_checked: str = None):
        """保存仓位计算历史"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            timestamp = now_local().isoformat()
            
            cursor.execute(
                """
                INSERT INTO sizing_history 
                (symbol, action, suggested_quantity, current_price, account_value, 
                 position_size, risk_percent, sizing_method, calculation_details, g_rules_checked, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, action, suggested_quantity, current_price, account_value,
                 position_size, risk_percent, sizing_method, calculation_details, g_rules_checked, timestamp)
            )
            conn.commit()
            database_logger.debug(f"Sizing history saved for {symbol}: {suggested_quantity} shares")

    def load_sizing_history(self, symbol: str = None, since: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """加载仓位计算历史"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 构建查询条件
            conditions = []
            params = []
            
            if symbol:
                conditions.append("symbol = ?")
                params.append(symbol)
            
            if since:
                conditions.append("timestamp >= ?")
                params.append(since)
            
            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
            
            query = f"""
                SELECT symbol, action, suggested_quantity, current_price, account_value,
                       position_size, risk_percent, sizing_method, calculation_details, g_rules_checked, timestamp
                FROM sizing_history 
                {where_clause}
                ORDER BY timestamp DESC
                LIMIT ?
            """
            
            params.append(limit)
            cursor.execute(query, params)
            
            rows = cursor.fetchall()
            history = []
            for row in rows:
                # 解析g_rules_checked JSON字符串
                g_rules_checked = []
                if row[9]:  # g_rules_checked字段
                    try:
                        g_rules_checked = json.loads(row[9])
                    except (json.JSONDecodeError, TypeError):
                        g_rules_checked = []
                
                history.append({
                    'symbol': row[0],
                    'action': row[1],
                    'suggested_quantity': row[2],
                    'current_price': row[3],
                    'account_value': row[4],
                    'position_size': row[5],
                    'risk_percent': row[6],
                    'sizing_method': row[7],
                    'calculation_details': row[8],
                    'g_rules_checked': g_rules_checked,
                    'timestamp': row[10]
                })
            
            database_logger.debug(f"Loaded {len(history)} sizing history records")
            return history
    
    # 通知相关方法
    # 旧的通知设置方法已删除，请使用 save_notification_config 和 load_notification_config
    
    def save_notification_history(self, channel: str, event_type: str, message: str, 
                                 status: str = 'pending', error_message: str = None):
        """保存通知历史"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            now = now_local().isoformat()
            sent_at = now if status == 'sent' else None
            
            cursor.execute("""
                INSERT INTO notification_history 
                (channel, event_type, message, status, error_message, sent_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (channel, event_type, message, status, error_message, sent_at, now))
            
            conn.commit()
            database_logger.debug(f"Notification history saved: {channel} - {event_type}")
    
    def load_notification_history(self, channel: str = None, status: str = None, 
                                 since: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """加载通知历史"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 构建查询条件
            conditions = []
            params = []
            
            if channel:
                conditions.append("channel = ?")
                params.append(channel)
            
            if status:
                conditions.append("status = ?")
                params.append(status)
            
            if since:
                conditions.append("created_at >= ?")
                params.append(since)
            
            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
            
            query = f"""
                SELECT channel, event_type, message, status, retry_count, 
                       error_message, sent_at, created_at
                FROM notification_history 
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """
            
            params.append(limit)
            cursor.execute(query, params)
            
            rows = cursor.fetchall()
            
            history = []
            for row in rows:
                history.append({
                    'channel': row[0],
                    'event_type': row[1],
                    'message': row[2],
                    'status': row[3],
                    'retry_count': row[4],
                    'error_message': row[5],
                    'sent_at': row[6],
                    'created_at': row[7]
                })
            
            database_logger.debug(f"Loaded notification history: {len(history)} records")
            return history
    
    def update_notification_status(self, notification_id: int, status: str, 
                                  error_message: str = None, retry_count: int = None):
        """更新通知状态"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            updates = ["status = ?"]
            params = [status]
            
            if status == 'sent':
                updates.append("sent_at = ?")
                params.append(now_local().isoformat())
            
            if error_message is not None:
                updates.append("error_message = ?")
                params.append(error_message)
            
            if retry_count is not None:
                updates.append("retry_count = ?")
                params.append(retry_count)
            
            params.append(notification_id)
            
            query = f"UPDATE notification_history SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, params)
            
            conn.commit()
            database_logger.debug(f"Notification status updated: {notification_id} -> {status}")
    
    # Config表相关方法
    def save_config(self, config_key: str, config_value: Any, description: str = None):
        """保存配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            timestamp = now_local().isoformat()
            
            # 将配置值转换为JSON字符串
            if isinstance(config_value, (dict, list)):
                value_str = json.dumps(config_value, ensure_ascii=False)
            else:
                value_str = str(config_value)
            
            cursor.execute(
                """
                INSERT OR REPLACE INTO config 
                (config_key, config_value, description, created_at, updated_at)
                VALUES (?, ?, ?, 
                    COALESCE((SELECT created_at FROM config WHERE config_key = ?), ?),
                    ?)
                """,
                (config_key, value_str, description, config_key, timestamp, timestamp)
            )
            conn.commit()
            database_logger.debug(f"Config saved: {config_key}")
    
    def save_notification_config(self, settings: Dict[str, Any]):
        """保存通知配置到config表"""
        self.save_config('notification_settings', settings, 'Notification configuration settings')
        database_logger.debug(f"Notification config saved to config table: {list(settings.keys())}")
    
    def load_notification_config(self) -> Dict[str, Any]:
        """从config表加载通知配置"""
        config = self.get_config('notification_settings', {})
        database_logger.debug(f"Loaded notification config from config table: {list(config.keys()) if config else 'empty'}")
        return config

    def save_dom_config(self, settings: Dict[str, Any]):
        """保存DOM设置到config表"""
        self.save_config('dom_config', settings, 'DOM configuration settings')
        database_logger.debug("DOM config saved to config table")

    def load_dom_config(self) -> Dict[str, Any]:
        """从config表加载DOM设置"""
        config = self.get_config('dom_config', {})
        database_logger.debug(
            "Loaded DOM config from config table: {}",
            list(config.keys()) if isinstance(config, dict) else 'empty'
        )
        return config if isinstance(config, dict) else {}

    def save_strategy_config(self, settings: Dict[str, Any]):
        """保存策略配置到config表"""
        self.save_config('strategy_settings', settings, 'Strategy configuration settings')
        database_logger.debug(f"Strategy config saved to config table: {list(settings.keys())}")
    
    def load_strategy_config(self) -> Dict[str, Any]:
        """从config表加载策略配置"""
        config = self.get_config('strategy_settings', {})
        database_logger.debug(f"Loaded strategy config from config table: {list(config.keys()) if config else 'empty'}")
        return config
    
    def get_config(self, config_key: str, default_value: Any = None) -> Any:
        """获取配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT config_value FROM config WHERE config_key = ?",
                (config_key,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return default_value
            
            try:
                # 尝试解析JSON
                return json.loads(row[0])
            except json.JSONDecodeError:
                # 如果不是JSON，返回原始字符串
                return row[0]
    
    def get_all_configs(self) -> Dict[str, Any]:
        """获取所有配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT config_key, config_value FROM config ORDER BY config_key"
            )
            rows = cursor.fetchall()
            
            configs = {}
            for config_key, config_value in rows:
                try:
                    configs[config_key] = json.loads(config_value)
                except json.JSONDecodeError:
                    configs[config_key] = config_value
            
            return configs
    
    def delete_config(self, config_key: str):
        """删除配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM config WHERE config_key = ?", (config_key,))
            conn.commit()
            database_logger.debug(f"Config deleted: {config_key}")

# 全局数据库实例
db_manager = None

def get_db_manager(db_path: str = "data/trading_bot.db") -> DatabaseManager:
    """获取数据库管理器单例"""
    global db_manager
    if db_manager is None:
        db_manager = DatabaseManager(db_path)
    return db_manager