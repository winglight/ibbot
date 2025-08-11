import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import pandas as pd
from ib_insync import IB, Stock, Future, Order, MarketOrder, LimitOrder, Contract
from loguru import logger
import time
import threading
import asyncio
import os
import csv
from concurrent.futures import ThreadPoolExecutor
from connection_manager import connection_manager, ConnectionConfig

class IBKRClient:
    def __init__(self):
        self.connection_manager = connection_manager
        self.order_backup_file = "data/orders_backup.csv"
        self._ensure_backup_directory()
    
    @property
    def connected(self) -> bool:
        """检查连接状态 - 为兼容性提供connected属性"""
        return self.connection_manager.is_connected()
        
    def connect(self) -> bool:
        """连接到IBKR"""
        return self.connection_manager.connect()
    
    def disconnect(self):
        """断开连接"""
        self.connection_manager.disconnect()
    
    def is_connected(self) -> bool:
        """检查连接状态"""
        return self.connection_manager.is_connected()
    
    @property
    def ib(self) -> Optional[IB]:
        """获取IB实例"""
        return self.connection_manager.get_ib_instance()
    
    def _place_order_in_thread(self, ib, symbol: str, quantity: int, action: str, 
                              exchange: str, sec_type: str, order_type: str, price: float = None):
        """在连接管理器的事件循环中执行下单操作"""
        def order_worker():
            try:
                # 获取连接管理器的事件循环
                loop = self.connection_manager._loop
                if not loop or loop.is_closed():
                    logger.error("No active event loop for IBKR connection")
                    return None
                
                # 在连接的事件循环中执行下单
                asyncio.set_event_loop(loop)
                
                # 根据证券类型获取正确的合约
                if sec_type == 'FUT' or symbol in ['MNQ', 'NQ', 'ES', 'MES']:
                    contract = self.get_future_contract(symbol, exchange)
                else:
                    contract = self.get_stock_contract(symbol, exchange)

                # 使用同步方法确认合约和下单
                qualified_contracts = ib.qualifyContracts(contract)
                if not qualified_contracts:
                    logger.error(f"Failed to qualify contract for {symbol}")
                    return None
                
                # 使用确认后的合约
                qualified_contract = qualified_contracts[0]
                
                # 创建订单
                if order_type == 'MARKET':
                    order = MarketOrder(action, quantity)
                else:  # LIMIT
                    order = LimitOrder(action, quantity, price)
                
                trade = ib.placeOrder(contract, order)
                logger.info(f"{order_type} order placed: {action} {quantity} {symbol} ({sec_type})")
                return trade
            except Exception as e:
                logger.error(f"Failed to place {order_type.lower()} order in thread: {e}")
                return None
        
        # 使用线程池执行
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(order_worker)
            return future.result(timeout=30)  # 30秒超时
    
    def get_stock_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD') -> Stock:
        """获取股票合约"""
        return Stock(symbol, exchange, currency)
    
    def get_future_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD'):
        """获取期货合约"""
        from datetime import datetime, timedelta
        
        # 为主要期货合约生成正确的expiry格式
        if symbol in ['MNQ', 'NQ', 'ES', 'MES']:
            # 获取当前日期
            now = datetime.now()
            
            # 找到下一个季度合约月份
            current_month = now.month
            current_year = now.year
            
            # 确定下一个合约月份
            if current_month <= 3:
                contract_month = 3
                contract_year = current_year
            elif current_month <= 6:
                contract_month = 6
                contract_year = current_year
            elif current_month <= 9:
                contract_month = 9
                contract_year = current_year
            else:
                contract_month = 12
                contract_year = current_year
            
            # 如果当前月份已经接近合约到期（比如在合约月份的第三周后），使用下一个合约
            if current_month == contract_month and now.day > 15:
                if contract_month == 12:
                    contract_month = 3
                    contract_year += 1
                elif contract_month == 9:
                    contract_month = 12
                elif contract_month == 6:
                    contract_month = 9
                elif contract_month == 3:
                    contract_month = 6
            
            # 使用YYYYMM格式，与test.py保持一致
            expiry = f"{contract_year}{contract_month:02d}"
            
            # 为不同的期货合约设置正确的交易所和细节
            contract = Future(symbol=symbol, exchange='CME', currency=currency)
            contract.lastTradeDateOrContractMonth = expiry
            
            # 为MNQ等微型期货设置正确的乘数和交易类别
            if symbol == 'MNQ':
                contract.multiplier = '2'
                contract.tradingClass = 'MNQ'
            elif symbol == 'MES':
                contract.multiplier = '5'
                contract.tradingClass = 'MES'
            elif symbol == 'NQ':
                contract.multiplier = '20'
                contract.tradingClass = 'NQ'
            elif symbol == 'ES':
                contract.multiplier = '50'
                contract.tradingClass = 'ES'
            
            return contract
        else:
            return Future(symbol=symbol, exchange=exchange, currency=currency)
    
    def get_contract(self, symbol: str, sec_type: str = 'STK', exchange: str = 'SMART', currency: str = 'USD'):
        """根据证券类型获取合约"""
        if sec_type == 'FUT':
            return self.get_future_contract(symbol, exchange, currency)
        else:
            return self.get_stock_contract(symbol, exchange, currency)
    
    def get_historical_data(self, symbol: str, duration: str = '1 Y',
                                bar_size: str = '1 day', exchange: str = 'SMART') -> pd.DataFrame:
        """获取历史数据"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for historical data: {symbol}")
                    return pd.DataFrame()
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for historical data: {symbol}")
                return pd.DataFrame()
            
            # MNQ是期货合约
            if symbol == 'MNQ':
                contract = self.get_future_contract(symbol, 'CME')
            else:
                contract = self.get_stock_contract(symbol, exchange)
            
            # 直接使用ib_insync的同步调用，它内部会处理异步
            bars = ib.reqHistoricalData(
                contract, endDateTime='', durationStr=duration,
                barSizeSetting=bar_size, whatToShow='TRADES', useRTH=True
            )
            
            if not bars:
                logger.warning(f"No historical data returned for {symbol}")
                return pd.DataFrame()
            
            data = []
            for bar in bars:
                data.append({
                    'date': bar.date,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                })
            
            df = pd.DataFrame(data)
            logger.info(f"Retrieved {len(df)} historical data points for {symbol}")
            return df
        except Exception as e:
            logger.error(f"Failed to get historical data for {symbol}: {e}")
            return pd.DataFrame()
    
    def _ensure_backup_directory(self):
        """确保备份目录存在"""
        os.makedirs(os.path.dirname(self.order_backup_file), exist_ok=True)
    
    def _backup_order(self, symbol: str, quantity: int, action: str, order_type: str, price: float = None, sec_type: str = 'STK', exchange: str = 'SMART'):
        """备份订单信息到CSV文件"""
        try:
            order_data = {
                'timestamp': datetime.now().isoformat(),
                'symbol': symbol,
                'quantity': quantity,
                'action': action,
                'order_type': order_type,
                'price': price if price else '',
                'sec_type': sec_type,
                'exchange': exchange,
                'status': 'PENDING'
            }
            
            # 检查文件是否存在，如果不存在则创建并写入表头
            file_exists = os.path.exists(self.order_backup_file)
            
            with open(self.order_backup_file, 'a', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['timestamp', 'symbol', 'quantity', 'action', 'order_type', 'price', 'sec_type', 'exchange', 'status']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                if not file_exists:
                    writer.writeheader()
                
                writer.writerow(order_data)
            
            logger.info(f"Order backed up: {action} {quantity} {symbol} ({order_type})")
        except Exception as e:
            logger.error(f"Failed to backup order: {e}")
    
    def _update_order_status(self, symbol: str, action: str, quantity: int, status: str):
        """更新订单状态"""
        try:
            if not os.path.exists(self.order_backup_file):
                return
            
            # 读取现有数据
            rows = []
            with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                rows = list(reader)
            
            # 更新匹配的订单状态
            updated = False
            for row in reversed(rows):  # 从最新的订单开始查找
                if (row['symbol'] == symbol and 
                    row['action'] == action and 
                    int(row['quantity']) == quantity and 
                    row['status'] == 'PENDING'):
                    row['status'] = status
                    updated = True
                    break
            
            if updated:
                # 重写文件
                with open(self.order_backup_file, 'w', newline='', encoding='utf-8') as csvfile:
                    fieldnames = ['timestamp', 'symbol', 'quantity', 'action', 'order_type', 'price', 'sec_type', 'exchange', 'status']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                
                logger.info(f"Order status updated: {action} {quantity} {symbol} -> {status}")
        except Exception as e:
            logger.error(f"Failed to update order status: {e}")
    
    def get_order_backup_history(self, days: int = 7) -> List[Dict]:
        """获取订单备份历史"""
        try:
            if not os.path.exists(self.order_backup_file):
                return []
            
            cutoff_date = datetime.now() - timedelta(days=days)
            orders = []
            
            with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    order_time = datetime.fromisoformat(row['timestamp'])
                    if order_time >= cutoff_date:
                        orders.append(dict(row))
            
            return sorted(orders, key=lambda x: x['timestamp'], reverse=True)
        except Exception as e:
            logger.error(f"Failed to get order backup history: {e}")
            return []
    
    def restore_pending_orders(self) -> List[Dict]:
        """恢复待处理的订单"""
        try:
            if not os.path.exists(self.order_backup_file):
                return []
            
            pending_orders = []
            
            with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    if row['status'] == 'PENDING':
                        # 检查订单是否超过1小时（可能是系统重启前的订单）
                        order_time = datetime.fromisoformat(row['timestamp'])
                        if datetime.now() - order_time > timedelta(hours=1):
                            # 标记为过期
                            self._update_order_status(row['symbol'], row['action'], int(row['quantity']), 'EXPIRED')
                        else:
                            pending_orders.append(dict(row))
            
            return pending_orders
        except Exception as e:
            logger.error(f"Failed to restore pending orders: {e}")
            return []
    
    def save_historical_data(self, df: pd.DataFrame, symbol: str, filename: str = None):
        """保存历史数据到CSV文件"""
        if filename is None:
            filename = f"data/{symbol}_historical.csv"
        
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        df.to_csv(filename, index=False)
        logger.info(f"Historical data saved to {filename}")
    
    def get_real_time_data(self, symbol: str, exchange: str = 'SMART'):
        """获取实时数据"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for real-time data: {symbol}")
                    return None
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for real-time data: {symbol}")
                return None
            
            # MNQ是期货合约
            if symbol == 'MNQ':
                contract = self.get_future_contract(symbol, 'CME')
            else:
                contract = self.get_stock_contract(symbol, exchange)
            
            # 直接使用ib_insync的同步调用，它内部会处理异步
            ticker = ib.reqMktData(contract, '', False, False)
            
            if not ticker:
                logger.warning(f"No ticker returned for {symbol}")
                return None
            
            # 等待数据
            import time
            time.sleep(2)
            
            if ticker.last and ticker.last > 0:
                logger.info(f"Retrieved real-time data for {symbol}: last={ticker.last}")
                return {
                    'symbol': symbol,
                    'last': ticker.last,
                    'bid': ticker.bid if ticker.bid else 0,
                    'ask': ticker.ask if ticker.ask else 0,
                    'volume': ticker.volume if ticker.volume else 0,
                    'timestamp': datetime.now().isoformat()
                }
            else:
                logger.warning(f"No valid price data for {symbol}: last={ticker.last}")
                return None
        except Exception as e:
            logger.error(f"Failed to get real-time data for {symbol}: {e}")
            return None
    
    def place_market_order(self, symbol: str, quantity: int,
                               action: str = 'BUY', exchange: str = 'SMART', sec_type: str = 'STK') -> Optional[Order]:
        """下市价单"""
        try:
            # 备份订单
            self._backup_order(symbol, quantity, action, 'MARKET', sec_type=sec_type, exchange=exchange)
            
            # 确保连接
            if not self.is_connected():
                logger.warning(f"Not connected to IBKR, attempting to reconnect...")
                if not self.connect():
                    logger.error(f"Failed to reconnect to IBKR for market order")
                    self._update_order_status(symbol, action, quantity, 'FAILED')
                    return None
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.warning(f"IBKR connection lost, attempting to reconnect...")
                if not self.connect():
                    logger.error(f"Failed to reconnect to IBKR for market order")
                    self._update_order_status(symbol, action, quantity, 'FAILED')
                    return None
                ib = self.ib
            
            # 在独立线程中执行下单操作
            result = self._place_order_in_thread(ib, symbol, quantity, action, exchange, sec_type, 'MARKET')
            
            # 更新订单状态
            if result:
                self._update_order_status(symbol, action, quantity, 'SUBMITTED')
            else:
                self._update_order_status(symbol, action, quantity, 'FAILED')
            
            return result
        except Exception as e:
            logger.error(f"Failed to place market order: {e}")
            self._update_order_status(symbol, action, quantity, 'FAILED')
            return None
    
    def place_limit_order(self, symbol: str, quantity: int, price: float,
                              action: str = 'BUY', exchange: str = 'SMART', sec_type: str = 'STK') -> Optional[Order]:
        """下限价单"""
        try:
            # 备份订单
            self._backup_order(symbol, quantity, action, 'LIMIT', price, sec_type=sec_type, exchange=exchange)
            
            # 确保连接
            if not self.is_connected():
                logger.warning(f"Not connected to IBKR, attempting to reconnect...")
                if not self.connect():
                    logger.error(f"Failed to reconnect to IBKR for limit order: {symbol}")
                    self._update_order_status(symbol, action, quantity, 'FAILED')
                    return None
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.warning(f"IBKR connection lost, attempting to reconnect...")
                if not self.connect():
                    logger.error(f"Failed to reconnect to IBKR for limit order")
                    self._update_order_status(symbol, action, quantity, 'FAILED')
                    return None
                ib = self.ib
            
            # 在独立线程中执行下单操作
            result = self._place_order_in_thread(ib, symbol, quantity, action, exchange, sec_type, 'LIMIT', price)
            
            # 更新订单状态
            if result:
                self._update_order_status(symbol, action, quantity, 'SUBMITTED')
            else:
                self._update_order_status(symbol, action, quantity, 'FAILED')
            
            return result
        except Exception as e:
            logger.error(f"Failed to place limit order: {e}")
            self._update_order_status(symbol, action, quantity, 'FAILED')
            return None
    
    def cancel_order(self, order_id: int) -> tuple[bool, str]:
        """取消指定订单"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for cancel order: {order_id}")
                    return False
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for cancel order")
                return False
            
            trades = ib.trades()
            target_trade = None
            for trade in trades:
                if trade.order.orderId == order_id:
                    target_trade = trade
                    break
            
            if not target_trade:
                logger.warning(f"Order {order_id} not found in active trades")
                return False, "Order not found"
            
            current_status = target_trade.orderStatus.status
            logger.info(f"Order {order_id} current status: {current_status}")
            
            # 检查订单状态是否可以取消
            if current_status in ['Filled', 'Cancelled', 'Inactive']:
                logger.warning(f"Order {order_id} cannot be cancelled, status: {current_status}")
                return False, f"Order cannot be cancelled, current status: {current_status}"
            
            # 如果已经是PendingCancel状态，直接等待确认
            if current_status == 'PendingCancel':
                logger.info(f"Order {order_id} already in PendingCancel status, waiting for confirmation...")
            else:
                # 发送取消请求
                logger.info(f"Sending cancel request for order {order_id}")
                ib.cancelOrder(target_trade.order)
            
            # 等待取消确认，增加等待时间到15秒以应对Paper Trading延迟
            import time
            max_wait_time = 15
            start_time = time.time()
            check_interval = 0.5  # 减少检查频率以避免过度查询
            
            while time.time() - start_time < max_wait_time:
                time.sleep(check_interval)
                # 重新获取订单状态
                updated_trades = ib.trades()
                for updated_trade in updated_trades:
                    if updated_trade.order.orderId == order_id:
                        new_status = updated_trade.orderStatus.status
                        if new_status in ['Cancelled', 'Inactive']:
                            logger.info(f"Order {order_id} successfully cancelled, final status: {new_status}")
                            return True, f"Order cancelled successfully, status: {new_status}"
                        elif new_status == 'PendingCancel':
                            # 继续等待，但记录等待时间
                            elapsed = time.time() - start_time
                            logger.info(f"Order {order_id} still pending cancellation... (elapsed: {elapsed:.1f}s)")
                            continue
                        else:
                            # 状态变化，记录日志
                            if new_status != current_status:
                                logger.info(f"Order {order_id} status changed from {current_status} to {new_status}")
                                current_status = new_status
                            break
                # 如果订单不在trades中了，需要再次确认
                else:
                    logger.debug(f"Order {order_id} not found in current trades, rechecking...")
                    # 不立即返回True，继续等待以确认状态
            
            # 超时，检查最终状态
            final_trades = ib.trades()
            for final_trade in final_trades:
                if final_trade.order.orderId == order_id:
                    final_status = final_trade.orderStatus.status
                    if final_status in ['Cancelled', 'Inactive']:
                        logger.info(f"Order {order_id} finally cancelled, status: {final_status}")
                        return True, f"Order cancelled successfully, status: {final_status}"
                    elif final_status == 'PendingCancel':
                        # Paper Trading环境中PendingCancel可能持续很久，但实际上订单已经被标记为取消
                        logger.warning(f"Order {order_id} still in PendingCancel after {max_wait_time}s - this may be normal in Paper Trading")
                        return False, f"Order cancellation pending (Paper Trading delay), status: {final_status}"
                    else:
                        logger.warning(f"Order {order_id} cancellation timeout, final status: {final_status}")
                        return False, f"Cancellation timeout, order status: {final_status}"
            
            # 订单不在列表中，在Paper Trading中这通常意味着已取消
            logger.info(f"Order {order_id} no longer in trades list after timeout - likely cancelled in Paper Trading")
            return True, "Order likely cancelled (no longer in active trades)"
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False, f"Error: {str(e)}"
    
    def cancel_all_orders(self) -> int:
        """取消所有订单"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for cancel all orders")
                    return 0
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for cancel all orders")
                return 0
            
            trades = ib.trades()
            cancelled_count = 0
            for trade in trades:
                if trade.orderStatus.status in ['Submitted', 'PreSubmitted']:
                    ib.cancelOrder(trade.order)
                    cancelled_count += 1
            logger.info(f"Cancelled {cancelled_count} orders")
            return cancelled_count
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return 0
    
    def get_orders(self, risk_manager=None) -> List[Dict]:
        """获取订单列表"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for get orders")
                    return []
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for get orders")
                return []
            
            trades = ib.trades()
            orders = []
            for trade in trades:
                # 从risk_manager的auto_orders中获取订单来源信息
                order_source = "未知"
                if risk_manager and hasattr(risk_manager, 'auto_orders'):
                    order_id_str = str(trade.order.orderId)
                    if order_id_str in risk_manager.auto_orders:
                        auto_order = risk_manager.auto_orders[order_id_str]
                        if auto_order.order_source == "manual":
                            order_source = "人工"
                        elif auto_order.order_source == "auto":
                            order_source = "机器人"
                        else:
                            order_source = auto_order.order_source
                    else:
                        # 如果在auto_orders中找不到记录，可能是历史订单，默认为人工
                        order_source = "人工"
                else:
                    # 如果没有risk_manager，默认为人工
                    order_source = "人工"
                
                orders.append({
                    'order_id': trade.order.orderId,
                    'symbol': trade.contract.symbol,
                    'action': trade.order.action,
                    'quantity': trade.order.totalQuantity,
                    'order_type': trade.order.orderType,
                    'limit_price': getattr(trade.order, 'lmtPrice', None),
                    'status': trade.orderStatus.status,
                    'filled': trade.orderStatus.filled,
                    'remaining': trade.orderStatus.remaining,
                    'avg_fill_price': trade.orderStatus.avgFillPrice,
                    'submit_time': trade.log[0].time if trade.log else None,
                    'order_source': order_source
                })
            return orders
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []
    
    def get_account_info(self) -> Dict:
        """获取账户信息"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for get account info")
                    return {}
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for get account info")
                return {}
            
            # Use synchronous methods to avoid event loop issues
            account_values = ib.accountValues()
            
            info = {
                'account_id': None,
                'account_type': 'Paper' if connection_manager.config.paper_trading else 'Live',
                'total_cash': 0,
                'available_funds': 0,
                'buying_power': 0,
                'net_liquidation': 0,
                'unrealized_pnl': 0,
                'realized_pnl': 0
            }
            
            # Get account ID from account values
            for item in account_values:
                if item.tag == 'AccountCode':
                    info['account_id'] = item.value
                elif item.tag == 'TotalCashValue' and item.currency == 'USD':
                    info['total_cash'] = float(item.value)
                elif item.tag == 'AvailableFunds' and item.currency == 'USD':
                    info['available_funds'] = float(item.value)
                elif item.tag == 'BuyingPower' and item.currency == 'USD':
                    info['buying_power'] = float(item.value)
                elif item.tag == 'NetLiquidation' and item.currency == 'USD':
                    info['net_liquidation'] = float(item.value)
                elif item.tag == 'UnrealizedPnL' and item.currency == 'USD':
                    info['unrealized_pnl'] = float(item.value)
                elif item.tag == 'RealizedPnL' and item.currency == 'USD':
                    info['realized_pnl'] = float(item.value)
            
            return info
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return {}
    
    def get_positions(self) -> List[Dict]:
        """获取持仓列表"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for get positions")
                    return []
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for get positions")
                return []
            
            # Get portfolio data directly without forcing account updates
            # to avoid event loop conflicts
            
            # Use portfolio() instead of positions() to get real-time market prices
            portfolio_items = ib.portfolio()
            position_list = []
            
            for item in portfolio_items:
                # Show all portfolio items to match TWS data
                # Get real-time market price from portfolio item
                current_price = item.marketPrice if item.marketPrice and item.marketPrice == item.marketPrice else item.averageCost
                price_source = "marketPrice" if item.marketPrice and item.marketPrice == item.marketPrice else "averageCost"
                
                logger.info(f"Portfolio data for {item.contract.symbol}: marketPrice={item.marketPrice}, averageCost={item.averageCost}, position={item.position}")
                logger.info(f"Price for {item.contract.symbol}: {current_price} (source: {price_source})")
                
                # Adjust average cost for specific symbols like MNQ
                avg_cost_adjusted = item.averageCost
                if item.contract.symbol == 'MNQ':
                    avg_cost_adjusted = item.averageCost / 2

                market_value = item.position * current_price
                
                # For futures contracts like MNQ, calculate PnL based on actual cost basis
                if item.contract.symbol == 'MNQ':
                    # Use the actual unrealized PnL from IBKR which accounts for leverage
                    unrealized_pnl = item.unrealizedPNL
                    # Calculate percentage based on actual cost basis (margin requirement)
                    actual_cost_basis = abs(item.position) * avg_cost_adjusted
                    unrealized_pnl_percent = (unrealized_pnl / actual_cost_basis) * 100 if actual_cost_basis else 0
                else:
                    # For stocks, use traditional calculation
                    unrealized_pnl = item.position * (current_price - avg_cost_adjusted)
                    unrealized_pnl_percent = (unrealized_pnl / (abs(item.position) * avg_cost_adjusted)) * 100 if avg_cost_adjusted else 0

                position_list.append({
                    'symbol': item.contract.symbol,
                    'exchange': item.contract.exchange,
                    'position': item.position,
                    'avg_cost': avg_cost_adjusted,
                    'current_price': current_price,
                    'market_value': market_value,
                    'unrealized_pnl': unrealized_pnl,
                    'unrealized_pnl_percent': unrealized_pnl_percent
                })
            
            return position_list
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

if __name__ == "__main__":
    """主函数，用于调试下单方法"""
    print("Starting IBKR Client Debug...")
    
    # 创建客户端实例
    client = IBKRClient()
    
    try:
        # 自动连接到IB
        print("Connecting to IBKR...")
        if client.connect():
            print("Successfully connected to IBKR")
            
            # 获取账户信息
            print("\nGetting account info...")
            account_info = client.get_account_info()
            print(f"Account ID: {account_info.get('account_id')}")
            print(f"Account Type: {account_info.get('account_type')}")
            print(f"Available Funds: ${account_info.get('available_funds', 0):,.2f}")
            print(f"Buying Power: ${account_info.get('buying_power', 0):,.2f}")
            
            # 获取当前持仓
            print("\nGetting positions...")
            positions = client.get_positions()
            if positions:
                for pos in positions:
                    print(f"Position: {pos['symbol']} - {pos['position']} shares @ ${pos['avg_cost']:.2f}")
            else:
                print("No positions found")
            
            # 获取当前订单
            print("\nGetting orders...")
            orders = client.get_orders()
            if orders:
                for order in orders:
                    print(f"Order: {order['order_id']} - {order['action']} {order['quantity']} {order['symbol']} @ {order.get('limit_price', 'Market')} - Status: {order['status']}")
            else:
                print("No active orders found")
            
            # 测试下单功能（注释掉以避免意外下单）
            print("\n=== Order Testing (Commented Out) ===")
            print("# Test market order:")
            print("# trade = client.place_market_order('AAPL', 1, 'BUY')")
            # trade = client.place_market_order('MNQ', 1, 'BUY')
            print("# if trade:")
            print("#     print(f'Market order placed: {trade.order.orderId}')")
            print("# else:")
            print("#     print('Failed to place market order')")
            
            print("\n# Test limit order:")
            print("# trade = client.place_limit_order('AAPL', 1, 150.0, 'BUY')")
            trade = client.place_limit_order('MNQ', 1, 23360, 'BUY')
            print("# if trade:")
            print("#     print(f'Limit order placed: {trade.order.orderId}')")
            print("# else:")
            print("#     print('Failed to place limit order')")
            
            print("\n# Test future order (MNQ):")
            print("# trade = client.place_market_order('MNQ', 1, 'BUY', sec_type='FUT')")
            print("# if trade:")
            print("#     print(f'Future order placed: {trade.order.orderId}')")
            print("# else:")
            print("#     print('Failed to place future order')")
            
        else:
            print("Failed to connect to IBKR")
            
    except Exception as e:
        print(f"Error during debug: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 断开连接
        print("\nDisconnecting...")
        client.disconnect()
        print("Debug session completed")