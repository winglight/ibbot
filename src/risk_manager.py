import asyncio
import os
import csv
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from queue import Queue

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from dotenv import load_dotenv

from .ibkr_client import IBKRClient
from .telegram_notifier import TelegramNotifier
from .database import DatabaseManager, get_db_manager
from .database import AutoOrder as DatabaseAutoOrder
from .database import RiskRule as DatabaseRiskRule

load_dotenv()

@dataclass
class AutoOrder:
    order_id: str
    symbol: str
    action: str
    quantity: int
    order_type: str
    price: Optional[float]
    status: str
    created_at: datetime
    executed_at: Optional[datetime] = None
    rule_id: Optional[str] = None
    order_source: str = "auto"  # "auto" or "manual"

class OrderType(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    RISK_CLOSE = "risk_close"

@dataclass
class RiskRule:
    symbol: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_loss_percent: Optional[float] = None
    enabled: bool = True
    created_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

@dataclass
class OrderRequest:
    order_type: OrderType
    symbol: str
    quantity: int
    price: Optional[float] = None
    reason: str = ""
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

class RiskManager:
    def __init__(self, ibkr_client: IBKRClient, telegram_notifier: TelegramNotifier):
        self.ibkr_client = ibkr_client
        self.telegram_notifier = telegram_notifier
        self.db_manager = get_db_manager()
        
        # 内存中的缓存，从数据库加载
        self.risk_rules: Dict[str, RiskRule] = {}
        self.auto_orders: Dict[str, AutoOrder] = {}
        
        # 订单队列
        self.order_queue = Queue()
        
        # 调度器
        self.scheduler = AsyncIOScheduler()

        # 组件依赖
        self.order_manager = None

        # 监控状态
        self.monitoring_active = False
        
        # 数据目录
        self.data_dir = "data"
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 风险阈值配置
        self.loss_threshold_1 = 0.03  # 3% 损失阈值，触发第一级警告
        self.loss_threshold_2 = 0.08  # 8% 损失阈值，触发自动平仓
        
        # 价格阈值与杠杆配置
        self.price_threshold = float(os.getenv('PRICE_THRESHOLD', 0.02))  # 从环境变量读取，默认2%金额阈值
        self.leverage_mnq = int(os.getenv('LEVERAGE_MNQ', 20))  # MNQ杠杆倍数，从环境变量读取
        
        # 待处理的限价订单状态
        self.pending_limit_orders: Dict[str, Dict[str, bool]] = {}
        
        # 警告历史记录
        self.alert_history: Dict[str, Dict[str, datetime]] = {}
        
        # 从数据库加载现有数据
        self._load_from_database()

    def _load_from_database(self):
        """从数据库加载风险规则和订单记录到内存缓存"""
        try:
            # 加载风险规则
            db_rules = self.db_manager.load_risk_rules()
            for rule_db in db_rules:
                rule = RiskRule(
                    symbol=rule_db.symbol,
                    stop_loss_price=rule_db.stop_loss_price,
                    take_profit_price=rule_db.take_profit_price,
                    max_loss_percent=rule_db.max_loss_percent,
                    enabled=getattr(rule_db, 'enabled', True),  # 从数据库读取enabled
                    created_at=datetime.fromisoformat(rule_db.timestamp) if rule_db.timestamp else datetime.now()
                )
                self.risk_rules[rule.symbol] = rule
            
            # 加载订单记录
            db_orders = self.db_manager.load_auto_orders()
            for order_db in db_orders:
                # 从数据库的AutoOrder转换为RiskManager的AutoOrder
                # 正确映射所有字段，处理缺失的status、executed_at等字段
                executed_dt = None
                try:
                    executed_dt = datetime.fromisoformat(order_db.executed_at) if getattr(order_db, 'executed_at', None) else None
                except Exception:
                    executed_dt = None
                order = AutoOrder(
                    order_id=order_db.order_id,
                    symbol=order_db.symbol,
                    action=order_db.action,
                    quantity=order_db.quantity,
                    order_type=order_db.order_type,
                    price=order_db.price,
                    status=getattr(order_db, 'status', None) or "pending",
                    created_at=datetime.fromisoformat(order_db.timestamp) if order_db.timestamp else datetime.now(),
                    executed_at=executed_dt,  # 可能为None
                    rule_id=getattr(order_db, 'rule_id', None),
                    order_source=getattr(order_db, 'order_source', None) or "auto"
                )
                self.auto_orders[order.order_id] = order
            
            logger.info(f"Loaded {len(self.risk_rules)} risk rules and {len(self.auto_orders)} orders from database")
        except Exception as e:
            logger.error(f"Error loading data from database: {e}")
            # 如果数据库加载失败，尝试从CSV文件迁移
            self._load_from_files_and_migrate()
    
    def _load_from_files_and_migrate(self):
        """从旧的CSV/JSON文件加载数据并迁移到数据库"""
        try:
            # 尝试从CSV文件加载并迁移到数据库
            logger.info("Attempting to migrate data from CSV files to database")
            
            # 迁移风险规则
            risk_rules_file = os.path.join(self.data_dir, "risk_rules.csv")
            if os.path.exists(risk_rules_file):
                self.load_risk_rules_from_csv(risk_rules_file)
                # 将加载的规则保存到数据库
                for rule in self.risk_rules.values():
                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                    )
                    self.db_manager.save_risk_rule(db_rule)
                logger.info(f"Migrated {len(self.risk_rules)} risk rules to database")
            
            # 迁移订单记录
            orders_file = os.path.join(self.data_dir, "orders.csv")
            if os.path.exists(orders_file):
                self.load_auto_orders_from_csv(orders_file)
                # 将加载的订单保存到数据库
                for order in self.auto_orders.values():
                    self.save_auto_order_to_db(order)
                logger.info(f"Migrated {len(self.auto_orders)} orders to database")
                
        except Exception as e:
            logger.error(f"Error during CSV to database migration: {e}")

    def start(self):
        """启动风险管理器"""
        try:
            # 检查是否有运行的事件循环
            try:
                loop = asyncio.get_running_loop()
                # 如果有运行的事件循环，直接启动调度器
                self._start_scheduler()
            except RuntimeError:
                # 没有运行的事件循环，创建一个新的事件循环来启动调度器
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    self._start_scheduler()
                except Exception as e:
                    logger.warning(f"无法启动异步调度器: {e}，将使用简化模式")
                    self.monitoring_active = True
                    logger.info("Risk manager started in simplified mode")
                    return
            
            self.monitoring_active = True
            logger.info("Risk monitoring started successfully")
            
        except Exception as e:
            logger.error(f"Error starting risk manager: {e}")
    
    def _start_scheduler(self):
        """启动调度器和添加任务"""
        # 启动调度器
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Risk manager scheduler started")
        
        # 添加监控任务
        self.scheduler.add_job(
            self._monitor_positions,
            'interval',
            seconds=15,  # 从30秒改为15秒，提高检查频率
            id='position_monitor',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=10,
            max_instances=1
        )
        
        # 添加处理订单队列任务
        self.scheduler.add_job(
            self.process_pending_orders,
            'interval',
            seconds=5,
            id='order_processor',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=10,
            max_instances=1
        )
        
        # 添加每日汇总任务（每天早上9点）
        self.scheduler.add_job(
            self._send_daily_summary,
            'cron',
            hour=9,
            minute=0,
            id='daily_summary',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=60,
            max_instances=1
        )

    def stop(self):
        """停止风险管理器"""
        try:
            self.monitoring_active = False
            if self.scheduler.running:
                self.scheduler.shutdown()
            logger.info("Risk manager stopped")
        except Exception as e:
            logger.error(f"Error stopping risk manager: {e}")

    def add_risk_rule(self, symbol: str, stop_loss_price: Optional[float] = None, 
                      take_profit_price: Optional[float] = None, max_loss_percent: Optional[float] = None):
        """添加风险规则"""
        try:
            logger.info(f"add_risk_rule called: symbol={symbol}, SL={stop_loss_price}, TP={take_profit_price}, max_loss%={max_loss_percent}")
            rule = RiskRule(
                symbol=symbol,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                max_loss_percent=max_loss_percent,
                enabled=True,
                created_at=datetime.now()
            )
            
            # 转换为数据库层的 RiskRule 并保存到数据库
            db_rule = DatabaseRiskRule(
                symbol=rule.symbol,
                stop_loss_price=rule.stop_loss_price,
                take_profit_price=rule.take_profit_price,
                max_loss_percent=rule.max_loss_percent,
                enabled=rule.enabled,
                timestamp=rule.created_at.isoformat()
            )
            self.db_manager.save_risk_rule(db_rule)
            logger.debug(f"Risk rule persisted: {db_rule}")
            
            # 更新内存缓存
            self.risk_rules[symbol] = rule
            
            logger.info(f"Risk rule added for {symbol}")
            return True
        except Exception as e:
            logger.error(f"Error adding risk rule: {e}")
            return False

    def remove_risk_rule(self, symbol: str):
        """删除风险规则"""
        try:
            logger.info(f"remove_risk_rule called: symbol={symbol}")
            # 从数据库删除
            self.db_manager.delete_risk_rule(symbol)
            
            # 从内存删除
            if symbol in self.risk_rules:
                del self.risk_rules[symbol]
                logger.info(f"Risk rule removed for {symbol}")
                return True
            else:
                logger.warning(f"Risk rule for {symbol} not found")
                return False
        except Exception as e:
            logger.error(f"Error removing risk rule: {e}")
            return False

    def update_risk_rule(self, symbol: str, stop_loss_price: float = None, 
                         take_profit_price: float = None, enabled: bool = True) -> bool:
        """更新风险规则"""
        try:
            logger.info(f"update_risk_rule called: symbol={symbol}, SL={stop_loss_price}, TP={take_profit_price}, enabled={enabled}")
            if symbol in self.risk_rules:
                rule = self.risk_rules[symbol]
                if stop_loss_price is not None:
                    logger.debug(f"Updating SL for {symbol}: {rule.stop_loss_price} -> {stop_loss_price}")
                    rule.stop_loss_price = stop_loss_price
                if take_profit_price is not None:
                    logger.debug(f"Updating TP for {symbol}: {rule.take_profit_price} -> {take_profit_price}")
                    rule.take_profit_price = take_profit_price
                if rule.enabled != enabled:
                    logger.debug(f"Updating enabled for {symbol}: {rule.enabled} -> {enabled}")
                rule.enabled = enabled
                
                # 转换为数据库层的 RiskRule 并保存到数据库
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    timestamp=rule.created_at.isoformat()
                )
                self.db_manager.save_risk_rule(db_rule)
                logger.debug(f"Risk rule persisted after update: {db_rule}")
                
                logger.info(f"Risk rule updated for {symbol}")
                return True
            else:
                logger.warning(f"Risk rule for {symbol} not found")
                return False
        except Exception as e:
            logger.error(f"Error updating risk rule for {symbol}: {e}")
            return False

    def get_risk_rules(self) -> Dict[str, RiskRule]:
        """获取所有风险规则"""
        return self.risk_rules.copy()

    async def _monitor_positions(self):
        """监控持仓风险"""
        try:
            if not self.ibkr_client or not getattr(self.ibkr_client, 'connected', False):
                logger.warning("IBKR client not ready, skipping position monitoring")
                return
            
            logger.info("=== Starting position monitoring cycle ===")
            
            positions = self.ibkr_client.get_positions()
            account_info = self.ibkr_client.get_account_info()
            
            logger.info(f"Found {len(positions)} positions to monitor")
            logger.info(f"Active risk rules: {len([r for r in self.risk_rules.values() if r.enabled])}")
            
            for position in positions:
                symbol = position['symbol']
                current_price = position['current_price']
                logger.info(f"Checking position: {symbol} @ ${current_price:.2f}, qty: {position['position']}")
                
                await self._check_position_risk(position, account_info)
                await self._check_stop_loss_take_profit(position)
            
            logger.info("=== Position monitoring cycle completed ===")
            
        except Exception as e:
            logger.error(f"Error monitoring positions: {e}")
    
    async def _check_position_risk(self, position: Dict, account_info: Dict):
        """检查持仓风险"""
        symbol = position['symbol']
        loss_percent = abs(position['unrealized_pnl_percent']) / 100
        
        # Check if position is in loss
        if position['unrealized_pnl'] >= 0:
            return
        
        current_time = datetime.now()
        
        # Check first threshold (notification)
        if loss_percent >= self.loss_threshold_1:
            alert_key = f"{symbol}_threshold1"
            last_alert = self.alert_history.get(symbol, {}).get('threshold1')
            
            # Send alert only once per hour to avoid spam
            if not last_alert or (current_time - last_alert).seconds > 3600:
                await self.telegram_notifier.send_loss_alert(
                    symbol=symbol,
                    position=position['position'],
                    current_price=position['current_price'],
                    avg_cost=position['avg_cost'],
                    loss_percent=loss_percent * 100,
                    threshold=self.loss_threshold_1
                )
                
                if symbol not in self.alert_history:
                    self.alert_history[symbol] = {}
                self.alert_history[symbol]['threshold1'] = current_time
        
        # Check second threshold (auto close)
        if loss_percent >= self.loss_threshold_2:
            alert_key = f"{symbol}_threshold2"
            last_alert = self.alert_history.get(symbol, {}).get('threshold2')
            
            # Auto close only once
            if not last_alert:
                # Check available funds before closing
                if self._check_sufficient_funds(account_info, position):
                    order_request = OrderRequest(
                        order_type=OrderType.RISK_CLOSE,
                        symbol=symbol,
                        quantity=abs(int(position['position'])),
                        reason=f"Auto close due to {loss_percent*100:.2f}% loss"
                    )
                    self.order_queue.put(order_request)
                    
                    if symbol not in self.alert_history:
                        self.alert_history[symbol] = {}
                    self.alert_history[symbol]['threshold2'] = current_time
                    
                    logger.warning(f"Auto close triggered for {symbol} due to {loss_percent*100:.2f}% loss")
    
    async def _check_stop_loss_take_profit(self, position: Dict):
        """检查止损止盈并提前下限价单"""
        symbol = position['symbol']
        current_price = position['current_price']
        
        if symbol not in self.risk_rules:
            logger.debug(f"No risk rule found for {symbol}")
            return
        
        rule = self.risk_rules[symbol]
        if not rule.enabled:
            logger.debug(f"Risk rule for {symbol} is disabled")
            return
        
        position_qty = int(position['position'])
        if position_qty == 0:
            logger.debug(f"No position for {symbol}, disabling rule")
            rule.enabled = False
            # 更新数据库中的风险规则状态
            db_rule = DatabaseRiskRule(
                symbol=rule.symbol,
                stop_loss_price=rule.stop_loss_price,
                take_profit_price=rule.take_profit_price,
                max_loss_percent=rule.max_loss_percent,
                enabled=rule.enabled,
                timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
            )
            self.db_manager.save_risk_rule(db_rule)
            return
        
        logger.info(f"Checking risk rule for {symbol}: SL=${rule.stop_loss_price}, TP=${rule.take_profit_price}")
        
        # Initialize pending orders tracking for this symbol
        if symbol not in self.pending_limit_orders:
            self.pending_limit_orders[symbol] = {"stop_loss": False, "take_profit": False}
        
        # 以金额阈值为基础，按杠杆换算成价格差阈值
        amount_threshold = self.price_threshold
        threshold_price = amount_threshold * self.leverage_mnq if symbol == 'MNQ' else amount_threshold
        
        # 检查是否已有任何limit order pending，如果有则跳过
        if self.pending_limit_orders[symbol]["stop_loss"] or self.pending_limit_orders[symbol]["take_profit"]:
            logger.debug(f"Already have pending limit order for {symbol}, skipping new order placement")
            return
        
        # 标记是否已下单，确保只下一个单
        order_placed = False
        
        # Prefer: Take Profit has higher priority than Stop Loss when both trigger simultaneously
        # Check take profit first
        if rule.take_profit_price and not order_placed:
            # 对于止盈：多头持仓时价格应该上涨到目标价格，空头持仓时价格应该下跌到目标价格
            if position_qty > 0:  # 多头持仓
                price_diff = current_price - rule.take_profit_price  # 当前价格 - 止盈价格
                # 修复：只有当价格接近止盈价格时才触发（price_diff应该接近0且为负值或小正值）
                should_trigger = price_diff >= -threshold_price and price_diff <= threshold_price
            else:  # 空头持仓
                price_diff = rule.take_profit_price - current_price  # 止盈价格 - 当前价格
                # 修复：只有当价格接近止盈价格时才触发（price_diff应该接近0且为负值或小正值）
                should_trigger = price_diff >= -threshold_price and price_diff <= threshold_price
            
            logger.info(f"Take profit check for {symbol}: current=${current_price:.2f}, target=${rule.take_profit_price:.2f}, price_diff=${price_diff:.4f}, threshold_price=${threshold_price:.4f}, position_qty={position_qty}")
            
            # If price reaches take profit level, place limit order
            if should_trigger:
                logger.info(f"Price close to take profit for {symbol}, placing limit order")
                try:
                    action = 'SELL' if position_qty > 0 else 'BUY'
                    # 直接调用下单方法，避免嵌套的事件循环调用
                    trade = self.ibkr_client.place_limit_order(
                        symbol=symbol,
                        quantity=abs(position_qty),
                        price=rule.take_profit_price,
                        action=action
                    )
                    
                    if trade:
                        self.pending_limit_orders[symbol]["take_profit"] = True
                        order_placed = True
                        
                        # 保存订单记录到auto_orders内存和数据库
                        auto_order = AutoOrder(
                            order_id=str(trade.order.orderId),
                            symbol=symbol,
                            action=action,
                            quantity=abs(position_qty),
                            order_type="LMT",
                            price=rule.take_profit_price,
                            status="Submitted",
                            created_at=datetime.now(),
                            executed_at=None,
                            rule_id=symbol,
                            order_source="tp"
                        )
                        self.auto_orders[str(trade.order.orderId)] = auto_order
                        # 保存到数据库，保证持久化
                        self.save_auto_order_to_db(auto_order)
                        
                        logger.info(f"✅ Limit order placed for take profit: {symbol} at ${rule.take_profit_price:.2f}")
                        logger.info(f"Take profit limit order successfully placed for {symbol}, rule remains active")
                        
                        # Send notification
                        await self.telegram_notifier.send_message(
                            f"🟢 Limit order placed for take profit\n"
                            f"Symbol: {symbol}\n"
                            f"Price: ${rule.take_profit_price:.2f}\n"
                            f"Current Price: ${current_price:.2f}\n"
                            f"Quantity: {abs(position_qty)}"
                        )
                    else:
                        logger.warning(f"Failed to place take profit limit order for {symbol}")
                        # 下单失败时禁用规则
                        rule.enabled = False
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.db_manager.save_risk_rule(db_rule)
                        logger.info(f"Take profit rule disabled due to order failure for {symbol}")
                except Exception as e:
                    logger.error(f"Failed to place take profit limit order for {symbol}: {e}")
                    # 下单异常时禁用规则
                    rule.enabled = False
                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                    )
                    self.db_manager.save_risk_rule(db_rule)
                    logger.info(f"Take profit rule disabled due to exception for {symbol}")
            
            # If price has reached take profit, execute market order
            if current_price >= rule.take_profit_price:
                logger.warning(f"🎯 Take profit triggered for {symbol}: ${current_price:.2f} >= ${rule.take_profit_price:.2f}")
                
                # Check position direction before placing order
                action = 'SELL' if position_qty > 0 else 'BUY'
                
                order_request = OrderRequest(
                    order_type=OrderType.TAKE_PROFIT,
                    symbol=symbol,
                    quantity=abs(position_qty),
                    reason=f"Take profit triggered at ${current_price:.2f}"
                )
                self.order_queue.put(order_request)
                
                # Disable rule after trigger and update database
                rule.enabled = False
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                )
                self.db_manager.save_risk_rule(db_rule)
                logger.info(f"Take profit order queued for {symbol} at ${current_price:.2f}, rule disabled")
                return  # 当触发止盈市价单时立即返回，不再检查止损

        # Check stop loss
        if rule.stop_loss_price and not order_placed:
            # 对于止损：多头持仓时价格应该下跌到目标价格，空头持仓时价格应该上涨到目标价格
            if position_qty > 0:  # 多头持仓
                price_diff = rule.stop_loss_price - current_price  # 止损价格 - 当前价格
                # 修复：只有当价格接近止损价格时才触发（price_diff应该接近0且为负值或小正值）
                should_trigger = price_diff >= -threshold_price and price_diff <= threshold_price
            else:  # 空头持仓
                price_diff = current_price - rule.stop_loss_price  # 当前价格 - 止损价格
                # 修复：只有当价格接近止损价格时才触发（price_diff应该接近0且为负值或小正值）
                should_trigger = price_diff >= -threshold_price and price_diff <= threshold_price
            
            logger.info(f"Stop loss check for {symbol}: current=${current_price:.2f}, target=${rule.stop_loss_price:.2f}, price_diff=${price_diff:.4f}, threshold_price=${threshold_price:.4f}, position_qty={position_qty}")
            
            # If price reaches stop loss level, place market order immediately
            if should_trigger:
                logger.info(f"Price close to stop loss for {symbol}, placing limit order")
                try:
                    action = 'SELL' if position_qty > 0 else 'BUY'
                    # 直接调用下单方法，避免嵌套的事件循环调用
                    trade = self.ibkr_client.place_limit_order(
                        symbol=symbol,
                        quantity=abs(position_qty),
                        price=rule.stop_loss_price,
                        action=action
                    )
                    
                    if trade:
                        self.pending_limit_orders[symbol]["stop_loss"] = True
                        order_placed = True
                        
                        # 保存订单记录到auto_orders内存和数据库
                        auto_order = AutoOrder(
                            order_id=str(trade.order.orderId),
                            symbol=symbol,
                            action=action,
                            quantity=abs(position_qty),
                            order_type="LMT",
                            price=rule.stop_loss_price,
                            status="Submitted",
                            created_at=datetime.now(),
                            executed_at=None,
                            rule_id=symbol,
                            order_source="sl"
                        )
                        self.auto_orders[str(trade.order.orderId)] = auto_order
                        self.save_auto_order_to_db(auto_order)
                        
                        logger.info(f"✅ Limit order placed for stop loss: {symbol} at ${rule.stop_loss_price:.2f}")
                        logger.info(f"Stop loss limit order successfully placed for {symbol}, rule remains active")
                        
                        # Send notification
                        await self.telegram_notifier.send_message(
                            f"🔴 Limit order placed for stop loss\n"
                            f"Symbol: {symbol}\n"
                            f"Price: ${rule.stop_loss_price:.2f}\n"
                            f"Current Price: ${current_price:.2f}\n"
                            f"Quantity: {abs(position_qty)}"
                        )
                    else:
                        logger.warning(f"Failed to place stop loss limit order for {symbol}")
                        # 下单失败时禁用规则
                        rule.enabled = False
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.db_manager.save_risk_rule(db_rule)
                        logger.info(f"Stop loss rule disabled due to order failure for {symbol}")
                except Exception as e:
                    logger.error(f"Failed to place stop loss limit order for {symbol}: {e}")
                    # 下单异常时禁用规则
                    rule.enabled = False
                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                    )
                    self.db_manager.save_risk_rule(db_rule)
                    logger.info(f"Stop loss rule disabled due to exception for {symbol}")
            
            # If price has reached stop loss, execute market order
            if current_price <= rule.stop_loss_price:
                logger.warning(f"🚨 Stop loss triggered for {symbol}: ${current_price:.2f} <= ${rule.stop_loss_price:.2f}")
                
                # Check position direction before placing order
                action = 'SELL' if position_qty > 0 else 'BUY'
                
                order_request = OrderRequest(
                    order_type=OrderType.STOP_LOSS,
                    symbol=symbol,
                    quantity=abs(position_qty),
                    reason=f"Stop loss triggered at ${current_price:.2f}"
                )
                self.order_queue.put(order_request)
                
                # Disable rule after trigger and update database
                rule.enabled = False
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                )
                self.db_manager.save_risk_rule(db_rule)
                logger.info(f"Stop loss order queued for {symbol} at ${current_price:.2f}, rule disabled")
                return  # 触发市价单后立即返回，不再检查止盈
    
    def _check_sufficient_funds(self, account_info: Dict, position: Dict) -> bool:
        """检查资金是否充足"""
        available_funds = account_info.get('available_funds', 0)
        required_funds = abs(position['position']) * position['current_price'] * 0.1  # 10% margin
        
        return available_funds >= required_funds
    
    async def process_pending_orders(self):
        """处理队列中的待处理订单"""
        processed_count = 0
        while not self.order_queue.empty():
            try:
                order_request = self.order_queue.get_nowait()
                await self._execute_order(order_request)
                processed_count += 1
            except Exception as e:
                logger.error(f"Error processing order: {e}")
        
        if processed_count > 0:
            logger.info(f"Processed {processed_count} pending orders")
    
    async def _execute_order(self, order_request: OrderRequest):
        """执行订单"""
        logger.info(f"开始执行订单: {order_request.order_type.value} for {order_request.symbol}, quantity: {order_request.quantity}")
        
        try:
            # 检查连接状态
            if not self.ibkr_client or not getattr(self.ibkr_client, 'connected', False):
                logger.error(f"Cannot execute order for {order_request.symbol}: IBKR client not ready")
                return
            
            logger.debug(f"IBKR连接状态正常，开始获取持仓信息 for {order_request.symbol}")
            
            # Determine order action
            positions = self.ibkr_client.get_positions()
            logger.debug(f"获取到 {len(positions)} 个持仓")
            
            position = next((p for p in positions if p['symbol'] == order_request.symbol), None)
            
            if not position or position['position'] == 0:
                logger.warning(f"No position found for {order_request.symbol}, skipping order. Available positions: {[p['symbol'] for p in positions]}")
                return
            
            logger.info(f"找到持仓: {order_request.symbol}, position: {position['position']}, avg_cost: {position.get('avg_cost', 'N/A')}")
            
            # Determine action based on current position
            action = 'SELL' if position['position'] > 0 else 'BUY'
            logger.info(f"确定订单动作: {action} {order_request.quantity} {order_request.symbol}")
            
            # 检查当前事件循环状态
            try:
                current_loop = asyncio.get_running_loop()
                logger.debug(f"当前事件循环: {current_loop}, 是否运行中: {current_loop.is_running()}")
            except RuntimeError as e:
                logger.debug(f"获取事件循环状态失败: {e}")
            
            # Place market order for immediate execution using executor to avoid event loop conflicts
            logger.info(f"使用executor执行下单操作以避免事件循环冲突")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            try:
                trade = await loop.run_in_executor(
                    None,
                    lambda: self.ibkr_client.place_market_order(
                        symbol=order_request.symbol,
                        quantity=order_request.quantity,
                        action=action
                    )
                )
                logger.info(f"下单操作完成，trade结果: {trade is not None}")
            except Exception as executor_error:
                logger.error(f"Executor执行下单失败 for {order_request.symbol}: {executor_error}")
                logger.error(f"错误类型: {type(executor_error).__name__}")
                import traceback
                logger.error(f"错误堆栈: {traceback.format_exc()}")
                raise
            
            if trade:
                order_id = trade.order.orderId
                
                # Record auto order
                # 根据订单类型设置order_source
                if order_request.order_type == OrderType.STOP_LOSS:
                    order_source = "sl"
                elif order_request.order_type == OrderType.TAKE_PROFIT:
                    order_source = "tp"
                else:
                    order_source = "strategy"
                
                auto_order = AutoOrder(
                    order_id=str(order_id),
                    symbol=order_request.symbol,
                    action=action,
                    quantity=order_request.quantity,
                    order_type="MKT",
                    price=position['current_price'],
                    status="Submitted",
                    created_at=datetime.now(),
                    executed_at=None,
                    rule_id=order_request.symbol,  # Use symbol as rule identifier
                    order_source=order_source
                )
                
                self.auto_orders[str(order_id)] = auto_order
                self.save_auto_order_to_db(auto_order)
                
                # Send notification based on order type
                if order_request.order_type == OrderType.STOP_LOSS:
                    await self.telegram_notifier.send_stop_loss_alert(
                        symbol=order_request.symbol,
                        position=position['position'],
                        trigger_price=position['current_price'],
                        order_type="loss",
                        order_id=order_id
                    )
                elif order_request.order_type == OrderType.TAKE_PROFIT:
                    await self.telegram_notifier.send_stop_loss_alert(
                        symbol=order_request.symbol,
                        position=position['position'],
                        trigger_price=position['current_price'],
                        order_type="profit",
                        order_id=order_id
                    )
                elif order_request.order_type == OrderType.RISK_CLOSE:
                    await self.telegram_notifier.send_auto_close_alert(
                        symbol=order_request.symbol,
                        position=position['position'],
                        order_id=order_id
                    )
                
                logger.info(f"Order executed: {order_request.order_type.value} for {order_request.symbol}, Order ID: {order_id}")
            else:
                logger.error(f"Failed to execute order for {order_request.symbol}")
                
        except Exception as e:
            logger.error(f"Error executing order for {order_request.symbol}: {e}")
    
    async def _send_daily_summary(self):
        """发送每日摘要"""
        try:
            if not self.ibkr_client or not getattr(self.ibkr_client, 'connected', False):
                return
            
            account_info = self.ibkr_client.get_account_info()
            positions = self.ibkr_client.get_positions()
            
            await self.telegram_notifier.send_daily_summary(account_info, positions)
            
        except Exception as e:
            logger.error(f"Error sending daily summary: {e}")
    
    def get_order_queue_size(self) -> int:
        """获取订单队列大小"""
        return self.order_queue.qsize()
    
    def clear_order_queue(self):
        """清空订单队列"""
        while not self.order_queue.empty():
            try:
                self.order_queue.get_nowait()
            except:
                break
        logger.info("Order queue cleared")
    
    def save_auto_order_to_db(self, order: AutoOrder):
        """保存单个订单到数据库并更新内存缓存"""
        try:
            # 创建数据库的AutoOrder实例，映射字段
            db_order = DatabaseAutoOrder(
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                action=order.action,
                order_type=order.order_type,
                price=order.price,
                timestamp=order.created_at.isoformat() if order.created_at else datetime.now().isoformat(),
                exchange=getattr(order, 'exchange', 'SMART'),
                sec_type=getattr(order, 'sec_type', 'STK'),
                status=order.status if hasattr(order, 'status') else 'pending',
                executed_at=order.executed_at.isoformat() if getattr(order, 'executed_at', None) else None,
                rule_id=getattr(order, 'rule_id', None),
                order_source=getattr(order, 'order_source', 'auto')
            )
            
            # 保存到数据库
            self.db_manager.save_auto_order(db_order)
            
            # 更新内存缓存
            self.auto_orders[order.order_id] = order
            
            logger.info(f"Order {order.order_id} saved to database")
        except Exception as e:
            logger.error(f"Error saving order to database: {e}")
    
    def get_auto_orders(self) -> List[Dict]:
        """获取所有订单记录列表（包含自动和手动订单）"""
        orders = []
        for order in self.auto_orders.values():
            orders.append({
                'order_id': order.order_id,
                'symbol': order.symbol,
                'action': order.action,
                'quantity': order.quantity,
                'order_type': order.order_type,
                'price': order.price,
                'status': order.status,
                'created_at': order.created_at.isoformat() if order.created_at else None,
                'executed_at': order.executed_at.isoformat() if order.executed_at else None,
                'rule_id': order.rule_id,
                'order_source': order.order_source
            })
        return orders

    def set_dependencies(self, order_manager=None):
        """注入外部依赖"""
        self.order_manager = order_manager

    def remove_order_record(self, order_id: str):
        """从记录中删除订单"""
        try:
            soft_deleted = False
            # 优先尝试通过 OrderManager 执行软删除，避免被 IBKR 重新同步
            if getattr(self, 'order_manager', None) and hasattr(self.order_manager, 'mark_order_deleted'):
                try:
                    soft_deleted = bool(self.order_manager.mark_order_deleted(order_id))
                    if soft_deleted:
                        logger.info(f"Order {order_id} soft-deleted via OrderManager")
                except Exception as e:
                    logger.warning(f"Order {order_id} soft-delete failed: {e}")

            had_memory_record = order_id in self.auto_orders
            if had_memory_record:
                del self.auto_orders[order_id]

            if soft_deleted or had_memory_record:
                logger.info(f"Order {order_id} removed from records")
                return True
            else:
                logger.warning(f"Order {order_id} not found in records")
                return soft_deleted
        except Exception as e:
            logger.error(f"Error removing order {order_id}: {e}")
            return False

    def update_order_status(self, order_id: str, new_status: str):
        """更新订单状态（内存与数据库）"""
        try:
            if order_id in self.auto_orders:
                order = self.auto_orders[order_id]
                order.status = new_status
                if new_status in ['Filled', 'Cancelled']:
                    order.executed_at = datetime.now()
                
                # 如果订单是由风险规则自动生成且已成交，则禁用对应的风险规则并持久化
                if new_status == 'Filled' and getattr(order, 'order_source', None) == 'auto' and getattr(order, 'rule_id', None):
                    symbol = order.rule_id
                    rule = self.risk_rules.get(symbol)
                    if rule and rule.enabled:
                        rule.enabled = False
                        try:
                            db_rule = DatabaseRiskRule(
                                symbol=rule.symbol,
                                stop_loss_price=rule.stop_loss_price,
                                take_profit_price=rule.take_profit_price,
                                max_loss_percent=rule.max_loss_percent,
                                enabled=rule.enabled,
                                timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                            )
                            self.db_manager.save_risk_rule(db_rule)
                            logger.info(f"Risk rule for {symbol} disabled due to filled order {order_id}")
                        except Exception as db_rule_err:
                            logger.warning(f"Failed to persist disabled rule for {symbol}: {db_rule_err}")
                
                # 同步到数据库：同时持久化状态、均价（若有）、执行时间
                try:
                    executed_str = order.executed_at.isoformat() if order.executed_at else None
                    self.db_manager.update_auto_order(order_id, status=new_status, price=order.price, executed_at=executed_str)
                    logger.info(f"Order {order_id} status persisted to DB: {new_status}, price={order.price}, executed_at={executed_str}")
                except Exception as db_err:
                    logger.warning(f"Failed to persist order {order_id} update to DB: {db_err}")
                
                return True
            else:
                logger.warning(f"Order {order_id} not found in records for status update")
                return False
        except Exception as e:
            logger.error(f"Error updating order {order_id} status: {e}")
            return False

    # 保留原有的CSV方法作为备份和迁移用途，但标记为弃用
    def save_risk_rules_to_csv(self, filename: str = "risk_rules.csv"):
        """保存风险规则到CSV文件 - 已弃用，请使用数据库"""
        logger.warning("save_risk_rules_to_csv is deprecated, data is now stored in database")
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['symbol', 'stop_loss_price', 'take_profit_price', 'max_loss_percent', 'enabled', 'created_at']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                writer.writeheader()
                for symbol, rule in self.risk_rules.items():
                    writer.writerow({
                        'symbol': rule.symbol,
                        'stop_loss_price': rule.stop_loss_price,
                        'take_profit_price': rule.take_profit_price,
                        'max_loss_percent': rule.max_loss_percent,
                        'enabled': rule.enabled,
                        'created_at': rule.created_at.isoformat() if rule.created_at else None
                    })
            logger.info(f"Risk rules saved to {filename}")
        except Exception as e:
            logger.error(f"Error saving risk rules to CSV: {e}")
    
    def load_risk_rules_from_csv(self, filename: str = "risk_rules.csv"):
        """从CSV文件加载风险规则 - 已弃用，请使用数据库"""
        logger.warning("load_risk_rules_from_csv is deprecated, data is now loaded from database")
        try:
            if not os.path.exists(filename):
                logger.warning(f"CSV file {filename} not found")
                return
            
            with open(filename, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                
                for row in reader:
                    symbol = row['symbol']
                    stop_loss_price = float(row['stop_loss_price']) if row['stop_loss_price'] else None
                    take_profit_price = float(row['take_profit_price']) if row['take_profit_price'] else None
                    max_loss_percent = float(row['max_loss_percent']) if row['max_loss_percent'] else None
                    enabled = row['enabled'].lower() == 'true' if row['enabled'] else True
                    created_at = datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.now()
                    
                    self.risk_rules[symbol] = RiskRule(
                        symbol=symbol,
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                        max_loss_percent=max_loss_percent,
                        enabled=enabled,
                        created_at=created_at
                    )
            
            logger.info(f"Risk rules loaded from {filename}, total: {len(self.risk_rules)}")
        except Exception as e:
            logger.error(f"Error loading risk rules from CSV: {e}")
    
    def save_auto_orders_to_csv(self, filename: str = "orders.csv"):
        """保存订单记录到CSV文件 - 已弃用，请使用数据库"""
        logger.warning("save_auto_orders_to_csv is deprecated, data is now stored in database")
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['order_id', 'symbol', 'action', 'quantity', 'order_type', 'price', 'status', 'created_at', 'executed_at', 'rule_id', 'order_source']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                writer.writeheader()
                for order_id, order in self.auto_orders.items():
                    writer.writerow({
                        'order_id': order.order_id,
                        'symbol': order.symbol,
                        'action': order.action,
                        'quantity': order.quantity,
                        'order_type': order.order_type,
                        'price': order.price,
                        'status': order.status,
                        'created_at': order.created_at.isoformat() if order.created_at else None,
                        'executed_at': order.executed_at.isoformat() if order.executed_at else None,
                        'rule_id': order.rule_id,
                        'order_source': order.order_source
                    })
            logger.info(f"All orders saved to {filename}")
        except Exception as e:
            logger.error(f"Error saving orders to CSV: {e}")
    
    def load_auto_orders_from_csv(self, filename: str = "orders.csv"):
        """从CSV文件加载订单记录 - 已弃用，请使用数据库"""
        logger.warning("load_auto_orders_from_csv is deprecated, data is now loaded from database")
        try:
            if not os.path.exists(filename):
                logger.warning(f"CSV file {filename} not found")
                return
            
            with open(filename, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                
                for row in reader:
                    created_at = datetime.fromisoformat(row['created_at']) if row['created_at'] else None
                    executed_at = datetime.fromisoformat(row['executed_at']) if row['executed_at'] else None
                    
                    auto_order = AutoOrder(
                        order_id=row['order_id'],
                        symbol=row['symbol'],
                        action=row['action'],
                        quantity=int(row['quantity']) if row['quantity'] else 0,
                        order_type=row['order_type'],
                        price=float(row['price']) if row['price'] else 0.0,
                        status=row['status'],
                        created_at=created_at,
                        executed_at=executed_at,
                        rule_id=row['rule_id'],
                        order_source=row['order_source']
                    )
                    
                    self.auto_orders[row['order_id']] = auto_order
            
            logger.info(f"All orders loaded from {filename}, total orders: {len(self.auto_orders)}")
        except Exception as e:
            logger.error(f"Error loading orders from CSV: {e}")