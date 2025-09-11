from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from loguru import logger

from core import notification_manager, telegram_notifier
from .auth import get_current_user
from src.database import get_db_manager

router = APIRouter()

class NotificationRequest(BaseModel):
    message: str


class NotificationTestRequest(BaseModel):
    channel: str
    message: str = "Test notification from IBKR Trading Bot"

@router.post("/api/notify")
async def send_notification(req: NotificationRequest, user: Optional[str] = Depends(get_current_user)):
    """发送通知"""
    try:
        results = await notification_manager.send_notification(
            message=req.message,
            event_type="test",
            category="api"
        )
        success = any(results.values()) if isinstance(results, dict) else bool(results)
        if not success:
            # 返回200状态码但标记为失败，避免前端报错
            return {"success": False, "message": "Notification disabled or failed to send"}
        return {"success": True}
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        # 返回200状态码但包含错误信息，避免前端报错
        return {"success": False, "message": f"Error: {str(e)}"}


@router.get("/api/notifications/settings")
async def get_notification_settings(user: Optional[str] = Depends(get_current_user)):
    """获取通知设置"""
    try:
        db = get_db_manager()
        settings = db.load_notification_config()

        # 前后端事件名映射：令牌<->标签
        token_to_label = {
            'order_filled': 'Order Filled',
            'position_opened': 'Position Opened',
            'position_closed': 'Position Closed',
            'risk_triggered': 'Risk Rule Triggered',
            'strategy_started': 'Strategy Started',
            'strategy_stopped': 'Strategy Stopped',
        }
        label_to_token = {v: k for k, v in token_to_label.items()}

        # 如果没有设置，返回默认配置（使用前端展示的事件标签）
        if not settings:
            default_events_labels = list(token_to_label.values())
            settings = {
                "chrome": {
                    "enabled": True,
                    "config": {},
                    "events": default_events_labels
                },
                "telegram": {
                    "enabled": False,
                    "config": {
                        "bot_token": "",
                        "chat_id": ""
                    },
                    "events": default_events_labels
                },
                "trading_reminder": {
                    "enabled": False,
                    "start_trading_time": "09:30",
                    "end_trading_time": "16:00",
                    "browser_notification_enabled": True,
                    "telegram_reminder_enabled": False
                },
                # 前端需要的邮件配置默认字段（兼容旧版本）
                "email_enabled": False,
                "email_address": ""
            }
        else:
            # 兼容旧数据：补齐email字段
            settings.setdefault("email_enabled", False)
            settings.setdefault("email_address", "")
            # 将存储的事件（可能为令牌）转换为前端标签
            for ch in ("chrome", "telegram"):
                ch_cfg = settings.get(ch) or {}
                evs = ch_cfg.get("events")
                # 如果缺失或为空则使用默认事件标签
                if not isinstance(evs, list) or not evs:
                    ch_cfg["events"] = list(token_to_label.values())
                else:
                    # 若存储的是令牌则转换为标签
                    if all(e in token_to_label for e in evs):
                        ch_cfg["events"] = [token_to_label.get(e, e) for e in evs]
                settings[ch] = ch_cfg
        
        return {"success": True, "settings": settings}
    except Exception as e:
        logger.error(f"Get notification settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/notifications/settings")
async def update_notification_settings(settings: Dict[str, Any], user: Optional[str] = Depends(get_current_user)):
    """更新通知设置"""
    try:
        # 前后端事件名映射：令牌<->标签
        token_to_label = {
            'order_filled': 'Order Filled',
            'position_opened': 'Position Opened',
            'position_closed': 'Position Closed',
            'risk_triggered': 'Risk Rule Triggered',
            'strategy_started': 'Strategy Started',
            'strategy_stopped': 'Strategy Stopped',
        }
        label_to_token = {v: k for k, v in token_to_label.items()}

        # 保存前，将事件标签转换为后端存储令牌
        for ch in ("chrome", "telegram"):
            ch_cfg = settings.get(ch) or {}
            evs = ch_cfg.get("events")
            if not isinstance(evs, list) or not evs:
                # 如果没有提供事件列表，默认保存所有事件（使用令牌）
                ch_cfg["events"] = list(token_to_label.keys())
            else:
                # 将事件标签转换为存储令牌
                if not all(e in token_to_label for e in evs):
                    ch_cfg["events"] = [label_to_token.get(e, e) for e in evs]
            settings[ch] = ch_cfg
        
        db = get_db_manager()
        db.save_notification_config(settings)
        
        # 更新Telegram通知器配置（使用update_config以重建Bot实例）
        if "telegram" in settings and telegram_notifier:
            telegram_config = settings.get("telegram", {})
            config = telegram_config.get("config", {})
            bot_token = (config.get("bot_token") or "").strip()
            chat_id = (config.get("chat_id") or "").strip()
            try:
                telegram_notifier.update_config(bot_token=bot_token, chat_id=chat_id)
            except Exception as e:
                logger.error(f"Update telegram notifier config failed: {e}")
        
        return {"success": True, "message": "Notification settings updated"}
    except Exception as e:
        logger.error(f"Update notification settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/notifications/history")
async def get_notification_history(
    channel: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    user: Optional[str] = Depends(get_current_user)
):
    """获取通知历史"""
    try:
        db = get_db_manager()
        history = db.load_notification_history(
            channel=channel,
            status=status,
            since=since,
            limit=limit
        )
        
        return {"success": True, "history": history}
    except Exception as e:
        logger.error(f"Get notification history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/notifications/test")
async def test_notification(
    req: NotificationTestRequest,
    user: Optional[str] = Depends(get_current_user)
):
    """测试通知发送"""
    try:
        # 将前端的 'chrome' 映射为后端的 'websocket' 渠道
        if req.channel == "all":
            channels = ["telegram", "websocket"]
        elif req.channel == "chrome":
            channels = ["websocket"]
        else:
            channels = [req.channel]

        results = await notification_manager.send_notification(
            message=req.message,
            event_type="test",
            channels=channels,
            category="api"
        )

        success = any(results.values()) if isinstance(results, dict) else bool(results)
        error_msg = None if success else f"Failed to send {req.channel} notification"

        return {
            "success": success,
            "message": "Test notification sent" if success else error_msg
        }
    except Exception as e:
        logger.error(f"Test notification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

