from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

try:  # pragma: no cover - exercised indirectly when ib_insync is missing
    from ib_insync import IB, util, ExecutionFilter
except ImportError:  # pragma: no cover - fallback for test environments
    from test.ib_insync_stub import IB, util, ExecutionFilter
from loguru import logger

from .utils import now_local
from .restart_utils import restart_program

# 为connection模块创建专用logger
connection_logger = logger.bind(module="connection", request_id="-")
import os
import threading
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from src.config import load_project_env
import types
import subprocess
from pathlib import Path


load_project_env()

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


def _get_env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None or val == "":
        return float(default)
    try:
        cleaned = val.split('#', 1)[0].strip()
        return float(cleaned)
    except Exception:
        try:
            connection_logger.warning(f"Invalid float for {key}='{val}', falling back to {default}")
        except Exception:
            pass
        return float(default)


def _get_env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return bool(default)

    try:
        cleaned = val.split('#', 1)[0].strip().lower()
    except Exception:
        return bool(default)

    if cleaned == "":
        return bool(default)

    return cleaned in {"1", "true", "yes", "y", "on"}

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
        self._failed_connect_attempts: int = 0
        self._max_failed_connect_attempts: int = max(0, _get_env_int('IBKR_MAX_CONNECT_RETRIES', 0))
        self._restart_delay_sec: float = max(0.0, _get_env_float('IBKR_RESTART_DELAY_SEC', 0.0))
        self._restart_pending: bool = False
        self._connect_attempt_lock = threading.Lock()
        self._connect_state_lock = threading.Lock()
        self._connect_state_cond = threading.Condition(self._connect_state_lock)
        self._connect_in_progress = False
        self._connect_attempt_started_at: Optional[float] = None
        self._connect_wait_limit_sec: float = max(
            1.0, _get_env_float("IBKR_CONNECT_WAIT_LIMIT_SEC", 8.0)
        )
        self._project_root = Path(__file__).resolve().parents[1]
        self._pre_restart_script = self._project_root / "vps-setup" / "quick-start.sh"
        self._pre_restart_attempted = False
        self._process_restart_enabled = _get_env_bool("IBKR_ENABLE_PROCESS_RESTART", False)
        self._restart_suppressed_logged = False
        self._loop_timeout_warning_interval = max(
            0.1, _get_env_float("IBKR_LOOP_TIMEOUT_WARNING_INTERVAL_SEC", 5.0)
        )
        self._last_loop_timeout_warning = 0.0
        self._loop_timeout_warning_suppressed = 0
        # Weak reference back to IBKRClient (assigned during IBKRClient initialisation)
        self._ibkr_client_ref = None  # type: ignore[attr-defined]
        self._ibkr_connect_in_progress = False

    def _notify_ibkr_client_connected(self, *, external: bool) -> None:
        """Inform ``IBKRClient`` when a connection succeeds outside its control."""

        ref = getattr(self, "_ibkr_client_ref", None)
        if ref is None:
            return

        try:
            client = ref()  # type: ignore[operator]
        except Exception:
            client = None

        if not client:
            return

        try:
            client._on_connection_manager_connected(external=external)  # type: ignore[attr-defined]
        except AttributeError:
            # Older ``IBKRClient`` versions will simply not receive the callback.
            return
        except Exception as exc:  # pragma: no cover - defensive guard
            connection_logger.debug(
                "IBKR client notification after connect failed",
                error=str(exc),
            )
        
    def update_config(self, config: ConnectionConfig) -> None:
        """更新连接配置"""
        if self.connected:
            connection_logger.warning("Connection is active. Disconnect first before updating config.")
            return
        
        self.config = config
        connection_logger.info(f"Connection config updated: {config.to_dict()}")

    def _patch_wrapper_depth_guard(self, wrapper: Any) -> None:
        """防止ib_insync深度更新触发IndexError导致崩溃"""

        if not wrapper or not hasattr(wrapper, "updateMktDepthL2"):
            return

        update_method = getattr(wrapper.updateMktDepthL2, "__func__", wrapper.updateMktDepthL2)
        if getattr(update_method, "__ibbot_depth_guard__", False):
            return

        try:
            from ib_insync.objects import DOMLevel, MktDepthData
        except Exception as exc:  # pragma: no cover - defensive guard
            connection_logger.debug(f"Skipping depth guard patch (missing DOM classes): {exc}")
            return

        orig_update = wrapper.updateMktDepthL2

        def safe_update(self_wrapped, reqId, position, marketMaker, operation, side, price, size, isSmartDepth=False):
            ticker = None
            try:
                ticker = self_wrapped.reqId2Ticker.get(reqId)
            except Exception:
                ticker = None

            if ticker is not None:
                dom = ticker.domBids if side else ticker.domAsks
                if operation == 1 and position >= len(dom):
                    missing = position - len(dom) + 1
                    if missing > 0:
                        try:
                            dom.extend(DOMLevel(price, 0.0, marketMaker) for _ in range(missing))
                        except Exception:
                            dom.extend([DOMLevel(price, 0.0, marketMaker)] * missing)
                    try:
                        dom[position] = DOMLevel(price, size, marketMaker)
                    except Exception as assign_error:  # pragma: no cover - extremely unlikely
                        connection_logger.debug(
                            "Depth guard assignment failed",
                            req_id=reqId,
                            position=position,
                            side=side,
                            error=str(assign_error),
                        )
                        return
                    tick = MktDepthData(
                        self_wrapped.lastTime,
                        position,
                        marketMaker,
                        operation,
                        side,
                        price,
                        size,
                    )
                    ticker.domTicks.append(tick)
                    try:
                        self_wrapped.pendingTickers.add(ticker)
                    except Exception:
                        pass
                    return

                if operation == 2 and position >= len(dom):
                    connection_logger.debug(
                        "Ignoring depth delete beyond bounds",
                        req_id=reqId,
                        position=position,
                        side=side,
                    )
                    return

            try:
                return orig_update(reqId, position, marketMaker, operation, side, price, size, isSmartDepth)
            except IndexError as exc:  # pragma: no cover - defensive fallback
                connection_logger.warning(
                    "Depth update raised IndexError even after guard",
                    req_id=reqId,
                    position=position,
                    side=side,
                    operation=operation,
                    error=str(exc),
                )
                return None

        safe_update.__ibbot_depth_guard__ = True
        wrapper.updateMktDepthL2 = types.MethodType(safe_update, wrapper)
        try:
            wrapper.updateMktDepth.__func__.__ibbot_depth_guard__ = True  # type: ignore[attr-defined]
        except Exception:
            pass
        connection_logger.debug("Patched ib_insync wrapper.updateMktDepthL2 to guard DOM depth indexes")
    
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
                self._patch_wrapper_depth_guard(self.ib.wrapper)
                
                # 尝试连接，如果客户端ID冲突则自动尝试其他ID
                original_client_id = self.config.client_id
                connected = False
                
                for attempt in range(5):  # 最多尝试5次
                    try_client_id = original_client_id + attempt
                    if try_client_id > 32:  # IBKR客户端ID范围是1-32
                        try_client_id = try_client_id - 32
                    
                    try:
                        connection_logger.info(f"尝试连接IBKR，客户端ID: {try_client_id}")
                        # 使用asyncio.wait_for添加额外的超时保护
                        await asyncio.wait_for(
                            self.ib.connectAsync(
                                host=self.config.host,
                                port=self.config.port,
                                clientId=try_client_id,
                                timeout=8  # 减少内部超时
                            ),
                            timeout=10  # 外部超时保护
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
                    except asyncio.TimeoutError:
                        self._last_connect_error = f"连接超时 - 请检查TWS/Gateway是否启用API连接 (端口: {self.config.port})"
                        connection_logger.error(f"客户端ID {try_client_id} 连接超时")
                        raise Exception(self._last_connect_error)
                    except Exception as conn_error:
                        error_msg = str(conn_error)
                        lower_error = error_msg.lower()
                        client_id_in_use_signals = (
                            "already connected",
                            "duplicate",
                            "client id is already in use",
                            "clientid already in use",
                            "peer closed connection",
                            "error 326",
                        )
                        if any(signal in lower_error for signal in client_id_in_use_signals):
                            connection_logger.warning(f"客户端ID {try_client_id} 已被使用，尝试下一个ID")
                            try:
                                self.ib.disconnect()
                            except Exception:
                                pass
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

                        if errorCode in {2107, 2108}:
                            connection_logger.info(
                                f"IBKR data farm status {errorCode}: {errorString}"
                            )
                            client = None
                            ref = getattr(self, "_ibkr_client_ref", None)
                            if ref is not None:
                                try:
                                    client = ref()
                                except Exception:
                                    client = None
                            if client is not None:
                                try:
                                    client.handle_data_farm_status(errorCode, errorString)
                                except Exception as wake_err:  # pragma: no cover - defensive
                                    connection_logger.debug(
                                        f"Failed to schedule data farm wake-up: {wake_err}"
                                    )
                            return

                        try:
                            import core  # 延迟导入避免循环依赖

                            snapshot_service = getattr(core, "account_snapshot_service", None)
                            if snapshot_service:
                                connection_loss_codes = {1100, 1300, 2103, 2105, 2110}
                                connection_restore_codes = {1101, 1102, 1301, 2104, 2106, 2111}

                                if errorCode in connection_loss_codes:
                                    snapshot_service.flag_connection_loss(
                                        errorString or f"IBKR error {errorCode}"
                                    )
                                elif errorCode in connection_restore_codes:
                                    snapshot_service.request_immediate_refresh(
                                        reason="connection_restored"
                                    )
                        except Exception as snapshot_err:  # pragma: no cover - 防御性日志
                            connection_logger.debug(
                                f"Snapshot service notification failed: {snapshot_err}"
                            )

                        if errorCode in {2103, 2105}:
                            client = None
                            ref = getattr(self, "_ibkr_client_ref", None)
                            if ref is not None:
                                try:
                                    client = ref()
                                except Exception:
                                    client = None
                            if client is not None:
                                try:
                                    client.handle_gateway_broken(errorCode, errorString)
                                except Exception as notify_err:  # pragma: no cover - defensive
                                    connection_logger.debug(
                                        f"Failed to dispatch gateway-broken handler: {notify_err}"
                                    )

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
                                import core
                                asyncio.create_task(core.ibkr_client._handle_order_error(reqId, errorCode, errorString))
                    
                        # 绑定错误处理器到wrapper
                    self.ib.wrapper.error = error_handler
                    connection_logger.debug("Added IBKR error handler for order rejection detection")
                    
                    # 添加订单状态变化事件监听器
                    def order_status_handler(trade):
                        """处理订单状态变化事件"""

                        async def _process_order_status():
                            try:
                                order_id = str(trade.order.orderId)
                                status = trade.orderStatus.status
                                connection_logger.info(f"Order status update: {order_id} -> {status}")

                                # 更新本地订单状态，确保手动市价单等及时同步
                                try:
                                    from .order_manager import get_order_manager
                                    om = get_order_manager()
                                    fill_price = getattr(trade.orderStatus, 'avgFillPrice', None)
                                    executed_at = now_local() if status in ('Filled', 'PartiallyFilled') else None
                                    updated = await asyncio.to_thread(
                                        om.update_order_status,
                                        order_id=order_id,
                                        status=status,
                                        fill_price=fill_price,
                                        executed_at=executed_at,
                                    )

                                    if not updated:
                                        try:
                                            contract = getattr(trade, 'contract', None)
                                            order_obj = getattr(trade, 'order', None)
                                            symbol = (
                                                getattr(contract, 'symbol', None)
                                                or getattr(contract, 'localSymbol', None)
                                                or getattr(order_obj, 'symbol', None)
                                                or getattr(order_obj, 'localSymbol', '')
                                            )
                                            action = (getattr(order_obj, 'action', None) or '').upper()
                                            quantity = getattr(order_obj, 'totalQuantity', 0) or 0
                                            order_type = str(getattr(order_obj, 'orderType', None) or 'MKT').upper()
                                            lmt_price = getattr(order_obj, 'lmtPrice', None)
                                            aux_price = getattr(order_obj, 'auxPrice', None)
                                            price_value = None
                                            for candidate in (lmt_price, aux_price):
                                                if isinstance(candidate, (int, float)) and candidate != 0:
                                                    price_value = float(candidate)
                                                    break

                                            order_source = None

                                            # 优先尝试从 RiskManager/OrderManager 中识别订单来源
                                            try:
                                                import core  # 延迟导入避免循环依赖

                                                risk_manager = getattr(core, 'risk_manager', None)
                                                if (risk_manager and
                                                        hasattr(risk_manager, 'auto_orders') and
                                                        order_id in risk_manager.auto_orders):
                                                    auto_order = risk_manager.auto_orders.get(order_id)
                                                    candidate = getattr(auto_order, 'order_source', None)
                                                    if candidate:
                                                        order_source = str(candidate)

                                                if order_source is None:
                                                    existing = om.get_order(order_id)
                                                    if existing and getattr(existing, 'order_source', None):
                                                        order_source = str(existing.order_source)
                                            except Exception as detect_err:  # pragma: no cover - defensive
                                                connection_logger.debug(
                                                    f"Order source detection failed for {order_id}: {detect_err}"
                                                )

                                            # 如果仍未识别来源，再回退到 orderRef 的启发式判断
                                            if not order_source:
                                                order_source = 'manual'
                                                order_ref = getattr(order_obj, 'orderRef', '')
                                                if isinstance(order_ref, str) and order_ref:
                                                    lower_ref = order_ref.lower()
                                                    if 'tp' in lower_ref:
                                                        order_source = 'tp'
                                                    elif 'sl' in lower_ref:
                                                        order_source = 'sl'
                                                    elif ('auto' in lower_ref or
                                                          'strategy' in lower_ref or
                                                          'risk' in lower_ref):
                                                        order_source = 'auto'

                                            await asyncio.to_thread(
                                                om.add_order,
                                                {
                                                    'order_id': order_id,
                                                    'symbol': str(symbol or ''),
                                                    'action': action,
                                                    'quantity': int(quantity),
                                                    'order_type': order_type,
                                                    'price': price_value,
                                                    'status': status or 'Submitted',
                                                    'order_source': order_source,
                                                    'exchange': str(getattr(contract, 'exchange', 'SMART') or 'SMART'),
                                                    'sec_type': str(getattr(contract, 'secType', 'STK') or 'STK'),
                                                    'fill_price': fill_price,
                                                },
                                            )

                                            await asyncio.to_thread(
                                                om.update_order_status,
                                                order_id=order_id,
                                                status=status,
                                                fill_price=fill_price,
                                                executed_at=executed_at,
                                            )
                                        except Exception as add_err:
                                            connection_logger.warning(
                                                f"Failed to record order {order_id} from status event: {add_err}"
                                            )
                                except Exception as e:
                                    connection_logger.warning(f"OrderManager status update failed for {order_id}: {e}")

                                # 如果订单已成交，处理佣金和PnL
                                if status == 'Filled':
                                    import core
                                    asyncio.create_task(core.ibkr_client._handle_order_filled(trade))
                            except Exception as e:
                                connection_logger.error(f"Error in order status handler: {e}")

                        asyncio.create_task(_process_order_status())

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
                            import core
                            asyncio.create_task(core.ibkr_client._update_order_commission_pnl(order_id, commission, pnl))
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

                try:
                    import core

                    snapshot_service = getattr(core, "account_snapshot_service", None)
                    if snapshot_service:
                        snapshot_service.request_immediate_refresh(
                            reason="connection_established"
                        )
                except Exception as notify_err:  # pragma: no cover - defensive logging
                    connection_logger.debug(
                        f"Failed to notify snapshot service after connect: {notify_err}"
                    )
                
                # 保存事件循环引用
                self._loop = loop
                self._connection_thread_alive = True
                self._loop_timeout_warning_suppressed = 0
                self._last_loop_timeout_warning = 0.0

                try:
                    external = not self._ibkr_connect_in_progress
                except Exception:
                    external = True
                self._notify_ibkr_client_connected(external=external)

                # 保持事件循环运行，直到明确要求断开连接
                try:
                    while self._connection_thread_alive and self.ib.isConnected():
                        try:
                            # 使用异步 sleep 避免阻塞事件循环，添加超时保护
                            await asyncio.wait_for(asyncio.sleep(0.1), timeout=1.0)
                        except asyncio.TimeoutError:
                            # 如果连sleep都超时，说明事件循环可能有问题
                            now = time.monotonic()
                            if now - self._last_loop_timeout_warning >= self._loop_timeout_warning_interval:
                                if self._loop_timeout_warning_suppressed:
                                    connection_logger.warning(
                                        "Event loop sleep timeout, checking connection health "
                                        f"(suppressed {self._loop_timeout_warning_suppressed} duplicates)"
                                    )
                                    self._loop_timeout_warning_suppressed = 0
                                else:
                                    connection_logger.warning(
                                        "Event loop sleep timeout, checking connection health"
                                    )
                                self._last_loop_timeout_warning = now
                            else:
                                self._loop_timeout_warning_suppressed += 1
                                connection_logger.debug(
                                    "Event loop sleep timeout detected but warning suppressed",
                                    suppressed=self._loop_timeout_warning_suppressed,
                                )
                            if not self.ib.isConnected():
                                break
                        except RuntimeError as e:
                            if "Event loop stopped before Future completed" in str(e):
                                connection_logger.debug("Event loop stopped signal received")
                                break
                            raise
                        except Exception as e:
                            connection_logger.warning(f"Unexpected error in connection loop: {e}")
                            # 继续循环，除非是严重错误
                            if "cannot be called from a running event loop" in str(e):
                                break
                finally:
                    # 确保连接状态正确设置
                    if self.ib and self.ib.isConnected():
                        try:
                            self.ib.disconnect()
                        except Exception as e:
                            connection_logger.debug(f"Error during cleanup disconnect: {e}")
                    self.connected = False
                    self._connection_thread_alive = False
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
                
                # 设置事件循环超时，防止无限运行
                def stop_loop_on_timeout():
                    if not loop.is_running():
                        return
                    # 如果连接线程已经进入稳定运行状态，则不要强制停止事件循环
                    if self._connection_thread_alive or self.connected:
                        connection_logger.debug(
                            "Event loop timeout guard triggered but connection thread is healthy; skipping stop"
                        )
                        return
                    connection_logger.warning("Event loop timeout while waiting for connection; stopping loop")
                    loop.call_soon_threadsafe(loop.stop)
                
                # 30秒后强制停止事件循环（如果还在运行）
                timeout_timer = threading.Timer(30.0, stop_loop_on_timeout)
                timeout_timer.start()
                
                try:
                    loop.run_forever()
                finally:
                    timeout_timer.cancel()
                    
            except Exception as e:
                connection_logger.error(f"Event loop error: {e}")
                # 设置错误状态，让主线程知道出现了问题
                self._last_connect_error = f"Event loop error: {e}"
            finally:
                # 确保任务能够正确完成，避免 "Task was destroyed but it is pending" 警告
                try:
                    if task and not task.done():
                        task.cancel()
                        # 给任务一点时间来响应取消
                        try:
                            loop.run_until_complete(asyncio.wait_for(task, timeout=1.0))
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                            # 任务被取消或超时是正常的
                            pass
                except Exception as e:
                    connection_logger.debug(f"Error during task cleanup: {e}")
                finally:
                    try:
                        # 确保所有待处理的任务都被取消
                        pending_tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
                        if pending_tasks:
                            connection_logger.debug(f"Cancelling {len(pending_tasks)} pending tasks")
                            for task in pending_tasks:
                                task.cancel()
                        
                        if not loop.is_closed():
                            loop.close()
                    except Exception as e:
                        connection_logger.debug(f"Error closing loop: {e}")
                    finally:
                        # 清理循环引用
                        self._loop = None
        
        # 重置最近错误
        self._last_connect_error = None
        
        # 使用守护线程而不是线程池，确保线程能够持续运行
        self._connection_thread = threading.Thread(target=run_worker, daemon=True)
        self._connection_thread.start()
        
        # 等待连接建立或快速失败，增加更强的超时保护
        max_wait = 15  # 增加等待时间，但添加更多检查点
        wait_time = 0
        check_interval = 0.1  # 更频繁的检查
        
        while wait_time < max_wait:
            time.sleep(check_interval)
            wait_time += check_interval
            
            # 若连接成功
            if self.connected:
                connection_logger.info("IBKR connection established successfully")
                return True
                
            # 若出现错误则立即失败返回
            if self._last_connect_error:
                connection_logger.error(f"IBKR connection failed fast: {self._last_connect_error}")
                # 确保清理连接线程
                self._force_stop_connection_thread()
                return False
                
            # 检查连接线程是否还活着
            if not self._connection_thread or not self._connection_thread.is_alive():
                connection_logger.error("Connection thread died unexpectedly")
                self._last_connect_error = "Connection thread terminated unexpectedly"
                return False
        
        # 超时处理
        connection_logger.error(f"IBKR connection timeout after {max_wait} seconds")
        self._last_connect_error = f"Connection timeout after {max_wait} seconds"
        
        # 强制停止连接线程，防止资源泄漏
        self._force_stop_connection_thread()
        return False

    def _record_connect_result(self, success: bool) -> None:
        if success:
            with self._connect_attempt_lock:
                self._failed_connect_attempts = 0
                self._restart_pending = False
                self._pre_restart_attempted = False
            self._restart_suppressed_logged = False
            return

        if self._max_failed_connect_attempts <= 0:
            return

        with self._connect_attempt_lock:
            self._failed_connect_attempts += 1
            attempts = self._failed_connect_attempts
            threshold = self._max_failed_connect_attempts

        last_error = self._last_connect_error
        if last_error:
            connection_logger.warning(
                f"IBKR connection attempt failed ({attempts}/{threshold}): {last_error}"
            )
        else:
            connection_logger.warning(
                f"IBKR connection attempt failed ({attempts}/{threshold})"
            )

        if attempts >= threshold:
            self._trigger_restart(attempts, threshold)

    def _trigger_restart(self, attempts: int, threshold: int) -> None:
        restart_allowed = self._process_restart_enabled

        with self._connect_attempt_lock:
            if self._restart_pending:
                return
            if restart_allowed:
                self._restart_pending = True

        last_error = self._last_connect_error
        reason = f"IBKR connect failed {attempts} times (threshold={threshold})"
        if last_error:
            reason = f"{reason}: {last_error}"

        if not restart_allowed:
            if not self._restart_suppressed_logged:
                connection_logger.error(
                    "Exceeded maximum IBKR connect retries but process restart is disabled; "
                    "set IBKR_ENABLE_PROCESS_RESTART=true to re-enable automatic restarts."
                )
                self._restart_suppressed_logged = True
            with self._connect_attempt_lock:
                self._failed_connect_attempts = 0
            return

        self._run_pre_restart_script()

        connection_logger.critical(
            f"Exceeded maximum IBKR connect retries ({threshold}). Restarting application..."
        )
        if self._restart_delay_sec > 0:
            connection_logger.info(
                f"Restart scheduled after {self._restart_delay_sec:.2f} seconds"
            )

        env_updates = {
            "IBKR_FAILED_CONNECT_ATTEMPTS": str(attempts),
        }
        if last_error:
            env_updates["IBKR_LAST_CONNECT_ERROR"] = last_error

        restart_program(
            reason=reason,
            delay_sec=self._restart_delay_sec,
            env_updates=env_updates,
        )

    def _run_pre_restart_script(self) -> None:
        with self._connect_attempt_lock:
            if self._pre_restart_attempted:
                return
            self._pre_restart_attempted = True

        script_path = self._pre_restart_script
        if not script_path.exists():
            connection_logger.warning(
                f"Pre-restart script not found at {script_path}"
            )
            return

        connection_logger.info(
            f"Running pre-restart script prior to restart: {script_path}"
        )
        try:
            result = subprocess.run(
                ["/bin/bash", str(script_path)],
                cwd=str(self._project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(30, int(self._restart_delay_sec) + 30),
                check=False,
            )
        except Exception as exc:
            connection_logger.error(
                f"Pre-restart script execution failed: {exc}"
            )
            return

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if stdout:
            connection_logger.info(f"Pre-restart script stdout: {stdout[:500]}")
        if stderr:
            connection_logger.warning(f"Pre-restart script stderr: {stderr[:500]}")

        if result.returncode != 0:
            connection_logger.error(
                f"Pre-restart script exited with code {result.returncode}"
            )

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
                self._record_connect_result(True)
                return True
            else:
                connection_logger.info("Configuration changed, reconnecting...")
                self.disconnect()

        wait_logged = False
        force_abort_existing = False
        with self._connect_state_cond:
            while self._connect_in_progress:
                if not wait_logged:
                    connection_logger.info("Another IBKR connection attempt is in progress; waiting for completion")
                    wait_logged = True

                # 如果等待期间连接已经建立，则直接返回
                if self.connected and self.ib and self.ib.isConnected():
                    connection_logger.info("IBKR connection became available while waiting for existing attempt")
                    self._record_connect_result(True)
                    return True

                started_at = self._connect_attempt_started_at
                if started_at is not None:
                    elapsed = time.monotonic() - started_at
                    if elapsed >= self._connect_wait_limit_sec:
                        connection_logger.warning(
                            "Existing IBKR connection attempt exceeded wait limit; forcing new attempt",
                            elapsed_seconds=round(elapsed, 1),
                            wait_limit=self._connect_wait_limit_sec,
                        )
                        self._connect_in_progress = False
                        self._connect_attempt_started_at = None
                        self._connect_state_cond.notify_all()
                        force_abort_existing = True
                        break

                # 等待其他连接尝试完成
                self._connect_state_cond.wait(timeout=1.0)

            # 等待结束后再次检查连接状态，避免重复启动连接
            if self.connected and self.ib and self.ib.isConnected():
                connection_logger.info("IBKR connection completed while waiting")
                self._record_connect_result(True)
                return True

            # 标记当前正在尝试连接，阻止其他线程同时发起连接
            self._connect_in_progress = True
            self._connect_attempt_started_at = time.monotonic()

        if force_abort_existing:
            self._force_stop_connection_thread()

        connection_logger.info("Connecting to IBKR...")
        success = False
        try:
            success = self._connect_in_thread()
            return success
        finally:
            self._record_connect_result(success)
            with self._connect_state_cond:
                self._connect_in_progress = False
                self._connect_attempt_started_at = None
                self._connect_state_cond.notify_all()

    def _force_stop_connection_thread(self) -> None:
        thread = self._connection_thread
        loop = self._loop

        self._connection_thread_alive = False

        if loop:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                try:
                    loop.stop()
                except Exception:
                    pass

        if thread and thread.is_alive():
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass

        if self.ib:
            try:
                self.ib.disconnect()
            except Exception:
                pass

        self.connected = False
        self.ib = None
        self._loop = None
        self._connection_thread = None

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

            try:
                import core

                snapshot_service = getattr(core, "account_snapshot_service", None)
                if snapshot_service:
                    snapshot_service.flag_connection_loss("disconnected")
            except Exception as notify_err:  # pragma: no cover - defensive logging
                connection_logger.debug(
                    f"Failed to notify snapshot service on disconnect: {notify_err}"
                )
        except Exception as e:
            connection_logger.error(f"Disconnect error: {e}")

    def is_connected(self) -> bool:
        """检查连接状态"""
        if not self.ib:
            self.connected = False
            return False

        try:
            # 优先使用 ib_insync 提供的 isConnected 方法，避免 connState 出现短暂不同步导致误判
            if hasattr(self.ib, "isConnected") and self.ib.isConnected():
                self.connected = True
                return True
        except Exception as exc:  # pragma: no cover - 容错保护
            connection_logger.debug(f"ib.isConnected() check failed: {exc}")

        try:
            # 直接检查client的连接状态，避免调用可能阻塞的方法
            # ib_insync的client对象有connState属性：DISCONNECTED=0, CONNECTING=1, CONNECTED=2
            client = getattr(self.ib, "client", None)
            if client:
                conn_state = getattr(client, "connState", 0)
                connected_state = getattr(client, "CONNECTED", 2)
                if conn_state == connected_state:  # CONNECTED = 2
                    self.connected = True
                    return True

                # 如果处于连接中状态且之前判定为已连接，则保持连接状态，减少抖动
                connecting_state = getattr(client, "CONNECTING", 1)
                if conn_state == connecting_state and self.connected:
                    return True

                disconnected_state = getattr(client, "DISCONNECTED", 0)
                if conn_state == disconnected_state:
                    self.connected = False
                    return False

            # client 不存在时视为未连接
            self.connected = False
            return False

        except Exception as e:
            connection_logger.warning(f"Connection check failed: {e}")
            # 如果之前判定为已连接，保持结果，避免短暂错误导致的误判
            if self.connected:
                return True
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
    
    

    def get_config(self) -> ConnectionConfig:
        """获取当前配置"""
        return self.config
    
    def run_in_connection_thread(
        self,
        func,
        *args,
        timeout: Optional[float] = 60.0,
        **kwargs,
    ):
        """在连接线程的事件循环中执行同步函数，跨线程安全调度并等待结果。

        ``timeout`` 参数允许调用者为等待连接线程执行结果设置自定义超时时间，
        从而避免在底层 API 卡住时长时间阻塞调用方的协程。
        """
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

            wait_for: Optional[float]
            if timeout is None:
                wait_for = None
            else:
                try:
                    wait_for = float(timeout)
                except (TypeError, ValueError):
                    wait_for = 60.0
                    connection_logger.debug(
                        "Invalid timeout %r provided to run_in_connection_thread; falling back to 60s",
                        timeout,
                    )
                else:
                    if wait_for <= 0:
                        wait_for = 60.0

            try:
                return result_future.result(timeout=wait_for)
            except concurrent.futures.TimeoutError:
                if wait_for is None:
                    connection_logger.error(
                        "Execution in connection thread timed out with no limit"
                    )
                else:
                    connection_logger.error(
                        "Execution in connection thread timed out after %.1f seconds",
                        wait_for,
                    )
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
