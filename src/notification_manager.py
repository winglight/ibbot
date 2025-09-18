import asyncio
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from loguru import logger

from .utils import now_local

from .telegram_notifier import TelegramNotifier
from .websocket_manager import WebSocketManager
from .database import DatabaseManager

# 为通知管理器绑定日志记录器
notification_logger = logger.bind(module="notification_manager", request_id="-")

class NotificationManager:
    """统一的通知管理器，负责所有通知的发送和保存"""
    
    def __init__(self, telegram_notifier: Optional[TelegramNotifier] = None, 
                 websocket_manager: Optional[WebSocketManager] = None,
                 db_manager: Optional[DatabaseManager] = None):
        self.telegram_notifier = telegram_notifier
        self.websocket_manager = websocket_manager
        self.db_manager = db_manager
        notification_logger.info("NotificationManager initialized")
    
    def set_dependencies(self, telegram_notifier: Optional[TelegramNotifier] = None,
                        websocket_manager: Optional[WebSocketManager] = None,
                        db_manager: Optional[DatabaseManager] = None):
        """设置依赖项"""
        if telegram_notifier:
            self.telegram_notifier = telegram_notifier
        if websocket_manager:
            self.websocket_manager = websocket_manager
        if db_manager:
            self.db_manager = db_manager

    def _load_settings(self) -> Dict[str, Any]:
        """加载通知配置（若未配置则返回默认），默认值需与后端保持一致。"""
        default_events = [
            'Order Filled',
            'Position Opened',
            'Position Closed',
            'Risk Rule',
            'Risk Rule Deactivated',
            'Take Profit Triggered',
            'Stop Loss Triggered',
            'Strategy Started',
            'Strategy Stopped',
            'DOM Level 3',
            'DOM Level 4',
            'Trading Reminder',
            'General'
        ]
        default_settings = {
            "chrome": {
                "enabled": True,
                "config": {},
                "events": default_events
            },
            "telegram": {
                "enabled": False,
                "config": {
                    "bot_token": "",
                    "chat_id": ""
                },
                "events": default_events
            },
            "trading_reminder": {
                "enabled": False,
                "start_trading_time": "09:30",
                "end_trading_time": "16:00",
                "browser_notification_enabled": True,
                "telegram_reminder_enabled": False
            },
            "email_enabled": False,
            "email_address": ""
        }
        try:
            if self.db_manager:
                loaded = self.db_manager.load_notification_config()
                if isinstance(loaded, dict) and loaded:
                    return loaded
        except Exception as e:
            notification_logger.error(f"Load notification settings failed: {e}")
        return default_settings

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
        settings = self._load_settings()
        telegram_enabled = bool(settings.get('telegram', {}).get('enabled', False))
        chrome_enabled = bool(settings.get('chrome', {}).get('enabled', True))

        requested = set(channels)
        allowed: List[str] = []
        if 'telegram' in requested and self.telegram_notifier and telegram_enabled:
            allowed.append('telegram')
        if 'websocket' in requested and self.websocket_manager and chrome_enabled:
            allowed.append('websocket')
        
        results: Dict[str, bool] = {}
        
        # 发送Telegram通知（仅在启用时）
        if 'telegram' in allowed:
            try:
                success = await self.telegram_notifier.send_message(message)
                results['telegram'] = success
                
                # 保存到数据库（仅记录尝试发送的渠道）
                if self.db_manager:
                    status = 'sent' if success else 'failed'
                    error_msg = None if success else 'Failed to send Telegram notification'
                    self.db_manager.save_notification_history(
                        channel='telegram',
                        event_type=event_type,
                        message=message,
                        status=status,
                        error_message=error_msg
                    )
                
                notification_logger.info(f"Telegram notification {'sent' if success else 'failed'}: {event_type}")
            except Exception as e:
                results['telegram'] = False
                notification_logger.error(f"Failed to send Telegram notification: {e}")
                
                # 保存错误到数据库
                if self.db_manager:
                    self.db_manager.save_notification_history(
                        channel='telegram',
                        event_type=event_type,
                        message=message,
                        status='failed',
                        error_message=str(e)
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
                if self.db_manager:
                    self.db_manager.save_notification_history(
                        channel='websocket',
                        event_type=event_type,
                        message=message,
                        status='sent'
                    )
                
                notification_logger.info(f"WebSocket notification sent: {event_type}")
            except Exception as e:
                results['websocket'] = False
                notification_logger.error(f"Failed to send WebSocket notification: {e}")
                
                # 保存错误到数据库
                if self.db_manager:
                    self.db_manager.save_notification_history(
                        channel='websocket',
                        event_type=event_type,
                        message=message,
                        status='failed',
                        error_message=str(e)
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