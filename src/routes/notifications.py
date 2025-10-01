from typing import Optional, Dict, Any, List

import core
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from loguru import logger

from src.notification_manager import NotificationManager
from src.telegram_notifier import TelegramNotifier
from src.database import get_db_manager, DatabaseManager
from .auth import get_current_user

router = APIRouter()

notification_manager: NotificationManager | None = None
telegram_notifier: TelegramNotifier | None = None


def _resolve_notification_manager(request: Request) -> NotificationManager:
    if notification_manager is not None:
        return notification_manager

    container = getattr(request.app.state, "core", None)
    if container is None:
        try:
            container = core.require_container()
        except RuntimeError as exc:  # pragma: no cover - defensive guard
            raise HTTPException(status_code=503, detail="Notification manager unavailable") from exc

    manager = getattr(container, "notification_manager", None)
    if manager is None:  # pragma: no cover - unexpected configuration
        raise HTTPException(status_code=503, detail="Notification manager unavailable")
    return manager


def _resolve_telegram_notifier(request: Request) -> TelegramNotifier | None:
    if telegram_notifier is not None:
        return telegram_notifier

    container = getattr(request.app.state, "core", None)
    if container is None:
        try:
            container = core.require_container()
        except RuntimeError:
            return None

    return getattr(container, "telegram_notifier", None)

class NotificationRequest(BaseModel):
    message: str
    event_type: Optional[str] = "general"


class NotificationTestRequest(BaseModel):
    channel: str
    message: str = "Test notification from IBKR Trading Bot"

@router.post("/api/notify")
async def send_notification(
    req: NotificationRequest,
    user: Optional[str] = Depends(get_current_user),
    manager: NotificationManager = Depends(_resolve_notification_manager),
):
    """发送通知"""
    try:
        event_type = (req.event_type or "general").strip() or "general"
        results = await manager.send_notification(
            message=req.message,
            event_type=event_type,
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
async def get_notification_settings(
    request: Request,
    user: Optional[str] = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db_manager),
):
    """获取通知设置"""
    try:
        settings = db.load_notification_config()

        # 前后端事件名映射：令牌<->标签
        token_to_label = {
            'order_filled': 'Order Filled',
            'position_opened': 'Position Opened',
            'position_closed': 'Position Closed',
            'risk_triggered': 'Risk Rule',
            'risk_rule': 'Risk Rule',
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
        label_to_token = {label.lower(): token for token, label in token_to_label.items()}
        label_to_token['risk rule triggered'] = 'risk_rule'

        default_events_labels = list(dict.fromkeys(token_to_label.values()))

        def events_to_labels(events: Optional[List[Any]]) -> List[str]:
            if not isinstance(events, list):
                return default_events_labels.copy()
            labels: List[str] = []
            for item in events:
                if isinstance(item, str):
                    key = item
                    if key in token_to_label:
                        labels.append(token_to_label[key])
                        continue
                    lowered = key.lower()
                    if lowered in label_to_token:
                        labels.append(token_to_label[label_to_token[lowered]])
                        continue
                labels.append(str(item))
            return labels

        sanitized_settings = {
            "chrome": {
                "enabled": True,
                "config": {},
                "events": default_events_labels.copy(),
            },
            "telegram": {
                "enabled": False,
                "config": {
                    "bot_token": "",
                    "chat_id": "",
                },
                "events": default_events_labels.copy(),
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

        if settings:
            chrome_cfg = settings.get("chrome") or {}
            sanitized_settings["chrome"]["enabled"] = bool(chrome_cfg.get("enabled", True))
            sanitized_settings["chrome"]["config"] = dict(chrome_cfg.get("config") or {})
            sanitized_settings["chrome"]["events"] = events_to_labels(chrome_cfg.get("events"))

            telegram_cfg = settings.get("telegram") or {}
            sanitized_settings["telegram"]["enabled"] = bool(telegram_cfg.get("enabled", False))
            original_tg_config = dict(telegram_cfg.get("config") or {})
            bot_token = original_tg_config.get("bot_token", "")
            chat_id = original_tg_config.get("chat_id", "")
            sanitized_settings["telegram"]["config"] = {
                "bot_token": "***" if bot_token else "",
                "chat_id": "***" if chat_id else "",
            }
            sanitized_settings["telegram"]["events"] = events_to_labels(telegram_cfg.get("events"))

            reminder_cfg = settings.get("trading_reminder") or {}
            sanitized_settings["trading_reminder"].update({
                "enabled": bool(reminder_cfg.get("enabled", False)),
                "start_trading_time": reminder_cfg.get("start_trading_time", "09:30"),
                "end_trading_time": reminder_cfg.get("end_trading_time", "16:00"),
                "browser_notification_enabled": reminder_cfg.get("browser_notification_enabled", True),
                "telegram_reminder_enabled": reminder_cfg.get("telegram_reminder_enabled", False),
            })

            sanitized_settings["email_enabled"] = bool(settings.get("email_enabled", False))
            sanitized_settings["email_address"] = settings.get("email_address", "") or ""

        return {"success": True, "settings": sanitized_settings}
    except Exception as e:
        logger.error(f"Get notification settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/notifications/settings")
async def update_notification_settings(
    settings: Dict[str, Any],
    user: Optional[str] = Depends(get_current_user),
    db: DatabaseManager = Depends(get_db_manager),
):
    """更新通知设置"""
    try:
        # 前后端事件名映射：令牌<->标签
        token_to_label = {
            'order_filled': 'Order Filled',
            'position_opened': 'Position Opened',
            'position_closed': 'Position Closed',
            'risk_triggered': 'Risk Rule',
            'risk_rule': 'Risk Rule',
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
        label_to_token = {label: token for token, label in token_to_label.items()}
        label_to_token['Risk Rule Triggered'] = 'risk_rule'

        existing_settings = db.load_notification_config() or {}
        existing_telegram_config = (
            (existing_settings.get("telegram") or {}).get("config") or {}
        )

        # 保存前，将事件标签转换为后端存储令牌
        for ch in ("chrome", "telegram"):
            ch_cfg = settings.get(ch) or {}
            evs = ch_cfg.get("events")
            if not isinstance(evs, list):
                # 如果没有提供事件列表，默认保存所有事件（使用令牌）
                ch_cfg["events"] = list(dict.fromkeys(token_to_label.keys()))
            else:
                converted = []
                for e in evs:
                    if e in token_to_label:
                        converted.append(e)
                    else:
                        converted.append(label_to_token.get(e, e))
                ch_cfg["events"] = converted
            settings[ch] = ch_cfg

        placeholder = "***"
        telegram_cfg = settings.get("telegram") or {}
        tg_config = dict(telegram_cfg.get("config") or {})
        if isinstance(tg_config.get("bot_token"), str) and tg_config["bot_token"].strip() == placeholder:
            tg_config["bot_token"] = existing_telegram_config.get("bot_token", "")
        if isinstance(tg_config.get("chat_id"), str) and tg_config["chat_id"].strip() == placeholder:
            tg_config["chat_id"] = existing_telegram_config.get("chat_id", "")
        telegram_cfg["config"] = tg_config
        settings["telegram"] = telegram_cfg

        db.save_notification_config(settings)

        # 更新Telegram通知器配置（使用update_config以重建Bot实例）
        notifier = _resolve_telegram_notifier(request)
        if "telegram" in settings and notifier:
            telegram_config = settings.get("telegram", {})
            config = telegram_config.get("config", {})
            bot_token = (config.get("bot_token") or "").strip()
            chat_id = (config.get("chat_id") or "").strip()
            try:
                notifier.update_config(bot_token=bot_token, chat_id=chat_id)
            except Exception as e:
                logger.error(f"Update telegram notifier config failed: {e}")

        manager = _resolve_notification_manager(request)
        if hasattr(manager, "invalidate_settings_cache"):
            manager.invalidate_settings_cache()

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


@router.delete("/api/notifications/history")
async def clear_notification_history(
    user: Optional[str] = Depends(get_current_user)
):
    """清空通知历史"""
    try:
        db = get_db_manager()
        deleted = db.clear_notification_history()
        return {"success": True, "cleared": deleted}
    except Exception as e:
        logger.error(f"Clear notification history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/notifications/test")
async def test_notification(
    req: NotificationTestRequest,
    request: Request,
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

        manager = _resolve_notification_manager(request)
        results = await manager.send_notification(
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

