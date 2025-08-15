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
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from .connection_manager import connection_manager, ConnectionConfig
from .database import get_db_manager

class IBKRClient:
    def __init__(self):
        self.connection_manager = connection_manager
        self.order_backup_file = "data/orders_backup.csv"
        self._ensure_backup_directory()
        # Price cache to track real-time prices
        self.price_cache = {}
        self.price_cache_timestamp = {}
        # Cache TTL seconds (used to decide when to refresh real-time data)
        self.price_cache_ttl_seconds = 60  # 增加到60秒，减少频繁请求
    
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
        try:
            # 获取IB实例和事件循环
            ib_instance = self.connection_manager.get_ib_instance()
            loop = getattr(self.connection_manager, '_loop', None)
            
            if not ib_instance:
                logger.error("No active IB instance for placing order")
                return None
                
            if not loop or loop.is_closed():
                logger.error("No active event loop for IBKR connection")
                return None
            
            # 在连接管理器的事件循环中异步执行下单
            async def execute_order_async():
                try:
                    # 根据证券类型获取正确的合约
                    if sec_type == 'FUT' or symbol in ['MNQ', 'NQ', 'ES', 'MES']:
                        contract = self.get_future_contract(symbol, exchange)
                    else:
                        contract = self.get_stock_contract(symbol, exchange)

                    # 使用异步方法确认合约
                    qualified_contracts = await ib_instance.qualifyContractsAsync(contract)
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
                    
                    # 使用确认后的合约提交订单
                    trade = ib_instance.placeOrder(qualified_contract, order)
                    logger.info(f"{order_type} order placed: {action} {quantity} {symbol} ({sec_type}) on {qualified_contract.exchange}, conId={getattr(qualified_contract, 'conId', 'N/A')}")
                    return trade
                except Exception as e:
                    logger.error(f"Failed to place order in async execution: {e}")
                    return None
            
            # 在连接管理器的事件循环中运行异步下单
            future = asyncio.run_coroutine_threadsafe(execute_order_async(), loop)
            return future.result(timeout=30)  # 30秒超时
            
        except Exception as e:
            logger.error(f"Failed to place {order_type.lower()} order in thread: {e}")
            return None
    
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
        # 注释掉备份功能
        # os.makedirs(os.path.dirname(self.order_backup_file), exist_ok=True)
        pass
    
    def _backup_order(self, symbol: str, quantity: int, action: str, order_type: str, price: float = None, sec_type: str = 'STK', exchange: str = 'SMART'):
        """备份订单信息到CSV文件（已禁用）"""
        # 注释掉备份功能，不再创建备份文件
        pass
        # try:
        #     self._ensure_backup_directory()
        #     
        #     file_exists = os.path.exists(self.order_backup_file)
        #     
        #     with open(self.order_backup_file, 'a', newline='', encoding='utf-8') as csvfile:
        #         fieldnames = ['timestamp', 'symbol', 'quantity', 'action', 'order_type', 'price', 'sec_type', 'exchange', 'status']
        #         writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        #         
        #         if not file_exists:
        #             writer.writeheader()
        #         
        #         writer.writerow({
        #             'timestamp': datetime.now().isoformat(),
        #             'symbol': symbol,
        #             'quantity': quantity,
        #             'action': action,
        #             'order_type': order_type,
        #             'price': price,
        #             'sec_type': sec_type,
        #             'exchange': exchange,
        #             'status': 'PENDING'
        #         })
        # except Exception as e:
        #     logger.error(f"Error backing up order: {e}")

    def _update_order_status(self, symbol: str, action: str, quantity: int, status: str):
        """更新备份文件中的订单状态（已禁用）"""
        # 注释掉备份功能
        pass
        # if not os.path.exists(self.order_backup_file):
        #     return
        #     
        # try:
        #     rows = []
        #     with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
        #         reader = csv.DictReader(csvfile)
        #         for row in reader:
        #             if (row['symbol'] == symbol and 
        #                 row['action'] == action and 
        #                 int(row['quantity']) == quantity and 
        #                 row['status'] == 'PENDING'):
        #                 row['status'] = status
        #             rows.append(row)
        #     
        #     with open(self.order_backup_file, 'w', newline='', encoding='utf-8') as csvfile:
        #         if rows:
        #             fieldnames = rows[0].keys()
        #             writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        #             writer.writeheader()
        #             writer.writerows(rows)
        # except Exception as e:
        #     logger.error(f"Error updating order status: {e}")

    def get_order_backup_history(self, days: int = 7) -> List[Dict]:
        """获取订单备份历史（已禁用）"""
        # 注释掉备份功能，返回空列表
        return []
        # try:
        #     if not os.path.exists(self.order_backup_file):
        #         return []
        #     
        #     cutoff_date = datetime.now() - timedelta(days=days)
        #     orders = []
        #     
        #     with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
        #         reader = csv.DictReader(csvfile)
        #         for row in reader:
        #             order_date = datetime.fromisoformat(row['timestamp'])
        #             if order_date >= cutoff_date:
        #                 orders.append(row)
        #     
        #     return orders
        # except Exception as e:
        #     logger.error(f"Error reading backup history: {e}")
        #     return []

    def restore_pending_orders(self) -> List[Dict]:
        """恢复待处理的订单（已禁用）"""
        # 注释掉备份功能，返回空列表
        return []
        # try:
        #     if not os.path.exists(self.order_backup_file):
        #         return []
        #     
        #     pending_orders = []
        #     
        #     with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
        #         reader = csv.DictReader(csvfile)
        #         for row in reader:
        #             if row['status'] == 'PENDING':
        #                 pending_orders.append(row)
        #                 # 标记为过期
        #                 self._update_order_status(row['symbol'], row['action'], int(row['quantity']), 'EXPIRED')
        #     
        #     return pending_orders
        # except Exception as e:
        #     logger.error(f"Error restoring pending orders: {e}")
        #     return []

    def save_historical_data(self, df: pd.DataFrame, symbol: str, filename: str = None):
        """保存历史数据到数据库"""
        try:
            # 获取数据库管理器
            db_manager = get_db_manager()
            
            # 保存到数据库
            db_manager.save_historical_data(symbol, df)
            logger.info(f"Historical data for {symbol} saved to database")
            
            # 保留文件保存作为备份（可选）
            if filename:
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                df.to_csv(filename, index=False)
                logger.info(f"Historical data also saved to {filename}")
        except Exception as e:
            logger.error(f"Error saving historical data to database: {e}")
            # 回退到文件保存
            if filename is None:
                filename = f"data/{symbol}_historical.csv"
            
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            df.to_csv(filename, index=False)
            logger.info(f"Historical data saved to {filename} (fallback)")
    
    def get_real_time_data(self, symbol: str, exchange: str = 'SMART'):
        """获取实时数据"""
        import threading
        current_thread = threading.current_thread()
        logger.info(f"get_real_time_data called for {symbol} in thread: {current_thread.name}")
        
        # 检测是否在ThreadPoolExecutor中运行
        is_in_thread_pool = 'ThreadPoolExecutor' in current_thread.name
        logger.info(f"Running in ThreadPoolExecutor: {is_in_thread_pool}")
        
        try:
            # 检查当前线程的事件循环状态
            try:
                loop = asyncio.get_running_loop()
                logger.info(f"Found running event loop in thread {current_thread.name}: {loop}")
            except RuntimeError as e:
                logger.info(f"No running event loop in thread {current_thread.name}: {e}")
                # 如果在ThreadPoolExecutor中且没有事件循环，这是正常的
                if is_in_thread_pool:
                    logger.info(f"ThreadPoolExecutor without event loop is expected for {symbol}")
            
            # 确保连接
            logger.debug(f"Checking IBKR connection for {symbol}")
            if not self.is_connected():
                logger.warning(f"IBKR not connected, attempting to connect for {symbol}")
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for real-time data: {symbol}")
                    return None
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for real-time data: {symbol}")
                return None
            
            logger.debug(f"IB instance status for {symbol}: connected={ib.isConnected()}")
            
            # MNQ是期货合约
            if symbol == 'MNQ':
                logger.debug(f"Creating future contract for {symbol}")
                contract = self.get_future_contract(symbol, 'CME')
            else:
                logger.debug(f"Creating stock contract for {symbol}")
                contract = self.get_stock_contract(symbol, exchange)
            
            logger.debug(f"Contract created for {symbol}: {contract}")
            
            # 在ThreadPoolExecutor中运行时，使用特殊的处理方式
            if is_in_thread_pool:
                logger.debug(f"Using ThreadPoolExecutor-safe approach for {symbol}")
                # 在ThreadPoolExecutor中，直接调用但忽略事件循环警告
                try:
                    # 临时抑制asyncio相关的警告
                    import warnings
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", message=".*no current event loop.*", category=RuntimeWarning)
                        warnings.filterwarnings("ignore", message=".*There is no current event loop.*", category=RuntimeWarning)
                        ticker = ib.reqMktData(contract, '', False, False)
                    logger.debug(f"ThreadPoolExecutor reqMktData successful for {symbol}")
                except Exception as thread_error:
                    logger.warning(f"ThreadPoolExecutor reqMktData failed for {symbol}: {thread_error}")
                    # 如果失败，返回None而不是继续尝试
                    return None
            else:
                logger.debug(f"Using normal approach for {symbol}")
                ticker = ib.reqMktData(contract, '', False, False)
            
            logger.debug(f"Market data request completed for {symbol}, ticker: {ticker}")
            
            if not ticker:
                logger.warning(f"No ticker returned for {symbol}")
                return None
            
            # 等待数据，根据运行环境调整等待时间
            import time
            # 在ThreadPoolExecutor中使用更短的等待时间
            max_wait = 3 if is_in_thread_pool else 8
            wait_interval = 0.5
            waited = 0
            logger.debug(f"Waiting for market data for {symbol}, max_wait={max_wait}s (ThreadPool: {is_in_thread_pool})")
            
            while waited < max_wait:
                time.sleep(wait_interval)
                waited += wait_interval
                
                # 检查是否获得有效价格
                if ticker.last and ticker.last > 0:
                    logger.info(f"Retrieved real-time data for {symbol}: last={ticker.last} (waited {waited:.1f}s)")
                    # update cache with longer TTL
                    self.price_cache[symbol] = ticker.last
                    self.price_cache_timestamp[symbol] = datetime.now()
                    return {
                        'symbol': symbol,
                        'last': ticker.last,
                        'bid': ticker.bid if ticker.bid else 0,
                        'ask': ticker.ask if ticker.ask else 0,
                        'volume': ticker.volume if ticker.volume else 0,
                        'timestamp': datetime.now().isoformat()
                    }
                
                # 检查是否有bid/ask数据
                if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    mid = (ticker.bid + ticker.ask) / 2
                    logger.info(f"Retrieved bid/ask mid for {symbol}: mid={mid:.2f} (bid={ticker.bid}, ask={ticker.ask}, waited {waited:.1f}s)")
                    self.price_cache[symbol] = mid
                    self.price_cache_timestamp[symbol] = datetime.now()
                    return {
                        'symbol': symbol,
                        'last': mid,
                        'bid': ticker.bid,
                        'ask': ticker.ask,
                        'volume': ticker.volume if ticker.volume else 0,
                        'timestamp': datetime.now().isoformat()
                    }
            
            # 如果等待后仍无有效数据，记录警告
            logger.warning(f"No valid price data for {symbol} after {max_wait}s: last={ticker.last}, bid={ticker.bid}, ask={ticker.ask}")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get real-time data for {symbol}: {e}")
            return None
    
    def place_market_order(self, symbol: str, quantity: int,
                               action: str = 'BUY', exchange: str = 'SMART', sec_type: str = 'STK') -> Optional[Order]:
        """下市价单"""
        # 下单前备份订单信息
        # self._backup_order(symbol, quantity, action, 'MARKET', sec_type=sec_type, exchange=exchange)
        
        try:
            trade = self._place_order_in_thread(
                None,  # will use self.ib
                symbol, quantity, action, exchange, sec_type, 'MARKET'
            )
            
            if trade:
                # 更新订单状态
                self._update_order_status(symbol, action, quantity, 'Submitted')
            
            return trade
        except Exception as e:
            logger.error(f"Failed to place market order: {e}")
            return None

    def place_limit_order(self, symbol: str, quantity: int, price: float,
                              action: str = 'BUY', exchange: str = 'SMART', sec_type: str = 'STK') -> Optional[Order]:
        """下限价单"""
        # 下单前备份订单信息
        # self._backup_order(symbol, quantity, action, 'LIMIT', price, sec_type=sec_type, exchange=exchange)
        
        try:
            trade = self._place_order_in_thread(
                None,  # will use self.ib
                symbol, quantity, action, exchange, sec_type, 'LIMIT', price
            )
            
            if trade:
                # 更新订单状态
                self._update_order_status(symbol, action, quantity, 'Submitted')
            
            return trade
        except Exception as e:
            logger.error(f"Failed to place limit order: {e}")
            return None
    
    def cancel_order(self, order_id: int) -> tuple[bool, str]:
        """取消指定订单"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for cancel order: {order_id}")
                    return False, "Connection failed"
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for cancel order")
                return False, "Connection lost"
            
            # 首先在连接管理器事件循环中强制刷新订单数据，避免事件循环冲突
            try:
                loop = getattr(self.connection_manager, '_loop', None)
                if loop and not loop.is_closed():
                    async def refresh_orders_async():
                        try:
                            # 使用 ib_insync 的异步API，这会正确设置内部 _results，并避免 KeyError
                            if hasattr(ib, 'reqOpenOrdersAsync'):
                                await ib.reqOpenOrdersAsync()
                            # 请求所有API来源的已完成订单（包含最近状态变化）
                            if hasattr(ib, 'reqCompletedOrdersAsync'):
                                await ib.reqCompletedOrdersAsync(apiOnly=False)
                            logger.debug("Requested order refresh from IBKR (async)")
                            # 等待片刻让返回的事件更新本地缓存
                            await asyncio.sleep(0.2)
                        except asyncio.CancelledError:
                            logger.debug("Order refresh task cancelled")
                            raise
                        except Exception as e:
                            logger.warning(f"Failed to request order refresh in async: {e}")
                    future = asyncio.run_coroutine_threadsafe(refresh_orders_async(), loop)
                    try:
                        future.result(timeout=1.0)
                    except concurrent.futures.TimeoutError:
                        logger.warning("Order refresh timeout (1.0s). Cancelling the refresh task to avoid pending tasks.")
                        future.cancel()
                    except Exception as e:
                        logger.warning(f"Order refresh failed: {e}")
                        try:
                            future.cancel()
                        except Exception:
                            pass
                else:
                    logger.debug("No active event loop for order refresh, using cached trades")
            except Exception as e:
                logger.debug(f"Failed to refresh orders: {e}")
            
            trades = ib.trades()
            target_trade = None
            for trade in trades:
                if trade.order.orderId == order_id:
                    target_trade = trade
                    break
            
            if not target_trade:
                logger.warning(f"Order {order_id} not found in active trades")
                # 检查订单是否可能已经完成或取消
                return False, f"Order {order_id} not found or already completed"
            
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
                        return True, f"Order cancellation initiated (Paper Trading may take longer)"
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
    
    def get_orders(self, risk_manager=None, strategy_manager=None) -> List[Dict]:
        """获取订单列表（支持去抖动，避免重复刷新）"""
        try:
            # 确保连接
            if not self.is_connected():
                if not self.connect():
                    logger.error(f"Cannot connect to IBKR for get orders")
                    return []
            
            ib = self.ib
            if not ib or not ib.isConnected():
                logger.error(f"IBKR connection lost for get orders")
                return []

            # 添加去抖逻辑 - 避免在短时间内重复刷新
            current_time = time.time()
            if not hasattr(self, '_last_order_refresh_time'):
                self._last_order_refresh_time = 0
            
            # 如果距离上次刷新不足1秒，直接返回缓存的trades数据
            time_since_last_refresh = current_time - self._last_order_refresh_time
            if time_since_last_refresh < 1.0:
                logger.debug(f"Skipping order refresh, last refresh was {time_since_last_refresh:.1f}s ago")
                # 直接使用缓存的trades，不重复请求
                trades = ib.trades()
            else:
                # 在连接管理器的事件循环中异步执行刷新操作，避免事件循环冲突
                try:
                    # 获取连接管理器的事件循环
                    loop = getattr(self.connection_manager, '_loop', None)
                    
                    if loop and not loop.is_closed():
                        # 在连接管理器的事件循环中异步执行刷新
                        async def refresh_orders_async():
                            try:
                                # 使用 ib_insync 的异步API，这会正确设置内部 _results，并避免 KeyError
                                if hasattr(ib, 'reqOpenOrdersAsync'):
                                    await ib.reqOpenOrdersAsync()
                                # 请求所有API来源的已完成订单（包含最近状态变化）
                                if hasattr(ib, 'reqCompletedOrdersAsync'):
                                    await ib.reqCompletedOrdersAsync(apiOnly=False)
                                logger.debug("Requested order refresh from IBKR (async)")
                                await asyncio.sleep(0.2)
                            except Exception as e:
                                logger.warning(f"Failed to request order refresh in async: {e}")
                        
                        # 在连接管理器的事件循环中执行异步刷新
                        future = asyncio.run_coroutine_threadsafe(refresh_orders_async(), loop)
                        try:
                            future.result(timeout=1.0)  # 1秒超时
                        except Exception as e:
                            logger.warning(f"Order refresh timeout or failed: {e}")
                    else:
                        logger.debug("No active event loop for order refresh, using cached data")
                    
                    # 更新刷新时间
                    self._last_order_refresh_time = current_time
                    
                    # 读取最新的 trades 列表
                    trades = ib.trades()
                except Exception as e:
                    logger.warning(f"Failed to request order refresh: {e}")
                    trades = ib.trades()  # fallback to existing trades
            
            # 处理订单数据
            orders: List[Dict[str, Any]] = []
            updated_orders = False  # Track if any orders were updated
            changed_orders: List[tuple[str, str]] = []  # (order_id_str, new_status)
            
            for trade in trades:
                # 从risk_manager的auto_orders中获取订单来源信息
                order_source = "未知"
                order_id_str = str(trade.order.orderId)
                
                if risk_manager and hasattr(risk_manager, 'auto_orders'):
                    if order_id_str in risk_manager.auto_orders:
                        auto_order = risk_manager.auto_orders[order_id_str]
                        if getattr(auto_order, 'order_source', None) == "manual":
                            order_source = "人工"
                        elif getattr(auto_order, 'order_source', None) == "auto":
                            order_source = "机器人"
                        else:
                            order_source = getattr(auto_order, 'order_source', "未知")
                        
                        # Synchronize order data back to risk_manager if status or price changed
                        current_status = trade.orderStatus.status
                        current_avg_fill_price = trade.orderStatus.avgFillPrice
                        
                        if (auto_order.status != current_status or 
                            (current_avg_fill_price and current_avg_fill_price > 0 and auto_order.price != current_avg_fill_price)):
                            
                            # Update the auto_order with latest data from IBKR
                            auto_order.status = current_status
                            if current_avg_fill_price and current_avg_fill_price > 0:
                                auto_order.price = current_avg_fill_price
                            
                            # Set executed_at if order is filled and not already set
                            if current_status == "Filled" and auto_order.executed_at is None:
                                from datetime import datetime
                                auto_order.executed_at = datetime.now()
                            
                            # NEW: update strategy execution price & PnL when we have fill price
                            try:
                                if strategy_manager and current_avg_fill_price and current_avg_fill_price > 0:
                                    strategy_manager.update_execution_price_and_pnl(order_id_str, current_avg_fill_price, ibkr_client=self)
                            except Exception as sm_err:
                                logger.warning(f"Failed to update strategy execution for order {order_id_str}: {sm_err}")
                            
                            updated_orders = True
                            changed_orders.append((order_id_str, current_status))
                            logger.info(f"Updated order {order_id_str}: status={current_status}, price={current_avg_fill_price}")
                            
                    else:
                        # 如果在auto_orders中找不到记录，可能是历史订单，默认为人工
                        order_source = "人工"
                else:
                    # 如果没有risk_manager，默认为人工
                    order_source = "人工"
                
                # Add debug logging for order data
                logger.debug(f"Processing order {trade.order.orderId}: totalQuantity={trade.order.totalQuantity}, action={trade.order.action}, orderType={trade.order.orderType}, status={trade.orderStatus.status}")
                
                orders.append({
                    'order_id': trade.order.orderId,
                    'symbol': getattr(trade.contract, 'localSymbol', None) or trade.contract.symbol,
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
            
            # Save updated orders to database if any were changed
            if updated_orders and risk_manager:
                for oid, new_status in changed_orders:
                    try:
                        risk_manager.update_order_status(oid, new_status)
                    except Exception as e:
                        logger.warning(f"Failed to persist updated order {oid}: {e}")
                logger.info("Updated order data saved to database through RiskManager")
            
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
                # Filter out zero positions to avoid stale data
                if item.position == 0:
                    continue
                
                sym = item.contract.symbol
                current_price = None
                price_source = None
                
                # 1) 首先检查缓存是否有效
                ts = self.price_cache_timestamp.get(sym)
                if ts and datetime.now() - ts < timedelta(seconds=self.price_cache_ttl_seconds):
                    cached_price = self.price_cache.get(sym)
                    if cached_price and cached_price > 0:
                        current_price = cached_price
                        price_source = "cache"
                        logger.debug(f"Using cached price for {sym}: {current_price}")
                
                # 2) 如果缓存无效，尝试获取实时价格
                if current_price is None:
                    try:
                        # 检查是否在ThreadPoolExecutor中运行
                        current_thread = threading.current_thread()
                        is_in_thread_pool = isinstance(current_thread, threading.Thread) and 'ThreadPoolExecutor' in current_thread.name
                        
                        # 即使在ThreadPoolExecutor中也尝试获取实时价格，但使用更短的超时
                        if not is_in_thread_pool:
                            data = self.get_real_time_data(sym)
                            if data and data.get('last'):
                                current_price = data['last']
                                price_source = "realtime"
                        else:
                            # 在ThreadPoolExecutor中，尝试快速获取实时价格
                            logger.debug(f"Attempting quick real-time fetch for {sym} in ThreadPoolExecutor")
                            data = self.get_real_time_data(sym)
                            if data and data.get('last'):
                                current_price = data['last']
                                price_source = "realtime_thread"
                    except Exception as e:
                        logger.debug(f"Realtime price fetch error for {sym}: {e}")
                
                # 3) 如果实时获取失败，尝试使用 marketPrice
                if current_price is None:
                    if item.marketPrice and item.marketPrice == item.marketPrice and item.marketPrice > 0:
                        current_price = item.marketPrice
                        price_source = "marketPrice"
                        logger.debug(f"Using marketPrice for {sym}: {current_price}")
                
                # 4) 最后兜底 averageCost
                if current_price is None:
                    if item.averageCost and item.averageCost > 0:
                        current_price = item.averageCost
                        price_source = "averageCost"
                        logger.warning(f"Using averageCost as fallback for {sym}: {current_price}")
                    else:
                        current_price = 0
                        price_source = "unknown"
                        logger.error(f"No valid price source found for {sym}")
                
                logger.info(f"Portfolio data for {sym}: marketPrice={item.marketPrice}, averageCost={item.averageCost}, position={item.position}")
                logger.info(f"Price for {sym}: {current_price} (source: {price_source})")
                
                # Adjust average cost for futures by contract multiplier to get entry price
                avg_cost_adjusted = item.averageCost
                futures_multipliers = {
                    'MNQ': 2,   # Micro E-mini Nasdaq-100
                    'NQ': 20,   # E-mini Nasdaq-100
                }
                if sym in futures_multipliers and item.averageCost:
                    avg_cost_adjusted = item.averageCost / futures_multipliers[sym]
                    logger.info(f"Futures multiplier adjustment for {sym}: {item.averageCost} / {futures_multipliers[sym]} = {avg_cost_adjusted}")
                else:
                    logger.info(f"No futures multiplier adjustment for {sym}, using original averageCost: {avg_cost_adjusted}")

                market_value = item.position * current_price
                
                position_info = {
                    'symbol': sym,
                    'position': item.position,
                    'avg_cost': avg_cost_adjusted,
                    'market_price': item.marketPrice if item.marketPrice == item.marketPrice else 0,
                    'current_price': current_price,
                    'market_value': market_value,
                    'unrealized_pnl': item.unrealizedPNL if hasattr(item, 'unrealizedPNL') else market_value - (item.position * avg_cost_adjusted),
                    'unrealized_pnl_percent': ((market_value - (item.position * avg_cost_adjusted)) / (item.position * avg_cost_adjusted) * 100) if item.position * avg_cost_adjusted != 0 else 0,
                    'price_source': price_source,
                }
                position_list.append(position_info)
            
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