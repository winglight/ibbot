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
        self.scheduler = AsyncIOScheduler()
        self.order_queue = Queue()
        self.risk_rules: Dict[str, RiskRule] = {}
        self.auto_orders: Dict[str, AutoOrder] = {}  # 自动下单记录
        self.processing_orders = False
        self.pending_limit_orders: Dict[str, Dict[str, bool]] = {}  # Track pending limit orders {symbol: {"stop_loss": bool, "take_profit": bool}}
        
        # Configuration from environment
        self.loss_threshold_1 = float(os.getenv('LOSS_THRESHOLD_1', '0.05'))  # 5%
        self.loss_threshold_2 = float(os.getenv('LOSS_THRESHOLD_2', '0.10'))  # 10%
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '5'))  # 5 minutes
        self.price_threshold = float(os.getenv('PRICE_THRESHOLD', '0.01'))  # 1% price threshold
        self.leverage_mnq = float(os.getenv('LEVERAGE_MNQ', '20'))  # MNQ leverage
        
        # Track alerts to avoid spam
        self.alert_history: Dict[str, Dict[str, datetime]] = {}
        
        logger.info(f"Risk Manager initialized with thresholds: {self.loss_threshold_1*100}%, {self.loss_threshold_2*100}%")
    
    def start(self):
        """启动风险管理器"""
        self.processing_orders = True
        
        # Schedule risk monitoring
        self.scheduler.add_job(
            self._monitor_positions,
            'interval',
            minutes=self.check_interval,
            id='position_monitor'
        )
        
        # Schedule daily summary
        self.scheduler.add_job(
            self._send_daily_summary,
            'cron',
            hour=18,  # 6 PM
            minute=0,
            id='daily_summary'
        )
        
        self.scheduler.start()
        
        # 加载保存的风险规则
        csv_path = os.path.join("data", "risk_rules.csv")
        self.load_risk_rules_from_csv(csv_path)
        
        # 加载当天的自动下单记录（统一到 data/orders.csv，兼容旧版 data/auto_orders.csv）
        auto_orders_path = os.path.join("data", "orders.csv")
        legacy_auto_orders_path = os.path.join("data", "auto_orders.csv")
        if os.path.exists(auto_orders_path):
            self.load_auto_orders_from_csv(auto_orders_path)
        elif os.path.exists(legacy_auto_orders_path):
            # 兼容迁移：先加载旧文件，再立刻写入新文件
            self.load_auto_orders_from_csv(legacy_auto_orders_path)
            self.save_auto_orders_to_csv(auto_orders_path)
            logger.info("Migrated legacy auto_orders.csv to orders.csv")
        else:
            logger.info("No existing orders CSV found to load")
        
        logger.info("Risk Manager started")
    
    def stop(self):
        """停止风险管理器"""
        self.processing_orders = False
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("Risk Manager stopped")
    
    def add_risk_rule(self, symbol: str, stop_loss_price: Optional[float] = None, 
                      take_profit_price: Optional[float] = None, max_loss_percent: Optional[float] = None):
        """添加风险管理规则"""
        self.risk_rules[symbol] = RiskRule(
            symbol=symbol,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            max_loss_percent=max_loss_percent
        )
        
        # 保存到CSV文件
        csv_path = os.path.join("data", "risk_rules.csv")
        self.save_risk_rules_to_csv(csv_path)
        
        logger.info(f"Risk rule added for {symbol}")
    
    def remove_risk_rule(self, symbol: str):
        """移除风险管理规则"""
        if symbol in self.risk_rules:
            del self.risk_rules[symbol]
            # Clear pending limit orders for this symbol
            if symbol in self.pending_limit_orders:
                del self.pending_limit_orders[symbol]
            
            # 保存到CSV文件
            csv_path = os.path.join("data", "risk_rules.csv")
            self.save_risk_rules_to_csv(csv_path)
            
            logger.info(f"Risk rule removed for {symbol}")
    
    def update_risk_rule(self, symbol: str, stop_loss_price: float = None, 
                         take_profit_price: float = None, enabled: bool = True) -> bool:
        """更新风险规则"""
        if symbol in self.risk_rules:
            rule = self.risk_rules[symbol]
            if stop_loss_price is not None:
                rule.stop_loss_price = stop_loss_price
            if take_profit_price is not None:
                rule.take_profit_price = take_profit_price
            rule.enabled = enabled
            
            # Reset pending limit orders when rule is updated
            if symbol in self.pending_limit_orders:
                self.pending_limit_orders[symbol] = {"stop_loss": False, "take_profit": False}
            
            # 保存到CSV文件
            csv_path = os.path.join("data", "risk_rules.csv")
            self.save_risk_rules_to_csv(csv_path)
            
            logger.info(f"Risk rule updated for {symbol}: SL=${stop_loss_price}, TP=${take_profit_price}")
            return True
        return False
    
    def get_risk_rules(self) -> Dict[str, RiskRule]:
        """获取所有风险管理规则"""
        return self.risk_rules.copy()
    
    async def _monitor_positions(self):
        """监控持仓风险"""
        try:
            if not self.ibkr_client.connected:
                logger.warning("IBKR not connected, skipping position monitoring")
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
            self.save_risk_rules_to_csv(os.path.join("data", "risk_rules.csv"))
            return
        
        logger.info(f"Checking risk rule for {symbol}: SL=${rule.stop_loss_price}, TP=${rule.take_profit_price}")
        
        # Initialize pending orders tracking for this symbol
        if symbol not in self.pending_limit_orders:
            self.pending_limit_orders[symbol] = {"stop_loss": False, "take_profit": False}
        
        # Calculate price threshold based on leverage
        threshold = self.price_threshold
        if symbol == 'MNQ':
            threshold *= self.leverage_mnq
        
        # Check stop loss
        if rule.stop_loss_price:
            price_diff = abs(current_price - rule.stop_loss_price) / rule.stop_loss_price
            logger.info(f"Stop loss check for {symbol}: current=${current_price:.2f}, target=${rule.stop_loss_price:.2f}, diff={price_diff:.4f}, threshold={threshold:.4f}")
            
            # If price is very close to stop loss, place limit order
            if price_diff <= threshold and not self.pending_limit_orders[symbol]["stop_loss"]:
                logger.info(f"Price close to stop loss for {symbol}, placing limit order")
                try:
                    action = 'SELL' if position_qty > 0 else 'BUY'
                    trade = self.ibkr_client.place_limit_order(
                        symbol=symbol,
                        quantity=abs(position_qty),
                        price=rule.stop_loss_price,
                        action=action
                    )
                    
                    if trade:
                        self.pending_limit_orders[symbol]["stop_loss"] = True
                        logger.info(f"✅ Limit order placed for stop loss: {symbol} at ${rule.stop_loss_price:.2f}")
                        
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
                except Exception as e:
                    logger.error(f"Failed to place stop loss limit order for {symbol}: {e}")
            elif self.pending_limit_orders[symbol]["stop_loss"]:
                logger.debug(f"Stop loss limit order already pending for {symbol}")
            
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
                
                # Disable rule after trigger and save
                rule.enabled = False
                self.save_risk_rules_to_csv(os.path.join("data", "risk_rules.csv"))
                logger.info(f"Stop loss order queued for {symbol} at ${current_price:.2f}, rule disabled")
        
        # Check take profit
        if rule.take_profit_price:
            price_diff = abs(current_price - rule.take_profit_price) / rule.take_profit_price
            logger.info(f"Take profit check for {symbol}: current=${current_price:.2f}, target=${rule.take_profit_price:.2f}, diff={price_diff:.4f}, threshold={threshold:.4f}")
            
            # If price is very close to take profit, place limit order
            if price_diff <= threshold and not self.pending_limit_orders[symbol]["take_profit"]:
                logger.info(f"Price close to take profit for {symbol}, placing limit order")
                try:
                    action = 'SELL' if position_qty > 0 else 'BUY'
                    trade = self.ibkr_client.place_limit_order(
                        symbol=symbol,
                        quantity=abs(position_qty),
                        price=rule.take_profit_price,
                        action=action
                    )
                    
                    if trade:
                        self.pending_limit_orders[symbol]["take_profit"] = True
                        logger.info(f"✅ Limit order placed for take profit: {symbol} at ${rule.take_profit_price:.2f}")
                        
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
                except Exception as e:
                    logger.error(f"Failed to place take profit limit order for {symbol}: {e}")
            elif self.pending_limit_orders[symbol]["take_profit"]:
                logger.debug(f"Take profit limit order already pending for {symbol}")
            
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
                
                # Disable rule after trigger and save
                rule.enabled = False
                self.save_risk_rules_to_csv(os.path.join("data", "risk_rules.csv"))
                logger.info(f"Take profit order queued for {symbol} at ${current_price:.2f}, rule disabled")
    
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
        try:
            if not self.ibkr_client.connected:
                logger.error("Cannot execute order: IBKR not connected")
                return
            
            # Determine order action
            positions = self.ibkr_client.get_positions()
            position = next((p for p in positions if p['symbol'] == order_request.symbol), None)
            
            if not position or position['position'] == 0:
                logger.warning(f"No position found for {order_request.symbol}, skipping order")
                return
            
            # Determine action based on current position
            action = 'SELL' if position['position'] > 0 else 'BUY'
            
            # Place market order for immediate execution
            trade = self.ibkr_client.place_market_order(
                symbol=order_request.symbol,
                quantity=order_request.quantity,
                action=action
            )
            
            if trade:
                order_id = trade.order.orderId
                
                # Record auto order
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
                    order_source="auto"
                )
                
                self.auto_orders[str(order_id)] = auto_order
                self.save_auto_orders_to_csv(os.path.join("data", "orders.csv"))
                
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
            if not self.ibkr_client.connected:
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
    
    def save_risk_rules_to_csv(self, filename: str = "risk_rules.csv"):
        """保存风险规则到CSV文件"""
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
        """从CSV文件加载风险规则"""
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
        """保存订单记录到CSV文件（包含自动和手动订单）"""
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
        """从CSV文件加载订单记录（包含自动和手动订单）"""
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

    def remove_order_record(self, order_id: str):
        """从记录中删除订单"""
        try:
            if order_id in self.auto_orders:
                del self.auto_orders[order_id]
                self.save_auto_orders_to_csv(os.path.join("data", "orders.csv"))
                logger.info(f"Order {order_id} removed from records")
                return True
            else:
                logger.warning(f"Order {order_id} not found in records")
                return False
        except Exception as e:
            logger.error(f"Error removing order {order_id}: {e}")
            return False

    def update_order_status(self, order_id: str, new_status: str):
        """更新订单状态"""
        try:
            if order_id in self.auto_orders:
                self.auto_orders[order_id].status = new_status
                if new_status in ['Filled', 'Cancelled']:
                    self.auto_orders[order_id].executed_at = datetime.now()
                self.save_auto_orders_to_csv(os.path.join("data", "orders.csv"))
                logger.info(f"Order {order_id} status updated to {new_status}")
                return True
            else:
                logger.warning(f"Order {order_id} not found in records for status update")
                return False
        except Exception as e:
            logger.error(f"Error updating order {order_id} status: {e}")
            return False