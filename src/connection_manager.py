from typing import Optional, Dict, Any
from dataclasses import dataclass
from ib_insync import IB, util, ExecutionFilter
from loguru import logger

# 为connection模块创建专用logger
connection_logger = logger.bind(module="connection", request_id="-")
import os
import threading
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import types


load_dotenv()

# Robust env int parser to tolerate inline comments and invalid values
# Examples:
#   IBKR_PORT="4002  # paper"
#   IBKR_PORT=4002
# Both will parse to 4002; invalid values fall back to default with a warning

def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or val == "":
        return int(default)
    try:
        cleaned = val.split('#', 1)[0].strip()
        return int(cleaned)
    except Exception:
        try:
            connection_logger.warning(f"Invalid integer for {key}='{val}', falling back to {default}")
        except Exception:
            pass
        return int(default)

@dataclass
class ConnectionConfig:
    """连接配置类"""
    host: str = '127.0.0.1'
    port: int = 7497
    client_id: int = 1
    paper_trading: bool = True
    
    @classmethod
    def from_env(cls) -> 'ConnectionConfig':
        """从环境变量加载配置"""
        return cls(
            host=os.getenv('IBKR_HOST', '127.0.0.1'),
            port=_get_env_int('IBKR_PORT', 4002),
            client_id=_get_env_int('IBKR_CLIENT_ID', 1),
            paper_trading=os.getenv('IBKR_PAPER_TRADING', 'True').lower() == 'true'
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'host': self.host,
            'port': self.port,
            'client_id': self.client_id,
            'paper_trading': self.paper_trading
        }

class ConnectionManager:
    """统一的IBKR连接管理器"""
    
    def __init__(self):
        self.ib: Optional[IB] = None
        self.config: ConnectionConfig = ConnectionConfig.from_env()
        self.connected: bool = False
        self._connection_thread_alive: bool = False
        self._connection_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 最近一次连接错误（用于快速失败）
        self._last_connect_error: Optional[str] = None
        
    def update_config(self, config: ConnectionConfig) -> None:
        """更新连接配置"""
        if self.connected:
            connection_logger.warning("Connection is active. Disconnect first before updating config.")
            return
        
        self.config = config
        connection_logger.info(f"Connection config updated: {config.to_dict()}")
    
    def _connect_in_thread(self) -> bool:
        """在独立线程中连接到IBKR服务器"""
        async def connect_worker():
            try:
                # 获取当前事件循环
                loop = asyncio.get_event_loop()
                
                # 断开现有连接
                if self.ib:
                    try:
                        self.ib.disconnect()
                    except:
                        pass
                
                # 创建新的IB实例
                self.ib = IB()
                
                # 尝试连接，如果客户端ID冲突则自动尝试其他ID
                original_client_id = self.config.client_id
                connected = False
                
                for attempt in range(5):  # 最多尝试5次
                    try_client_id = original_client_id + attempt
                    if try_client_id > 32:  # IBKR客户端ID范围是1-32
                        try_client_id = try_client_id - 32
                    
                    try:
                        connection_logger.info(f"尝试连接IBKR，客户端ID: {try_client_id}")
                        await self.ib.connectAsync(
                            host=self.config.host,
                            port=self.config.port,
                            clientId=try_client_id,
                            timeout=10
                        )
                                # 验证连接是否真正建立
                        if self.ib.isConnected():
                            # 连接成功，更新配置中的客户端ID
                            self.config.client_id = try_client_id
                            connected = True
                            self._last_connect_error = None
                            connection_logger.info(f"成功连接IBKR，使用客户端ID: {try_client_id}")
                            break
                        else:
                            raise Exception(f"连接建立失败 - 端口 {self.config.port} 不可达或TWS/Gateway未运行")
                    except Exception as conn_error:
                        error_msg = str(conn_error)
                        if "already connected" in error_msg.lower() or "duplicate" in error_msg.lower():
                            connection_logger.warning(f"客户端ID {try_client_id} 已被使用，尝试下一个ID")
                            continue
                        elif "TimeoutError" in error_msg or "timeout" in error_msg.lower():
                            self._last_connect_error = f"连接超时 - 请检查TWS/Gateway是否启用API连接 (端口: {self.config.port})"
                            raise Exception(self._last_connect_error)
                        elif "ConnectionRefusedError" in error_msg or "refused" in error_msg.lower():
                            self._last_connect_error = f"连接被拒绝 - TWS/Gateway可能未运行或端口错误 (端口: {self.config.port})"
                            raise Exception(self._last_connect_error)
                        else:
                            if attempt == 4:  # 最后一次尝试
                                self._last_connect_error = f"连接失败: {error_msg}"
                                raise Exception(self._last_connect_error)
                            connection_logger.warning(f"客户端ID {try_client_id} 连接失败: {error_msg}，尝试下一个ID")
                 
                if not connected:
                    self._last_connect_error = f"无法找到可用的客户端ID (尝试了 {original_client_id} 到 {original_client_id + 4})"
                    raise Exception(self._last_connect_error)
                
                # 连接后设置默认市场数据类型，避免无权限时报 10197
                try:
                    default_mkt_data_type = _get_env_int('IBKR_MARKET_DATA_TYPE', 3)  # 1=实时,2=冻结,3=延迟,4=延迟冻结
                    self.ib.reqMarketDataType(default_mkt_data_type)
                    connection_logger.info(f"Set default IBKR market data type to {default_mkt_data_type}")
                except Exception as e:
                    connection_logger.debug(f"Failed to set default market data type: {e}")
                
                # 在连接建立后为 completedOrder 事件做安全防护，避免 KeyError: 'completedOrders'
                try:
                    w = self.ib.wrapper
                    orig_completedOrder = w.completedOrder
                    def safe_completedOrder(self_wrapped, c, o, st):
                        try:
                            # 确保结果字典中有 completedOrders 键
                            try:
                                _ = self_wrapped._results['completedOrders']
                            except KeyError:
                                self_wrapped._results['completedOrders'] = []
                        except Exception:
                            # 某些实现 _results 可能不是普通 dict，尽力处理
                            try:
                                if hasattr(self_wrapped._results, 'get') and self_wrapped._results.get('completedOrders') is None:
                                    self_wrapped._results['completedOrders'] = []
                            except Exception:
                                pass
                        return orig_completedOrder(c, o, st)
                    # 绑定成实例方法
                    w.completedOrder = types.MethodType(safe_completedOrder, w)
                    # 预置一次键，进一步降低风险
                    try:
                        w._results['completedOrders'] = []
                    except Exception:
                        pass
                    connection_logger.debug("Patched ib_insync wrapper.completedOrder to guard 'completedOrders' key")
                except Exception as e:
                    connection_logger.debug(f"Failed to patch wrapper.completedOrder: {e}")
                
                # 添加错误处理机制来捕获IBKR错误
                try:
                    def error_handler(reqId, errorCode, errorString, advancedOrderRejectJson=""):
                        """处理IBKR错误，特别是订单相关错误"""
                        connection_logger.info(f"IBKR Error - ReqId: {reqId}, Code: {errorCode}, Message: {errorString}")
                        
                        # 检查是否是订单相关错误
                        if reqId and reqId > 0:  # reqId > 0 通常表示订单相关
                            # 常见的订单错误代码：
                            # 201: Order rejected - reason
                            # 202: Order cancelled - reason  
                            # 203: The security is not available or allowed for this account
                            # 399: Order Message
                            # 434: The order size is too small
                            # 478: Order size does not conform to market rule
                            # 10147: OrderId that GetOpenOrder is attempting to query does not exist
                            # 10148: Requested market data is not subscribed
                            # 保证金不足相关错误代码
                            margin_error_codes = [10148, 10149, 10150, 399, 400, 401, 402]
                            # 订单被拒绝相关错误代码
                            rejection_error_codes = [201, 202, 203, 434, 478, 10147, 300] + margin_error_codes
                            
                            # 保证金不足相关关键词
                            margin_keywords = [
                                'insufficient', 'margin', 'buying power', 'rejected', 'not enough',
                                'order rejected', 'insufficient funds', 'insufficient buying power',
                                'margin requirement', 'account restriction', 'position limit'
                            ]
                            
                            is_order_error = (errorCode in rejection_error_codes or 
                                            any(keyword.lower() in errorString.lower() for keyword in margin_keywords))
                            
                            if is_order_error:
                                connection_logger.error(f"Order {reqId} rejected - Code: {errorCode}, Reason: {errorString}")
                                # 异步更新订单状态为失败
                                asyncio.create_task(self._handle_order_error(reqId, errorCode, errorString))
                    
                        # 绑定错误处理器到wrapper
                    self.ib.wrapper.error = error_handler
                    connection_logger.debug("Added IBKR error handler for order rejection detection")
                    
                    # 添加订单状态变化事件监听器
                    def order_status_handler(trade):
                        """处理订单状态变化事件"""
                        try:
                            order_id = str(trade.order.orderId)
                            status = trade.orderStatus.status
                            connection_logger.info(f"Order status update: {order_id} -> {status}")

                            # 更新本地订单状态，确保手动市价单等及时同步
                            try:
                                from .order_manager import get_order_manager
                                om = get_order_manager()
                                fill_price = getattr(trade.orderStatus, 'avgFillPrice', None)
                                executed_at = datetime.now() if status in ('Filled', 'PartiallyFilled') else None
                                om.update_order_status(
                                    order_id=order_id,
                                    status=status,
                                    fill_price=fill_price,
                                    executed_at=executed_at,
                                )
                            except Exception as e:
                                connection_logger.warning(f"OrderManager status update failed for {order_id}: {e}")

                            # 如果订单已成交，处理佣金和PnL
                            if status == 'Filled':
                                asyncio.create_task(self._handle_order_filled(trade))
                        except Exception as e:
                            connection_logger.error(f"Error in order status handler: {e}")
                    
                    # 添加成交细节事件监听器（ib_insync: execDetailsEvent）
                    def exec_details_handler(trade, fill):
                        """处理订单成交明细事件（不含佣金报告）"""
                        try:
                            order_id = str(trade.order.orderId)
                            connection_logger.info(f"Order fill event: {order_id}, fill: {fill}")
                        except Exception as e:
                            connection_logger.error(f"Error in exec details handler: {e}")
                    
                    # 添加佣金报告事件监听器（ib_insync: commissionReportEvent）
                    def commission_report_handler(trade, fill, report):
                        """处理成交后的佣金与已实现盈亏报告"""
                        try:
                            order_id = str(trade.order.orderId)
                            commission = getattr(report, 'commission', None)
                            pnl = getattr(report, 'realizedPNL', None)
                            connection_logger.info(
                                f"Commission report for order {order_id}: commission={commission}, pnl={pnl}"
                            )
                            # 异步更新订单的佣金和PnL
                            asyncio.create_task(self._update_order_commission_pnl(order_id, commission, pnl))
                        except Exception as e:
                            connection_logger.error(f"Error in commission report handler: {e}")
                    
                    # 绑定事件监听器（兼容性检测）
                    if hasattr(self.ib, 'orderStatusEvent'):
                        self.ib.orderStatusEvent += order_status_handler
                    if hasattr(self.ib, 'execDetailsEvent'):
                        self.ib.execDetailsEvent += exec_details_handler
                    if hasattr(self.ib, 'commissionReportEvent'):
                        self.ib.commissionReportEvent += commission_report_handler
                    connection_logger.debug("Added order status, exec details and commission report event handlers")
                    
                except Exception as e:
                    connection_logger.warning(f"Failed to setup event handlers: {e}")
                
                self.connected = True
                account_type = "Paper" if self.config.paper_trading else "Live"
                connection_logger.info(f"Connected to IBKR {account_type} account at {self.config.host}:{self.config.port}")
                
                # 保存事件循环引用
                self._loop = loop
                self._connection_thread_alive = True
                
                # 保持事件循环运行，直到明确要求断开连接
                try:
                    while self._connection_thread_alive and self.ib.isConnected():
                        try:
                            # 使用异步 sleep 避免阻塞事件循环
                            await asyncio.sleep(0.1)
                        except RuntimeError as e:
                            if "Event loop stopped before Future completed" in str(e):
                                connection_logger.debug("Event loop stopped signal received")
                                break
                            raise
                finally:
                    # 不在这里批量取消任务，避免递归取消；仅记录日志
                    connection_logger.info("IBKR connection loop ended")
            
            except Exception as e:
                connection_logger.error(f"Failed to connect to IBKR: {e}")
                self.connected = False
                # 标记错误以便主线程快速返回
                try:
                    self._last_connect_error = str(e)
                except Exception:
                    self._last_connect_error = "Unknown connection error"
                     
                 
        
        def run_worker():
            # 在新线程中创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # 立即保存事件循环引用，确保其他线程能够使用
            self._loop = loop
            
            task = None
            try:
                # 在事件循环中创建任务并运行整个事件循环，避免 run_until_complete 在停止时抛错
                task = loop.create_task(connect_worker())
                loop.run_forever()
            except Exception as e:
                connection_logger.error(f"Event loop error: {e}")
            finally:
                # 确保任务能够正确完成，避免 "Task was destroyed but it is pending" 警告
                try:
                    if task and not task.done():
                        task.cancel()
                        # 给任务一点时间来响应取消
                        try:
                            loop.run_until_complete(asyncio.wait_for(task, timeout=0.5))
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                            # 任务被取消或超时是正常的
                            pass
                except Exception as e:
                    connection_logger.debug(f"Error during task cleanup: {e}")
                finally:
                    try:
                        if not loop.is_closed():
                            loop.close()
                    except Exception as e:
                        connection_logger.debug(f"Error closing loop: {e}")
        
        # 重置最近错误
        self._last_connect_error = None
        
        # 使用守护线程而不是线程池，确保线程能够持续运行
        self._connection_thread = threading.Thread(target=run_worker, daemon=True)
        self._connection_thread.start()
        
        # 等待连接建立或快速失败
        max_wait = 12  # 将最大等待缩短，避免API超时
        wait_time = 0
        while wait_time < max_wait:
            time.sleep(0.2)
            wait_time += 0.2
            # 若连接成功
            if self.connected:
                connection_logger.info("IBKR connection established successfully")
                return True
            # 若出现错误则立即失败返回
            if self._last_connect_error:
                connection_logger.error(f"IBKR connection failed fast: {self._last_connect_error}")
                return False
        
        connection_logger.error("IBKR connection timeout")
        return False
    
    def connect(self) -> bool:
        """连接到IBKR服务器"""
        # 检查当前连接是否使用相同配置
        if self.connected and self.ib and self.ib.isConnected():
            # 如果配置发生变化，需要重新连接
            current_host = getattr(self.ib, 'host', None)
            current_port = getattr(self.ib, 'port', None)
            if (current_host == self.config.host and 
                current_port == self.config.port):
                connection_logger.info("Already connected to IBKR with same configuration")
                return True
            else:
                connection_logger.info("Configuration changed, reconnecting...")
                self.disconnect()
        
        connection_logger.info("Connecting to IBKR...")
        return self._connect_in_thread()
    
    def disconnect(self) -> None:
        """断开与IBKR服务器的连接，并停止事件循环"""
        try:
            # 标记线程应当停止
            self._connection_thread_alive = False
            
            # 安全断开 IB
            if self.ib and self.ib.isConnected():
                try:
                    self.ib.disconnect()
                    connection_logger.info("Disconnected from IBKR")
                except Exception as e:
                    connection_logger.warning(f"Error while disconnecting IB: {e}")
            
            # 关闭事件循环
            if self._loop and not self._loop.is_closed():
                try:
                    # 停止 run_forever 循环
                    self._loop.call_soon_threadsafe(self._loop.stop)
                    # 等待线程结束时再做最终清理
                except Exception as e:
                    connection_logger.debug(f"Error closing loop: {e}")
            
            # 等待线程结束
            if self._connection_thread and self._connection_thread.is_alive():
                try:
                    self._connection_thread.join(timeout=2.0)
                except Exception:
                    pass
            
            # 清理状态
            self.connected = False
            self.ib = None
            self._loop = None
            self._connection_thread = None
        except Exception as e:
            connection_logger.error(f"Disconnect error: {e}")

    def is_connected(self) -> bool:
        """检查连接状态"""
        if not self.ib:
            self.connected = False
            return False
            
        try:
            # 直接检查client的连接状态，避免调用可能阻塞的方法
            # ib_insync的client对象有connState属性：DISCONNECTED=0, CONNECTING=1, CONNECTED=2
            if hasattr(self.ib, 'client') and self.ib.client:
                # 直接检查连接状态常量，不调用任何方法
                conn_state = getattr(self.ib.client, 'connState', 0)
                is_connected = (conn_state == self.ib.client.CONNECTED)  # CONNECTED = 2
                self.connected = is_connected
                return is_connected
            else:
                # 如果没有client属性，说明未连接
                self.connected = False
                return False
                
        except Exception as e:
            connection_logger.warning(f"Connection check failed: {e}")
            self.connected = False
            return False
    
    def get_last_connect_error(self) -> Optional[str]:
        """获取最近一次连接错误原因，便于对外展示更具体的失败信息"""
        try:
            return self._last_connect_error
        except Exception:
            return None
    
    def get_ib_instance(self) -> Optional[IB]:
        """获取IB实例"""
        if self.is_connected():
            return self.ib
        return None
    
    def get_account_info(self) -> Dict[str, Any]:
        """获取账户信息"""
        if not self.is_connected():
            raise Exception("Not connected to IBKR")
            
        try:
            account_values = self.ib.accountValues()
            account_summary = self.ib.accountSummary()
            
            # 提取关键信息
            info = {
                'account_id': '',
                'account_type': 'Paper' if self.config.paper_trading else 'Live',
                'total_cash': 0.0,
                'net_liquidation': 0.0,
                'buying_power': 0.0
            }
            
            # 从account_values中提取信息
            for av in account_values:
                if av.tag == 'AccountCode':
                    info['account_id'] = av.value
                elif av.tag == 'TotalCashValue' and av.currency == 'USD':
                    info['total_cash'] = float(av.value)
                elif av.tag == 'NetLiquidation' and av.currency == 'USD':
                    info['net_liquidation'] = float(av.value)
                elif av.tag == 'BuyingPower' and av.currency == 'USD':
                    info['buying_power'] = float(av.value)
            
            return info
            
        except Exception as e:
            connection_logger.error(f"Failed to get account info: {e}")
            raise
    
    async def _handle_order_error(self, req_id, error_code, error_string):
        """处理订单错误，更新订单状态为失败"""
        try:
            from .order_manager import get_order_manager
            from .models import OrderStatus
            
            # 记录错误信息
            connection_logger.error(f"Order error - ReqId: {req_id}, Code: {error_code}, Message: {error_string}")
            
            # 从IBKRClient获取reqId到订单ID的映射
            if hasattr(self, '_ibkr_client_ref') and self._ibkr_client_ref:
                ibkr_client = self._ibkr_client_ref()
                if ibkr_client and hasattr(ibkr_client, 'req_id_to_order_id'):
                    order_id = ibkr_client.req_id_to_order_id.get(req_id)
                    if order_id:
                        # 更新订单状态为失败
                        order_manager = get_order_manager()
                        order_manager.update_order_status(
                            order_id=order_id,
                            status=OrderStatus.REJECTED.value,
                            notes=f"IBKR Error {error_code}: {error_string}"
                        )
                        connection_logger.info(f"Updated order {order_id} status to REJECTED due to error {error_code}")
                        
                        # 清理映射
                        del ibkr_client.req_id_to_order_id[req_id]
                    else:
                        connection_logger.warning(f"No order mapping found for reqId {req_id}")
                else:
                    connection_logger.warning(f"IBKRClient reference not available for error handling")
            
        except Exception as e:
            connection_logger.error(f"Failed to handle order error: {e}")
    
    async def _handle_order_filled(self, trade):
        """处理订单成交事件"""
        try:
            order_id = str(trade.order.orderId)
            connection_logger.info(f"Handling filled order: {order_id}")
            
            # 获取佣金和PnL信息
            total_commission = 0
            total_pnl = 0
            
            for fill in trade.fills:
                if hasattr(fill, 'commissionReport') and fill.commissionReport:
                    total_commission += fill.commissionReport.commission
                    if hasattr(fill.commissionReport, 'realizedPNL'):
                        total_pnl += fill.commissionReport.realizedPNL or 0
            
            connection_logger.info(f"Order {order_id} commission: {total_commission}, pnl: {total_pnl}")
            
            # 更新数据库中的订单信息
            await self._update_order_commission_pnl(order_id, total_commission, total_pnl)
            
        except Exception as e:
            connection_logger.error(f"Error handling filled order: {e}")
    
    async def _update_order_commission_pnl(self, order_id, commission, pnl):
        """更新订单的佣金和PnL到数据库并同步内存对象"""
        try:
            from .database import get_db_manager
            
            db = get_db_manager()
            oid = str(order_id)
            await db.update_order_commission_pnl(oid, commission, pnl)
            connection_logger.info(f"Updated order {oid} commission={commission}, pnl={pnl} in database")

            # 同步内存中的订单（如果存在）
            try:
                from .order_manager import get_order_manager
                om = get_order_manager()
                oid = str(order_id)
                order = om.orders.get(oid)
                if order:
                    prev_comm, prev_pnl = order.commission, order.pnl
                    if commission is not None:
                        order.commission = commission
                    if pnl is not None:
                        order.pnl = pnl
                    connection_logger.debug(
                        f"Synchronized in-memory order {oid} commission: {prev_comm} -> {order.commission}, pnl: {prev_pnl} -> {order.pnl}"
                    )
                else:
                    connection_logger.debug(f"In-memory order {oid} not found for commission/pnl sync")
            except Exception as sync_err:
                connection_logger.debug(f"Failed to sync in-memory order commission/pnl: {sync_err}")
            
            # 将最新的佣金和PnL更新通过 WebSocket 广播给前端，触发订单列表刷新
            try:
                from .websocket_manager import websocket_manager
                message = {
                    "type": "order_update",
                    "order_id": oid,
                    "commission": commission,
                    "pnl": pnl,
                }
                # 如果拿到了内存中的订单对象，补充更多字段（与 order_filled 事件保持一致的结构）
                if 'order' in locals() and order:
                    message.update({
                        "symbol": getattr(order, 'symbol', None),
                        "action": getattr(order, 'action', None),
                        "quantity": getattr(order, 'quantity', None),
                        "fill_price": getattr(order, 'fill_price', None),
                        "order_type": getattr(order, 'order_type', None),
                        "executed_at": order.executed_at.isoformat() if getattr(order, 'executed_at', None) else None,
                    })
                await websocket_manager.broadcast(message)
                connection_logger.info(f"Broadcasted order_update for order {oid} (commission={commission}, pnl={pnl})")
            except Exception as ws_err:
                connection_logger.warning(f"Failed to broadcast order_update for order {oid}: {ws_err}")
            
        except Exception as e:
            connection_logger.error(f"Error updating order commission/pnl in database: {e}")
    
    def backfill_commissions(self, days: int = 7) -> int:
        """Backfill commission and PnL for recent executions by requesting executions from IBKR.
        This will not override existing non-null commission/pnl in DB thanks to COALESCE guards.
        Returns the number of update attempts made.
        """
        try:
            if not self.ib or not self.ib.isConnected():
                connection_logger.error("Cannot backfill commissions: IB not connected")
                return 0

            import datetime as _dt

            async def _backfill_op():
                try:
                    ib_local = self.ib
                    # Compute start time for the execution filter (local time, format YYYYMMDD-HH:MM:SS)
                    start_dt = _dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))
                    # IB API expects a hyphen between date and time, e.g. 20250131-13:45:00
                    start_str = start_dt.strftime('%Y%m%d-%H:%M:%S')

                    # Request executions; this will also trigger commissionReport events
                    filt = ExecutionFilter(time=start_str)
                    connection_logger.info(f"Requesting executions since {start_str} for backfilling commissions/PnL")
                    try:
                        await ib_local.reqExecutionsAsync(filt)
                    except Exception as e:
                        connection_logger.warning(f"reqExecutionsAsync failed: {e}; trying sync fallback")
                        try:
                            ib_local.reqExecutions(filt)
                        except Exception as e2:
                            connection_logger.error(f"Sync reqExecutions failed: {e2}")
                            return 0

                    # Allow some time for commission reports to arrive and pair with executions
                    await asyncio.sleep(1.5)

                    fills = ib_local.fills()
                    if not fills:
                        connection_logger.info("No fills returned from IBKR for the specified window")
                        return 0

                    update_count = 0
                    for fill in fills:
                        try:
                            # Prefer commissionReport attached to fill
                            cr = getattr(fill, 'commissionReport', None)
                            exec_obj = getattr(fill, 'execution', None)
                            if cr and exec_obj and hasattr(exec_obj, 'orderId'):
                                order_id = str(exec_obj.orderId)
                                commission = cr.commission
                                pnl = getattr(cr, 'realizedPNL', None)
                                connection_logger.debug(
                                    f"Backfill commission report for order {order_id}: commission={commission}, pnl={pnl}"
                                )
                                # Use the same updater which syncs DB and in-memory order
                                await self._update_order_commission_pnl(order_id, commission, pnl)
                                update_count += 1
                        except Exception as inner_e:
                            connection_logger.warning(f"Failed to process a fill during backfill: {inner_e}")

                    connection_logger.info(f"Backfill completed. Updates attempted: {update_count}")
                    return update_count
                except Exception as e:
                    connection_logger.error(f"Backfill operation failed: {e}")
                    return 0

            result = self.run_async_in_connection_thread(_backfill_op())
            try:
                return int(result or 0)
            except Exception:
                return 0
        except Exception as e:
            connection_logger.error(f"Backfill commissions wrapper error: {e}")
            return 0

    def get_config(self) -> ConnectionConfig:
        """获取当前配置"""
        return self.config
    
    def run_in_connection_thread(self, func, *args, **kwargs):
        """在连接线程的事件循环中执行同步函数，跨线程安全调度并等待结果。"""
        try:
            loop = self._loop
            if not loop or loop.is_closed():
                connection_logger.warning("Connection event loop not available")
                return None
            if not self.ib or not self.ib.isConnected():
                connection_logger.warning("IBKR connection not available")
                return None

            # 如果已在连接线程，直接执行
            if threading.current_thread() == self._connection_thread:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    connection_logger.error(f"Error executing function in connection thread: {e}")
                    raise

            import concurrent.futures
            result_future: concurrent.futures.Future = concurrent.futures.Future()

            def runner():
                try:
                    res = func(*args, **kwargs)
                    result_future.set_result(res)
                except Exception as e:
                    result_future.set_exception(e)

            try:
                loop.call_soon_threadsafe(runner)
            except Exception as e:
                connection_logger.error(f"Failed to schedule function on connection loop: {e}")
                return None

            try:
                return result_future.result(timeout=60)
            except concurrent.futures.TimeoutError:
                connection_logger.error("Execution in connection thread timed out after 60 seconds")
                return None
            except Exception as e:
                connection_logger.error(f"Execution in connection thread raised error: {e}")
                raise
        except Exception as e:
            connection_logger.error(f"Failed to execute function in connection thread: {e}")
            return None

    def run_async_in_connection_thread(self, coro, timeout: float = 60):
        """在连接线程的事件循环中执行协程，跨线程调度并等待结果。

        该方法用于需要在连接线程的事件循环上运行的异步 IBKR API 调用，
        可以避免在事件循环已运行时调用同步 API 导致的
        "this event loop is already running" 错误。
        """
        try:
            loop = self._loop
            # 如果循环还未就绪，但连接线程可能刚启动，给出短暂重试机会
            if (not loop or loop.is_closed()) and self._connection_thread and self._connection_thread.is_alive():
                try:
                    time.sleep(0.05)
                except Exception:
                    pass
                loop = self._loop

            if not loop or loop.is_closed():
                connection_logger.warning("Connection event loop not available")
                return None
            if not self.ib or not self.ib.isConnected():
                connection_logger.warning("IBKR connection not available")
                return None

            # 检查事件循环是否正在运行
            if not loop.is_running():
                connection_logger.warning("Connection event loop is not running")
                return None

            import concurrent.futures
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=timeout)
            except RuntimeError as e:
                if "no current event loop" in str(e).lower():
                    connection_logger.error(f"No current event loop in thread: {e}")
                    return None
                else:
                    raise
            except concurrent.futures.TimeoutError:
                connection_logger.error(f"Execution in connection thread timed out after {timeout} seconds")
                return None
            except Exception as e:
                connection_logger.error(f"Execution in connection thread raised error: {e}")
                raise
        except Exception as e:
            connection_logger.error(f"Failed to execute coroutine in connection thread: {e}")
            return None


connection_manager = ConnectionManager()
