import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Mapping
from loguru import logger

from .utils import now_local

from .telegram_notifier import TelegramNotifier
from .websocket_manager import WebSocketManager
from .cache import ensure_redis

SETTINGS_STORE_KEY = "json:notification:settings"
HISTORY_LIST_KEY = "list:notification:history"
HISTORY_MAX_ENTRIES = 500

EVENT_TOKEN_TO_LABEL = {
    'order_filled': 'Order Filled',
    'position_opened': 'Position Opened',
    'position_closed': 'Position Closed',
    'risk_rule': 'Risk Rule',
    'risk_triggered': 'Risk Rule',
    'risk_rule_deactivated': 'Risk Rule Deactivated',
    'risk_circuit_breaker': 'Risk Circuit Breaker',
    'strategy_risk': 'Strategy Risk',
    'take_profit': 'Take Profit Triggered',
    'stop_loss': 'Stop Loss Triggered',
    'strategy_started': 'Strategy Started',
    'strategy_stopped': 'Strategy Stopped',
    'queue_status': 'Queue Status',
    'queue_warning': 'Queue Warning',
    'dom_level_3': 'DOM Level 3',
    'dom_level_4': 'DOM Level 4',
    'trading_reminder': 'Trading Reminder',
    'general': 'General',
    'test': 'Test',
}

EVENT_LABEL_TO_TOKEN = {
    label.lower(): token for token, label in EVENT_TOKEN_TO_LABEL.items()
}
EVENT_LABEL_TO_TOKEN['risk rule triggered'] = 'risk_rule'
EVENT_LABEL_TO_TOKEN['stop loss triggered'] = 'stop_loss'
EVENT_LABEL_TO_TOKEN['take profit triggered'] = 'take_profit'

DEFAULT_EVENT_TOKENS: List[str] = [
    'order_filled',
    'position_opened',
    'position_closed',
    'risk_rule',
    'risk_circuit_breaker',
    'risk_rule_deactivated',
    'strategy_risk',
    'take_profit',
    'stop_loss',
    'strategy_started',
    'strategy_stopped',
    'queue_status',
    'queue_warning',
    'dom_level_3',
    'dom_level_4',
    'trading_reminder',
    'general',
    'test',
]

# 为通知管理器绑定日志记录器
notification_logger = logger.bind(module="notification_manager", request_id="-")

class NotificationManager:
    """统一的通知管理器，负责所有通知的发送和保存"""

    def __init__(
        self,
        telegram_notifier: Optional[TelegramNotifier] = None,
        websocket_manager: Optional[WebSocketManager] = None,
        redis_client: Optional[Any] = None,
        *,
        history_key: str = HISTORY_LIST_KEY,
        history_max_entries: int = HISTORY_MAX_ENTRIES,
    ):
        self.telegram_notifier = telegram_notifier
        self.websocket_manager = websocket_manager
        self._redis = redis_client
        self._history_key = history_key
        self._history_max_entries = max(0, int(history_max_entries))
        self._settings_cache: Optional[Dict[str, Any]] = None
        self._settings_expires: Optional[datetime] = None
        self._settings_lock: Optional[asyncio.Lock] = None
        self._settings_ttl_seconds: int = 45
        notification_logger.info("NotificationManager initialized")

    # ------------------------------------------------------------------
    def _normalize_event_token(self, event_type: Optional[str]) -> str:
        token = (event_type or '').strip()
        if not token:
            return 'general'

        lowered = token.lower()
        if lowered in EVENT_TOKEN_TO_LABEL:
            return lowered

        if lowered in EVENT_LABEL_TO_TOKEN:
            return EVENT_LABEL_TO_TOKEN[lowered]

        alias_map = {
            'order_update': 'order_filled',
            'auto_close': 'risk_rule',
            'dom_strategy': 'dom_level_3',
            'daily_summary': 'general',
            'connection_connected': 'general',
            'connection_disconnected': 'general',
            'connection_problem': 'general',
            'queue_started': 'queue_status',
            'queue_stopped': 'queue_status',
            'queue_state': 'queue_status',
        }
        if lowered in alias_map:
            return alias_map[lowered]

        if lowered.startswith('risk_'):
            if lowered == 'risk_rule_deactivated':
                return 'risk_rule_deactivated'
            if lowered == 'risk_circuit_breaker':
                return 'risk_circuit_breaker'
            return 'risk_rule'

        if lowered.startswith('take_profit'):
            return 'take_profit'

        if lowered.startswith('stop_loss'):
            return 'stop_loss'

        if lowered.startswith('strategy_started'):
            return 'strategy_started'

        if lowered.startswith('strategy_stopped'):
            return 'strategy_stopped'

        if lowered.startswith('queue_warning'):
            return 'queue_warning'
        if lowered.startswith('queue_'):
            return 'queue_status'

        return 'general'

    # ------------------------------------------------------------------
    def _normalize_event_list(self, events: Optional[List[Any]]) -> List[str]:
        if not isinstance(events, list) or not events:
            return DEFAULT_EVENT_TOKENS.copy()

        normalized: List[str] = []
        for event in events:
            token = self._normalize_event_token(event)
            if token not in normalized:
                normalized.append(token)

        if not normalized:
            return DEFAULT_EVENT_TOKENS.copy()
        return normalized

    def set_dependencies(
        self,
        telegram_notifier: Optional[TelegramNotifier] = None,
        websocket_manager: Optional[WebSocketManager] = None,
        redis_client: Optional[Any] = None,
    ):
        """设置依赖项"""
        if telegram_notifier:
            self.telegram_notifier = telegram_notifier
        if websocket_manager:
            self.websocket_manager = websocket_manager
        if redis_client is not None:
            self._redis = redis_client

    def _load_settings(self, raw_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """将数据库配置与默认值合并，确保返回结构完整。"""
        default_settings = {
            "chrome": {
                "enabled": True,
                "config": {},
                "events": DEFAULT_EVENT_TOKENS.copy(),
            },
            "telegram": {
                "enabled": False,
                "config": {
                    "bot_token": "",
                    "chat_id": "",
                },
                "events": DEFAULT_EVENT_TOKENS.copy(),
            },
            "trading_reminder": {
                "enabled": False,
                "start_trading_time": "09:30",
                "end_trading_time": "16:00",
                "browser_notification_enabled": True,
                "telegram_reminder_enabled": False,
            },
            "email_enabled": False,
            "email_address": "",
        }

        settings: Dict[str, Any] = dict(default_settings)
        if isinstance(raw_settings, dict):
            for key, value in raw_settings.items():
                settings[key] = value

        chrome_cfg = dict(settings.get("chrome") or {})
        chrome_cfg.setdefault("enabled", True)
        chrome_cfg.setdefault("config", {})
        chrome_cfg["events"] = self._normalize_event_list(chrome_cfg.get("events"))
        settings["chrome"] = chrome_cfg

        telegram_cfg = dict(settings.get("telegram") or {})
        telegram_cfg.setdefault("enabled", False)
        telegram_cfg.setdefault("config", {"bot_token": "", "chat_id": ""})
        telegram_cfg["events"] = self._normalize_event_list(telegram_cfg.get("events"))
        settings["telegram"] = telegram_cfg

        reminder_cfg = dict(settings.get("trading_reminder") or {})
        reminder_cfg.setdefault("enabled", False)
        reminder_cfg.setdefault("start_trading_time", "09:30")
        reminder_cfg.setdefault("end_trading_time", "16:00")
        reminder_cfg.setdefault("browser_notification_enabled", True)
        reminder_cfg.setdefault("telegram_reminder_enabled", False)
        settings["trading_reminder"] = reminder_cfg

        settings["email_enabled"] = bool(settings.get("email_enabled", False))
        settings["email_address"] = settings.get("email_address", "") or ""

        return settings

    async def _load_settings_async(self) -> Dict[str, Any]:
        """异步加载通知配置，并对结果进行缓存以减少数据库访问。"""
        now = datetime.utcnow()
        if (
            self._settings_cache is not None
            and self._settings_expires is not None
            and now < self._settings_expires
        ):
            return self._settings_cache

        if self._settings_lock is None:
            self._settings_lock = asyncio.Lock()

        async with self._settings_lock:
            # 在获取锁后再次检查缓存，避免竞争条件
            now = datetime.utcnow()
            if (
                self._settings_cache is not None
                and self._settings_expires is not None
                and now < self._settings_expires
            ):
                return self._settings_cache

            loaded: Optional[Mapping[str, Any]] = None
            redis = await self._ensure_redis()
            if redis is not None:
                try:
                    record: Optional[Any] = None
                    json_api = getattr(redis, "json", None)
                    if callable(json_api):
                        record = await json_api().get(SETTINGS_STORE_KEY)
                    else:
                        raw_value = await redis.get(SETTINGS_STORE_KEY)
                        if isinstance(raw_value, str):
                            record = json.loads(raw_value)
                    if isinstance(record, Mapping):
                        if isinstance(record.get("settings"), Mapping):
                            loaded = record["settings"]
                        else:
                            loaded = record
                except Exception as exc:
                    notification_logger.error(
                        f"Load notification settings failed: {exc}"
                    )

            settings = self._load_settings(dict(loaded) if isinstance(loaded, Mapping) else None)
            self._settings_cache = settings
            self._settings_expires = now + timedelta(seconds=self._settings_ttl_seconds)
            return settings

    def invalidate_settings_cache(self) -> None:
        """手动失效缓存，以便在配置更新后立即生效。"""
        self._settings_cache = None
        self._settings_expires = None

    async def _save_notification_history(
        self,
        *,
        channel: str,
        event_type: str,
        message: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """在后台线程中保存通知历史，避免阻塞事件循环。"""

        redis = await self._ensure_redis()
        if redis is None or not self._history_key:
            return

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stream": "manager",
            "message_id": uuid.uuid4().hex,
            "channel": channel,
            "status": status,
            "message": message,
            "event_type": event_type,
            "retries": 0,
        }
        if error_message:
            entry["error"] = error_message

        try:
            serialised = json.dumps(entry, ensure_ascii=False)
        except TypeError:
            entry.pop("message", None)
            serialised = json.dumps(entry, ensure_ascii=False)

        try:
            await redis.lpush(self._history_key, serialised)
            if self._history_max_entries:
                await redis.ltrim(self._history_key, 0, self._history_max_entries - 1)
        except Exception as exc:  # pragma: no cover - defensive logging
            notification_logger.warning(
                f"Failed to persist notification history: {exc}",
                channel=channel,
                event_type=event_type,
            )

    async def _ensure_redis(self) -> Optional[Any]:
        if self._redis is not None:
            return self._redis
        redis = await ensure_redis()
        self._redis = redis
        return redis

    async def send_notification(self, message: str, event_type: str = "general",
                               channels: List[str] = None, **kwargs) -> Dict[str, bool]:
        """发送通知到指定渠道

        Args:
            message: 通知消息
            event_type: 事件类型
            channels: 发送渠道列表，默认为['telegram', 'websocket']
            **kwargs: 额外参数
            
        Returns:
            各渠道发送结果
        """
        if channels is None:
            channels = ['telegram', 'websocket']

        # 根据设置过滤实际发送的渠道（仅在启用时才尝试发送）
        settings = await self._load_settings_async()
        event_token = self._normalize_event_token(event_type)
        telegram_enabled = bool(settings.get('telegram', {}).get('enabled', False))
        chrome_enabled = bool(settings.get('chrome', {}).get('enabled', True))

        requested = set(channels)
        allowed: List[str] = []
        telegram_events = settings.get('telegram', {}).get('events') or []
        chrome_events = settings.get('chrome', {}).get('events') or []

        def is_event_allowed(channel: str) -> bool:
            if channel == 'telegram':
                events = telegram_events
            elif channel == 'websocket':
                events = chrome_events
            else:
                return True
            if not isinstance(events, list) or not events:
                return True
            return event_token in events

        if (
            'telegram' in requested
            and self.telegram_notifier
            and telegram_enabled
            and is_event_allowed('telegram')
        ):
            allowed.append('telegram')
        if (
            'websocket' in requested
            and self.websocket_manager
            and chrome_enabled
            and is_event_allowed('websocket')
        ):
            allowed.append('websocket')

        results: Dict[str, bool] = {}
        
        # 发送Telegram通知（仅在启用时）
        if 'telegram' in allowed:
            try:
                success = await self.telegram_notifier.send_message(message)
                results['telegram'] = success
                
                # 保存到数据库（仅记录尝试发送的渠道）
                status = 'sent' if success else 'failed'
                error_msg = None if success else 'Failed to send Telegram notification'
                await self._save_notification_history(
                    channel='telegram',
                    event_type=event_type,
                    message=message,
                    status=status,
                    error_message=error_msg,
                )

                notification_logger.info(f"Telegram notification {'sent' if success else 'failed'}: {event_type}")
            except Exception as e:
                results['telegram'] = False
                notification_logger.error(f"Failed to send Telegram notification: {e}")

                # 保存错误到数据库
                await self._save_notification_history(
                    channel='telegram',
                    event_type=event_type,
                    message=message,
                    status='failed',
                    error_message=str(e),
                )
        
        # 发送WebSocket通知（仅在启用时）
        if 'websocket' in allowed:
            try:
                notification_data = {
                    "type": event_type,
                    "message": message,
                    "timestamp": now_local().isoformat(),
                    **kwargs
                }
                await self.websocket_manager.broadcast(notification_data)
                results['websocket'] = True
                
                # 保存到数据库
                await self._save_notification_history(
                    channel='websocket',
                    event_type=event_type,
                    message=message,
                    status='sent',
                )
                
                notification_logger.info(f"WebSocket notification sent: {event_type}")
            except Exception as e:
                results['websocket'] = False
                notification_logger.error(f"Failed to send WebSocket notification: {e}")
                
                # 保存错误到数据库
                await self._save_notification_history(
                    channel='websocket',
                    event_type=event_type,
                    message=message,
                    status='failed',
                    error_message=str(e),
                )

        return results
    
    async def send_order_notification(self, order_type: str, symbol: str, quantity: int,
                                    price: Optional[float], status: str, order_id: str):
        """发送订单通知"""
        # 构建消息
        if status.lower() == "filled":
            emoji = "✅"
        elif status.lower() in ["cancelled", "canceled"]:
            emoji = "❌"
        else:
            emoji = "📋"
        
        order_id_str = str(order_id)
        
        message = f"{emoji} 订单{status} {emoji}\n\n"
        message += f"类型: {order_type}\n"
        message += f"代码: {symbol}\n"
        message += f"数量: {quantity:,}\n"
        if price:
            message += f"价格: ${price:.2f}\n"
        message += f"订单ID: {order_id_str}\n"
        
        # WebSocket额外数据
        websocket_data = {
            "order_id": order_id_str,
            "symbol": symbol,
            "action": order_type,
            "quantity": quantity,
            "fill_price": price,
            "order_type": order_type,
            "executed_at": now_local().isoformat()
        }
        
        return await self.send_notification(
            message=message,
            event_type="order_filled" if status.lower() == "filled" else "order_update",
            **websocket_data
        )
    
    async def send_risk_notification(self, symbol: str, message: str, risk_type: str = "general"):
        """发送风险管理通知"""
        formatted_message = f"🚨 风险警报 🚨\n\n资产: {symbol}\n{message}"
        
        return await self.send_notification(
            message=formatted_message,
            event_type=f"risk_{risk_type}",
            symbol=symbol
        )
    
    async def send_connection_notification(self, status: str, details: str = ""):
        """发送连接状态通知"""
        if status == "connected":
            emoji = "✅"
            title = "已连接"
        elif status == "disconnected":
            emoji = "❌"
            title = "已断开"
        else:
            emoji = "⚠️"
            title = "连接问题"
        
        message = f"{emoji} IBKR {title} {emoji}\n\n"
        if details:
            message += f"详情: {details}\n\n"
        message += f"时间: {now_local().strftime('%Y-%m-%d %H:%M:%S')}"
        
        return await self.send_notification(
            message=message,
            event_type=f"connection_{status}",
            connection_status=status,
            details=details
        )
    
    async def send_stop_loss_notification(self, symbol: str, position: float, 
                                        trigger_price: float, order_type: str, order_id: str):
        """发送止损止盈通知"""
        action = "止盈" if order_type == "profit" else "止损"
        emoji = "💰" if order_type == "profit" else "🛑"
        
        order_id_str = str(order_id)
        
        message = f"{emoji} 自动{action}触发 {emoji}\n\n"
        message += f"代码: {symbol}\n"
        message += f"持仓: {position:,.0f} 股\n"
        message += f"触发价格: ${trigger_price:.2f}\n"
        message += f"订单ID: {order_id_str}\n\n"
        message += f"📈 风险管理系统执行的自动{action}订单。"
        
        event_type = "take_profit" if order_type == "profit" else "stop_loss"

        return await self.send_notification(
            message=message,
            event_type=event_type,
            symbol=symbol,
            position=position,
            trigger_price=trigger_price,
            order_id=order_id_str
        )
    
    async def send_auto_close_notification(self, symbol: str, position: float, order_id: str):
        """发送自动平仓通知"""
        order_id_str = str(order_id)
        message = f"🔴 自动平仓执行 🔴\n\n"
        message += f"代码: {symbol}\n"
        message += f"平仓数量: {position:,.0f} 股\n"
        message += f"订单ID: {order_id_str}\n\n"
        message += f"💼 由于亏损阈值触发的自动平仓。"
        
        return await self.send_notification(
            message=message,
            event_type="auto_close",
            symbol=symbol,
            position=position,
            order_id=order_id_str
        )
    
    async def send_daily_summary_notification(self, account_info: dict, positions: list):
        """发送每日摘要通知"""
        message = f"📊 每日账户摘要 📊\n\n"
        message += f"账户: {account_info.get('account_id', 'N/A')}\n"
        message += f"净清算价值: ${account_info.get('net_liquidation', 0):,.2f}\n"
        message += f"可用资金: ${account_info.get('available_funds', 0):,.2f}\n"
        message += f"未实现盈亏: ${account_info.get('unrealized_pnl', 0):,.2f}\n"
        message += f"已实现盈亏: ${account_info.get('realized_pnl', 0):,.2f}\n\n"
        
        if positions:
            message += f"活跃持仓: {len(positions)}\n"
            total_value = sum(pos.get('market_value', 0) for pos in positions)
            total_pnl = sum(pos.get('unrealized_pnl', 0) for pos in positions)
            message += f"总持仓价值: ${total_value:,.2f}\n"
            message += f"总未实现盈亏: ${total_pnl:,.2f}\n"
        else:
            message += f"无活跃持仓\n"
        
        return await self.send_notification(
            message=message,
            event_type="daily_summary",
            account_info=account_info,
            positions_count=len(positions) if positions else 0
        )
    
    async def send_test_notification(self, channel: str, message: str = "测试通知"):
        """发送测试通知"""
        channels = [channel] if channel != 'all' else ['telegram', 'websocket']
        
        return await self.send_notification(
            message=message,
            event_type="test",
            channels=channels
        )