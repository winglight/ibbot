from __future__ import annotations

from typing import Any, Callable, Coroutine, Dict, Optional
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
from . import account_snapshot_cache
from .routes.service_clients import AccountServiceClient, ServiceClientError
import types
import subprocess
from pathlib import Path


load_project_env()

# Cached HTTP client for the account service to avoid repeated reconfiguration
_ACCOUNT_SERVICE_CLIENT: AccountServiceClient | None = None


def _get_account_service_client() -> AccountServiceClient:
    global _ACCOUNT_SERVICE_CLIENT
    if _ACCOUNT_SERVICE_CLIENT is None:
        _ACCOUNT_SERVICE_CLIENT = AccountServiceClient()
    return _ACCOUNT_SERVICE_CLIENT

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
        self._connect_async_timeout_sec: float = max(
            1.0, _get_env_float("IBKR_CONNECT_ASYNC_TIMEOUT_SEC", 8.0)
        )
        self._connect_handshake_grace_sec: float = max(
            0.0, _get_env_float("IBKR_CONNECT_HANDSHAKE_GRACE_SEC", 15.0)
        )
        self._connect_handshake_poll_interval_sec: float = max(
            0.05, _get_env_float("IBKR_CONNECT_HANDSHAKE_POLL_INTERVAL_SEC", 0.2)
        )
        default_result_timeout = max(
            self._connect_async_timeout_sec + self._connect_handshake_grace_sec + 5.0,
            15.0,
        )
        self._connect_result_timeout_sec: float = max(
            default_result_timeout,
            _get_env_float("IBKR_CONNECT_RESULT_TIMEOUT_SEC", default_result_timeout),
        )
        self._connect_result_event: Optional[threading.Event] = None
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
        self._post_connect_health_interval = max(
            15.0, _get_env_float("IBKR_CONNECTED_HEALTH_CHECK_INTERVAL_SEC", 30.0)
        )
        self._disconnect_wait_event: Optional[asyncio.Event] = None
        # Weak reference back to IBKRClient (assigned during IBKRClient initialisation)
        self._ibkr_client_ref = None  # type: ignore[attr-defined]
        self._ibkr_connect_in_progress = False
        self._client_id_min = max(0, _get_env_int("IBKR_CLIENT_ID_MIN", 1))
        self._client_id_max = max(self._client_id_min, _get_env_int("IBKR_CLIENT_ID_MAX", 32))
        if self._client_id_min == self._client_id_max:
            self._client_id_cycle_size = 1
        else:
            self._client_id_cycle_size = (self._client_id_max - self._client_id_min) + 1
        self._client_id_retry_limit = max(1, _get_env_int("IBKR_CLIENT_ID_RETRY_LIMIT", 5))
        self._next_client_id_hint = self._normalize_client_id(self.config.client_id)
        self.config.client_id = self._next_client_id_hint
        self._client_id_conflict_detected: bool = False

    def _schedule_background_task(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        description: str,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            try:
                loop.create_task(coro_factory())
                return
            except Exception as exc:  # pragma: no cover - defensive guard
                connection_logger.debug(
                    "Failed to schedule {} on running loop: {}",
                    description,
                    exc,
                )

        target_loop = self._loop if self._loop and self._loop.is_running() else None
        if target_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(coro_factory(), target_loop)
                return
            except Exception as exc:  # pragma: no cover - defensive guard
                connection_logger.debug(
                    "Failed to schedule {} on connection loop: {}",
                    description,
                    exc,
                )

        def _runner() -> None:
            try:
                asyncio.run(coro_factory())
            except Exception as exc:  # pragma: no cover - defensive guard
                connection_logger.debug(
                    "Background {} task raised: {}",
                    description,
                    exc,
                )

        thread_name = f"ibbot-{description.replace(' ', '-')}"
        threading.Thread(target=_runner, name=thread_name, daemon=True).start()

    def _mark_snapshots_stale(self, reason: str) -> None:
        async def _task() -> None:
            try:
                await account_snapshot_cache.mark_all_snapshots_stale(reason)
            except Exception as exc:  # pragma: no cover - defensive guard
                connection_logger.warning(
                    "Failed to mark account snapshots stale ({}): {}",
                    reason,
                    exc,
                )

        self._schedule_background_task(
            _task,
            description="account-snapshots-stale",
        )

    def _request_account_refresh(self, reason: str) -> None:
        async def _task() -> None:
            try:
                client = _get_account_service_client()
                await client.request_refresh(reason=reason)
            except ServiceClientError as exc:
                detail = getattr(exc, "detail", None)
                connection_logger.warning(
                    "Account refresh request failed ({reason}): {error}",
                    reason=reason,
                    error=detail or str(exc),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                connection_logger.warning(
                    "Account refresh request failed ({reason}): {error}",
                    reason=reason,
                    error=str(exc),
                )

        self._schedule_background_task(
            _task,
            description="account-refresh",
        )

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

    def _notify_ibkr_client_disconnected(self, *, trigger: str) -> None:
        """Notify ``IBKRClient`` that the managed session terminated."""

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
            client.mark_connection_lost(trigger)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                client._handle_disconnect_detected(trigger, signal_monitor=True)  # type: ignore[attr-defined]
            except AttributeError:
                return
            except Exception as exc:  # pragma: no cover - defensive guard
                connection_logger.debug(
                    "IBKR client notification after disconnect failed",
                    error=str(exc),
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            connection_logger.debug(
                "IBKR client notification after disconnect failed",
                error=str(exc),
            )

    def _normalize_client_id(self, candidate: int) -> int:
        try:
            candidate = int(candidate)
        except Exception:
            candidate = self._client_id_min

        lower = self._client_id_min
        upper = self._client_id_max
        span = max(1, getattr(self, "_client_id_cycle_size", 1))

        if lower > upper:
            lower, upper = upper, lower
            span = max(1, (upper - lower) + 1)

        if span <= 1:
            return lower

        if candidate < lower or candidate > upper:
            normalized = lower + ((candidate - lower) % span)
        else:
            normalized = candidate

        if normalized < lower:
            normalized = lower
        elif normalized > upper:
            normalized = upper

        return normalized

    def _compute_next_client_id(self, current: int) -> int:
        lower = self._client_id_min
        span = max(1, getattr(self, "_client_id_cycle_size", 1))
        current_norm = self._normalize_client_id(current)

        if span <= 1:
            return current_norm

        offset = (current_norm - lower + 1) % span
        return lower + offset

    def _handle_client_id_conflict(
        self,
        current_id: Optional[int],
        *,
        source: str,
        notify_client: bool = True,
    ) -> int:
        with self._connect_attempt_lock:
            conflict_id = self._normalize_client_id(current_id or self.config.client_id)
            next_id = self._compute_next_client_id(conflict_id)
            self.config.client_id = next_id
            self._next_client_id_hint = next_id
            self._last_connect_error = "检测到客户端ID冲突"

        if next_id == conflict_id and self._client_id_cycle_size <= 1:
            connection_logger.warning(
                "Detected IBKR client ID conflict but no alternate IDs are configured",
                client_id=conflict_id,
                source=source,
            )
            return conflict_id

        connection_logger.warning(
            "Detected IBKR client ID conflict; advancing to next ID",
            previous_client_id=conflict_id,
            next_client_id=next_id,
            source=source,
        )

        try:
            if self.ib and self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass

        self.connected = False

        client = None
        ref = getattr(self, "_ibkr_client_ref", None)
        if ref is not None:
            try:
                client = ref()
            except Exception:
                client = None

        if client is not None and notify_client:
            loop = getattr(client, "_async_loop", None)

            def _trigger_reconnect() -> None:
                try:
                    client._handle_disconnect_detected("client-id-conflict")  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    client._schedule_reconnect_task()  # type: ignore[attr-defined]
                except Exception:
                    pass

            if loop and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(_trigger_reconnect)
                except RuntimeError:
                    if loop.is_running():
                        _trigger_reconnect()
            else:
                _trigger_reconnect()

        return next_id
        
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

    def _ensure_error_handler_installed(self, wrapper: Any) -> None:
        """Install enhanced error handler once to capture client ID conflicts early."""

        if not wrapper or getattr(wrapper, "_ibbot_error_handler_installed", False):
            return

        orig_error_handler = getattr(wrapper, "error", None)

        def error_handler(self_wrapped, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            """处理IBKR错误，特别是订单相关错误"""
            connection_logger.info(
                f"IBKR Error - ReqId: {reqId}, Code: {errorCode}, Message: {errorString}"
            )

            lower_error_string = (errorString or "").lower()
            conflict_signals = (
                "client id is already in use",
                "clientid already in use",
                "already connected",
                "duplicate",
                "peer closed connection",
                "error 326",
            )

            if errorCode == 326 or any(signal in lower_error_string for signal in conflict_signals):
                self._client_id_conflict_detected = True
                self._handle_client_id_conflict(self.config.client_id, source="error-handler")
                if orig_error_handler and orig_error_handler is not error_handler:
                    try:
                        orig_error_handler(reqId, errorCode, errorString, advancedOrderRejectJson)
                    except Exception:
                        pass
                return

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
                if orig_error_handler and orig_error_handler is not error_handler:
                    try:
                        orig_error_handler(reqId, errorCode, errorString, advancedOrderRejectJson)
                    except Exception:
                        pass
                return

            connection_loss_codes = {1100, 1300, 2103, 2105, 2110}
            connection_restore_codes = {1101, 1102, 1301, 2104, 2106, 2111}

            if errorCode in connection_loss_codes:
                self._mark_snapshots_stale(errorString or f"IBKR error {errorCode}")
            elif errorCode in connection_restore_codes:
                self._request_account_refresh("connection_restored")

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

            if reqId and reqId > 0:  # reqId > 0 通常表示订单相关
                margin_error_codes = [10148, 10149, 10150, 399, 400, 401, 402]
                rejection_error_codes = [201, 202, 203, 434, 478, 10147, 300] + margin_error_codes

                margin_keywords = [
                    'insufficient', 'margin', 'buying power', 'rejected', 'not enough',
                    'order rejected', 'insufficient funds', 'insufficient buying power',
                    'margin requirement', 'account restriction', 'position limit'
                ]

                try:
                    keyword_match = any(
                        keyword.lower() in (errorString or "").lower()
                        for keyword in margin_keywords
                    )
                except Exception:
                    keyword_match = False

                is_order_error = (
                    errorCode in rejection_error_codes or keyword_match
                )

                if is_order_error:
                    connection_logger.error(
                        f"Order {reqId} rejected - Code: {errorCode}, Reason: {errorString}"
                    )
                    try:
                        import core
                        asyncio.create_task(
                            core.ibkr_client._handle_order_error(reqId, errorCode, errorString)
                        )
                    except Exception as task_err:
                        connection_logger.debug(
                            f"Failed to dispatch order error handler: {task_err}"
                        )

            if orig_error_handler and orig_error_handler is not error_handler:
                try:
                    orig_error_handler(reqId, errorCode, errorString, advancedOrderRejectJson)
                except Exception:
                    pass

        wrapper.error = types.MethodType(error_handler, wrapper)
        setattr(wrapper, "_ibbot_error_handler_installed", True)
        connection_logger.debug("Added IBKR error handler for order rejection detection")

    def _notify_connect_waiters(self) -> None:
        try:
            if self._connect_result_event:
                self._connect_result_event.set()
        except Exception:
            pass
        with self._connect_state_cond:
            self._connect_state_cond.notify_all()

    def _connect_in_thread(self) -> bool:
        """在独立线程中连接到IBKR服务器"""
        self._connect_result_event = threading.Event()
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
                self._ensure_error_handler_installed(self.ib.wrapper)

                # 尝试连接，如果客户端ID冲突则自动尝试其他ID
                connected = False
                attempts = 0
                attempted_ids = set()
                try_client_id = self._normalize_client_id(self._next_client_id_hint)

                while attempts < self._client_id_retry_limit:
                    self._client_id_conflict_detected = False
                    if try_client_id in attempted_ids:
                        break
                    attempted_ids.add(try_client_id)
                    attempts += 1
                    self.config.client_id = try_client_id
                    try:
                        connection_logger.info(f"尝试连接IBKR，客户端ID: {try_client_id}")
                        connect_timeout = max(self._connect_async_timeout_sec, 1.0)
                        grace_deadline = (
                            loop.time() + self._connect_handshake_grace_sec
                            if self._connect_handshake_grace_sec > 0
                            else None
                        )

                        try:
                            await self.ib.connectAsync(
                                host=self.config.host,
                                port=self.config.port,
                                clientId=try_client_id,
                                timeout=connect_timeout,
                            )
                        except asyncio.TimeoutError:
                            connection_logger.warning(
                                "connectAsync timed out; entering handshake grace period",
                                client_id=try_client_id,
                                timeout_sec=round(connect_timeout, 1),
                            )

                        if not self.ib.isConnected() and grace_deadline is not None:
                            last_state: Optional[int] = None
                            while loop.time() < grace_deadline:
                                client = getattr(self.ib, "client", None)
                                conn_state = getattr(client, "connState", None)
                                if conn_state != last_state:
                                    last_state = conn_state
                                    connection_logger.debug(
                                        "Waiting for IBKR handshake to complete",
                                        client_id=try_client_id,
                                        conn_state=conn_state,
                                        grace_remaining=round(grace_deadline - loop.time(), 1),
                                    )
                                if self.ib.isConnected():
                                    break
                                await asyncio.sleep(self._connect_handshake_poll_interval_sec)

                        if self.ib.isConnected():
                            connected = True
                            self._last_connect_error = None
                            self._next_client_id_hint = try_client_id
                            connection_logger.info(f"成功连接IBKR，使用客户端ID: {try_client_id}")
                            break

                        if grace_deadline is not None:
                            raise TimeoutError(
                                f"IBKR handshake timed out after {self._connect_handshake_grace_sec:.1f}s"
                            )

                        raise Exception(
                            f"连接建立失败 - 端口 {self.config.port} 不可达或TWS/Gateway未运行"
                        )
                    except Exception as conn_error:
                        if self._client_id_conflict_detected:
                            self._client_id_conflict_detected = False
                            try_client_id = self._normalize_client_id(self._next_client_id_hint)
                            connection_logger.debug(
                                "Retrying IBKR connection after conflict signalled by error handler",
                                next_client_id=try_client_id,
                            )
                            continue
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
                            next_id = self._handle_client_id_conflict(
                                try_client_id,
                                source="connect-attempt",
                                notify_client=False,
                            )
                            try_client_id = next_id
                            continue
                        elif (
                            "TimeoutError" in error_msg
                            or "timeout" in lower_error
                            or "timed out" in lower_error
                        ):
                            self._last_connect_error = f"连接超时 - 请检查TWS/Gateway是否启用API连接 (端口: {self.config.port})"
                            raise Exception(self._last_connect_error)
                        elif "ConnectionRefusedError" in error_msg or "refused" in error_msg.lower():
                            self._last_connect_error = f"连接被拒绝 - TWS/Gateway可能未运行或端口错误 (端口: {self.config.port})"
                            raise Exception(self._last_connect_error)
                        else:
                            if attempts >= self._client_id_retry_limit:
                                self._last_connect_error = f"连接失败: {error_msg}"
                                raise Exception(self._last_connect_error)
                            connection_logger.warning(f"客户端ID {try_client_id} 连接失败: {error_msg}，尝试下一个ID")

                    try_client_id = self._compute_next_client_id(try_client_id)
                    self._next_client_id_hint = try_client_id

                if not connected:
                    attempted_list = ", ".join(str(cid) for cid in sorted(attempted_ids)) or str(self.config.client_id)
                    self._last_connect_error = f"无法找到可用的客户端ID (尝试了 {attempted_list})"
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
                
                # 添加错误处理机制来捕获IBKR错误（幂等安装）
                try:
                    self._ensure_error_handler_installed(self.ib.wrapper)
                except Exception as exc:
                    connection_logger.debug(f"Failed to install error handler after connect: {exc}")

                # 添加订单状态变化事件监听器
                def order_status_handler(trade):
                    """处理订单状态变化事件"""

                    async def _process_order_status():
                        try:
                            order_id = str(trade.order.orderId)
                            status = trade.orderStatus.status
                            connection_logger.info(f"Order status update: {order_id} -> {status}")

                            connection_logger.debug(
                                "Order state updates forwarded to orders service",
                                order_id=order_id,
                                status=status,
                            )

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
                    
                self.connected = True
                account_type = "Paper" if self.config.paper_trading else "Live"
                connection_logger.info(f"Connected to IBKR {account_type} account at {self.config.host}:{self.config.port}")

                self._notify_connect_waiters()

                self._request_account_refresh("connection_established")

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

                disconnect_reason = "stop-request"
                disconnect_event = asyncio.Event()
                self._disconnect_wait_event = disconnect_event

                def _signal_disconnect(*_args: Any, **_kwargs: Any) -> None:
                    try:
                        disconnect_event.set()
                    except Exception:
                        pass

                registered_handlers: list[tuple[str, Any]] = []
                for attr in ("disconnectedEvent", "connectionClosed"):
                    event = getattr(self.ib, attr, None)
                    if event is None:
                        continue
                    try:
                        event += _signal_disconnect
                    except Exception as exc:
                        connection_logger.debug(
                            "Failed to register {} handler on IB client",
                            attr,
                            attr=attr,
                            error=str(exc),
                        )
                        continue
                    registered_handlers.append((attr, _signal_disconnect))

                # 保持事件循环运行，直到明确要求断开连接
                try:
                    while self._connection_thread_alive:
                        current_ib = self.ib
                        if not current_ib or not current_ib.isConnected():
                            disconnect_reason = "connection-health-check"
                            break

                        try:
                            await asyncio.wait_for(
                                disconnect_event.wait(),
                                timeout=self._post_connect_health_interval,
                            )
                            disconnect_reason = "ib-event"
                            break
                        except asyncio.TimeoutError:
                            if not current_ib.isConnected():
                                connection_logger.warning(
                                    "IBKR connection lost during health check window"
                                )
                                disconnect_reason = "connection-health-check"
                                break
                            continue
                        except RuntimeError as e:
                            if "Event loop stopped before Future completed" in str(e):
                                connection_logger.debug("Event loop stopped signal received")
                                disconnect_reason = "loop-stopped"
                                break
                            raise
                        except Exception as e:
                            connection_logger.warning(f"Unexpected error in connection loop: {e}")
                            if "cannot be called from a running event loop" in str(e):
                                disconnect_reason = "loop-error"
                                break
                finally:
                    for attr, handler in registered_handlers:
                        event = getattr(self.ib, attr, None)
                        if event is None:
                            continue
                        try:
                            event -= handler
                        except Exception:
                            pass
                    self._disconnect_wait_event = None

                    # 确保连接状态正确设置
                    if self.ib and self.ib.isConnected():
                        try:
                            self.ib.disconnect()
                        except Exception as e:
                            connection_logger.debug(f"Error during cleanup disconnect: {e}")
                    self.connected = False
                    self._connection_thread_alive = False
                    connection_logger.info("IBKR connection loop ended", reason=disconnect_reason)
                    try:
                        self._notify_ibkr_client_disconnected(trigger=disconnect_reason)
                    except Exception:
                        pass
            
            except Exception as e:
                connection_logger.error(f"Failed to connect to IBKR: {e}")
                self.connected = False
                # 标记错误以便主线程快速返回
                try:
                    self._last_connect_error = str(e)
                except Exception:
                    self._last_connect_error = "Unknown connection error"
                self._notify_connect_waiters()
                     
                 
        
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
                self._notify_connect_waiters()
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

        deadline = time.monotonic() + self._connect_result_timeout_sec
        event = self._connect_result_event

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            wait_for = min(self._connect_handshake_poll_interval_sec, max(0.05, remaining))
            if event is not None:
                event.wait(timeout=wait_for)
            else:
                time.sleep(wait_for)

            if self.connected:
                connection_logger.info("IBKR connection established successfully")
                return True

            if self._last_connect_error:
                connection_logger.error(f"IBKR connection failed fast: {self._last_connect_error}")
                self._force_stop_connection_thread()
                return False

            if not self._connection_thread or not self._connection_thread.is_alive():
                connection_logger.error("Connection thread died unexpectedly")
                self._last_connect_error = "Connection thread terminated unexpectedly"
                return False

        connection_logger.error(
            f"IBKR connection timeout after {self._connect_result_timeout_sec} seconds"
        )
        self._last_connect_error = (
            f"Connection timeout after {self._connect_result_timeout_sec} seconds"
        )
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
            # ``ib_insync.IB`` 在不同版本中对主机/端口信息的暴露方式并不一致。
            # 某些版本只在 ``IB.client`` 上保存这些字段，直接访问 ``IB.host``
            # 会得到 ``None``，从而被误判为配置已经变更并触发重复的重连流程。
            current_host = getattr(self.ib, "host", None)
            current_port = getattr(self.ib, "port", None)

            client = getattr(self.ib, "client", None)
            if client is not None:
                if not current_host:
                    current_host = getattr(client, "host", current_host)
                if current_port in (None, 0):
                    current_port = getattr(client, "port", current_port)

            # 兜底使用当前配置，确保缺失字段不会导致误判
            if current_host is None:
                current_host = self.config.host

            try:
                current_port_int = int(current_port)
            except (TypeError, ValueError):
                current_port_int = self.config.port

            if (
                str(current_host).strip() == str(self.config.host).strip()
                and current_port_int == int(self.config.port)
            ):
                connection_logger.info("Already connected to IBKR with same configuration")
                self._record_connect_result(True)
                return True

            connection_logger.info(
                "Configuration changed, reconnecting...",
                current_host=current_host,
                current_port=current_port_int,
            )
            self.disconnect()

        wait_logged = False
        limit_warning_logged = False
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
                        if not limit_warning_logged:
                            connection_logger.warning(
                                "Existing IBKR connection attempt exceeded wait limit; continuing to wait",
                                elapsed_seconds=round(elapsed, 1),
                                wait_limit=self._connect_wait_limit_sec,
                            )
                            limit_warning_logged = True

                if self._last_connect_error:
                    self._connect_state_cond.wait(timeout=0.1)
                    continue

                thread = self._connection_thread
                if thread and not thread.is_alive():
                    connection_logger.warning(
                        "Existing IBKR connection attempt thread is no longer running; allowing new attempt"
                    )
                    self._connect_in_progress = False
                    self._connect_attempt_started_at = None
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

        event = self._disconnect_wait_event
        if event is not None and loop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                if loop.is_running():
                    event.set()

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
        self._notify_connect_waiters()

    def disconnect(self) -> None:
        """断开与IBKR服务器的连接，并停止事件循环"""
        try:
            # 标记线程应当停止
            self._connection_thread_alive = False

            event = self._disconnect_wait_event
            loop = self._loop
            if event is not None and loop and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:
                    if loop.is_running():
                        event.set()

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

            self._mark_snapshots_stale("disconnected")
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

        except Exception as e:
            connection_logger.warning(f"Connection check failed: {e}")
            # 如果之前判定为已连接，保持结果，避免短暂错误导致的误判
            if self.connected:
                return True
            self.connected = False
        return False

    def is_connecting(self) -> bool:
        with self._connect_state_cond:
            return self._connect_in_progress

    def wait_for_connection(self, timeout: Optional[float] = None) -> bool:
        """Block until an in-flight connection attempt succeeds or finishes."""

        deadline = time.monotonic() + (timeout if timeout is not None else self._connect_wait_limit_sec)
        with self._connect_state_cond:
            while True:
                if self.connected and self.ib and self.ib.isConnected():
                    return True
                if not self._connect_in_progress:
                    return False

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False

                self._connect_state_cond.wait(timeout=min(1.0, remaining))

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
