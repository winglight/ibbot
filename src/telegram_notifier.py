import asyncio
import os
from typing import Optional
from telegram import Bot
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

class TelegramNotifier:
    def __init__(self):
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.bot = None
        
        if self.bot_token and self.chat_id:
            self.bot = Bot(token=self.bot_token)
            logger.info("Telegram notifier initialized")
        else:
            logger.warning("Telegram credentials not found. Notifications disabled.")
    
    async def send_message(self, message: str) -> bool:
        """发送Telegram消息"""
        if not self.bot or not self.chat_id:
            logger.warning("Telegram not configured. Message not sent.")
            return False
        
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=message, parse_mode='HTML')
            logger.info("Telegram message sent successfully")
            return True
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return False
    
    async def send_loss_alert(self, symbol: str, position: float, current_price: float, 
                            avg_cost: float, loss_percent: float, threshold: float):
        """发送亏损警报"""
        loss_amount = position * (avg_cost - current_price)
        
        message = f"🚨 <b>Loss Alert</b> 🚨\n\n"
        message += f"<b>Symbol:</b> {symbol}\n"
        message += f"<b>Position:</b> {position:,.0f} shares\n"
        message += f"<b>Avg Cost:</b> ${avg_cost:.2f}\n"
        message += f"<b>Current Price:</b> ${current_price:.2f}\n"
        message += f"<b>Loss:</b> ${loss_amount:,.2f} ({loss_percent:.2f}%)\n"
        message += f"<b>Threshold:</b> {threshold*100:.1f}%\n\n"
        message += f"⚠️ Loss threshold exceeded!"
        
        await self.send_message(message)
    
    async def send_auto_close_alert(self, symbol: str, position: float, order_id: int):
        """发送自动平仓警报"""
        message = f"🔴 <b>Auto Close Executed</b> 🔴\n\n"
        message += f"<b>Symbol:</b> {symbol}\n"
        message += f"<b>Position Closed:</b> {position:,.0f} shares\n"
        message += f"<b>Order ID:</b> {order_id}\n\n"
        message += f"💼 Position automatically closed due to loss threshold."
        
        await self.send_message(message)
    
    async def send_stop_loss_alert(self, symbol: str, position: float, trigger_price: float, 
                                 order_type: str, order_id: int):
        """发送止盈止损警报"""
        action = "Take Profit" if order_type == "profit" else "Stop Loss"
        emoji = "💰" if order_type == "profit" else "🛑"
        
        message = f"{emoji} <b>{action} Triggered</b> {emoji}\n\n"
        message += f"<b>Symbol:</b> {symbol}\n"
        message += f"<b>Position:</b> {position:,.0f} shares\n"
        message += f"<b>Trigger Price:</b> ${trigger_price:.2f}\n"
        message += f"<b>Order ID:</b> {order_id}\n\n"
        message += f"📈 {action} order executed."
        
        await self.send_message(message)
    
    async def send_connection_alert(self, status: str, details: str = ""):
        """发送连接状态警报"""
        if status == "connected":
            emoji = "✅"
            title = "Connected"
        elif status == "disconnected":
            emoji = "❌"
            title = "Disconnected"
        else:
            emoji = "⚠️"
            title = "Connection Issue"
        
        message = f"{emoji} <b>IBKR {title}</b> {emoji}\n\n"
        if details:
            message += f"<b>Details:</b> {details}\n\n"
        message += f"🕐 Time: {asyncio.get_event_loop().time()}"
        
        await self.send_message(message)
    
    async def send_order_alert(self, order_type: str, symbol: str, quantity: int, 
                             price: Optional[float], status: str, order_id: int):
        """发送订单状态警报"""
        if status.lower() == "filled":
            emoji = "✅"
        elif status.lower() in ["cancelled", "canceled"]:
            emoji = "❌"
        else:
            emoji = "📋"
        
        message = f"{emoji} <b>Order {status}</b> {emoji}\n\n"
        message += f"<b>Type:</b> {order_type}\n"
        message += f"<b>Symbol:</b> {symbol}\n"
        message += f"<b>Quantity:</b> {quantity:,}\n"
        if price:
            message += f"<b>Price:</b> ${price:.2f}\n"
        message += f"<b>Order ID:</b> {order_id}\n"
        
        await self.send_message(message)
    
    async def send_daily_summary(self, account_info: dict, positions: list):
        """发送每日账户摘要"""
        message = f"📊 <b>Daily Account Summary</b> 📊\n\n"
        message += f"<b>Account:</b> {account_info.get('account_id', 'N/A')}\n"
        message += f"<b>Net Liquidation:</b> ${account_info.get('net_liquidation', 0):,.2f}\n"
        message += f"<b>Available Funds:</b> ${account_info.get('available_funds', 0):,.2f}\n"
        message += f"<b>Unrealized P&L:</b> ${account_info.get('unrealized_pnl', 0):,.2f}\n"
        message += f"<b>Realized P&L:</b> ${account_info.get('realized_pnl', 0):,.2f}\n\n"
        
        if positions:
            message += f"<b>Active Positions:</b> {len(positions)}\n"
            total_value = sum(pos.get('market_value', 0) for pos in positions)
            total_pnl = sum(pos.get('unrealized_pnl', 0) for pos in positions)
            message += f"<b>Total Position Value:</b> ${total_value:,.2f}\n"
            message += f"<b>Total Unrealized P&L:</b> ${total_pnl:,.2f}\n"
        else:
            message += f"<b>No active positions</b>\n"
        
        await self.send_message(message)