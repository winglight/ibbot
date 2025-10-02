from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from src.cache import ensure_redis
from src.routes.service_clients import (
    NotificationServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)
from src.utils import now_local

from .auth import get_current_user

router = APIRouter()

_NOTIFICATION_SERVICE_CLIENT: NotificationServiceClient | None = NotificationServiceClient()


def set_notification_service_client(client: NotificationServiceClient | None) -> None:
    """Override the default notification service client (mainly for tests)."""

    global _NOTIFICATION_SERVICE_CLIENT
    _NOTIFICATION_SERVICE_CLIENT = client


def _get_notification_service_client() -> NotificationServiceClient | None:
    return _NOTIFICATION_SERVICE_CLIENT


SETTINGS_STORE_KEY = "json:notification:settings"
CONFIG_HASH_KEY = "hash:config:notification"
CONFIG_MODULE_SET_KEY = "set:config:modules"
CONFIG_UPDATE_CHANNEL = "pubsub:config:update"
SETTINGS_VERSION = "2024.09"

EVENT_TOKEN_TO_LABEL: Dict[str, str] = {
    "order_filled": "Order Filled",
    "position_opened": "Position Opened",
    "position_closed": "Position Closed",
    "risk_rule": "Risk Rule",
    "risk_triggered": "Risk Rule",
    "risk_rule_deactivated": "Risk Rule Deactivated",
    "risk_circuit_breaker": "Risk Circuit Breaker",
    "strategy_risk": "Strategy Risk",
    "take_profit": "Take Profit Triggered",
    "stop_loss": "Stop Loss Triggered",
    "strategy_started": "Strategy Started",
    "strategy_stopped": "Strategy Stopped",
    "queue_status": "Queue Status",
    "queue_warning": "Queue Warning",
    "dom_level_3": "DOM Level 3",
    "dom_level_4": "DOM Level 4",
    "trading_reminder": "Trading Reminder",
    "general": "General",
    "test": "Test",
}
EVENT_LABEL_TO_TOKEN: Dict[str, str] = {label.lower(): token for token, label in EVENT_TOKEN_TO_LABEL.items()}
EVENT_LABEL_TO_TOKEN["risk rule triggered"] = "risk_rule"
EVENT_LABEL_TO_TOKEN["risk rule"] = "risk_rule"

DEFAULT_EVENT_TOKENS: List[str] = [
    "order_filled",
    "position_opened",
    "position_closed",
    "risk_rule",
    "risk_circuit_breaker",
    "risk_rule_deactivated",
    "strategy_risk",
    "take_profit",
    "stop_loss",
    "strategy_started",
    "strategy_stopped",
    "queue_status",
    "queue_warning",
    "dom_level_3",
    "dom_level_4",
    "trading_reminder",
    "general",
    "test",
]
DEFAULT_EVENT_LABELS: List[str] = [EVENT_TOKEN_TO_LABEL[token] for token in DEFAULT_EVENT_TOKENS]


class NotificationRequest(BaseModel):
    message: str
    event_type: Optional[str] = "general"


class NotificationTestRequest(BaseModel):
    channel: str
    message: str = "Test notification from IBKR Trading Bot"


def _bool_from_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _default_storage_settings() -> Dict[str, Any]:
    return {
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


def _merge_existing_settings(existing: Mapping[str, Any] | None) -> Dict[str, Any]:
    merged = _default_storage_settings()
    if not isinstance(existing, Mapping):
        return merged

    chrome_existing = existing.get("chrome")
    if isinstance(chrome_existing, Mapping):
        chrome_cfg = merged["chrome"]
        chrome_cfg["enabled"] = _bool_from_value(chrome_existing.get("enabled"), chrome_cfg["enabled"])
        if isinstance(chrome_existing.get("config"), Mapping):
            config = dict(chrome_cfg.get("config") or {})
            config.update({k: v for k, v in chrome_existing["config"].items()})
            chrome_cfg["config"] = config
        if isinstance(chrome_existing.get("events"), list):
            chrome_cfg["events"] = list(chrome_existing["events"])

    telegram_existing = existing.get("telegram")
    if isinstance(telegram_existing, Mapping):
        telegram_cfg = merged["telegram"]
        telegram_cfg["enabled"] = _bool_from_value(telegram_existing.get("enabled"), telegram_cfg["enabled"])
        if isinstance(telegram_existing.get("config"), Mapping):
            config = dict(telegram_cfg.get("config") or {})
            config.update({k: v for k, v in telegram_existing["config"].items()})
            telegram_cfg["config"] = config
        if isinstance(telegram_existing.get("events"), list):
            telegram_cfg["events"] = list(telegram_existing["events"])

    reminder_existing = existing.get("trading_reminder")
    if isinstance(reminder_existing, Mapping):
        reminder_cfg = merged["trading_reminder"]
        reminder_cfg.update({k: v for k, v in reminder_existing.items()})

    if "email_enabled" in existing:
        merged["email_enabled"] = _bool_from_value(existing.get("email_enabled"), merged["email_enabled"])
    if "email_address" in existing:
        merged["email_address"] = str(existing.get("email_address") or "").strip()

    return merged


def _events_to_labels(events: Any) -> List[str]:
    if not isinstance(events, list) or not events:
        return DEFAULT_EVENT_LABELS.copy()
    labels: List[str] = []
    for item in events:
        if isinstance(item, str):
            candidate = item.strip()
            if not candidate:
                continue
            if candidate in EVENT_TOKEN_TO_LABEL:
                label = EVENT_TOKEN_TO_LABEL[candidate]
            else:
                lowered = candidate.lower()
                token = EVENT_LABEL_TO_TOKEN.get(lowered)
                label = EVENT_TOKEN_TO_LABEL.get(token, candidate)
            if label not in labels:
                labels.append(label)
            continue
        labels.append(str(item))
    if not labels:
        return DEFAULT_EVENT_LABELS.copy()
    return labels


def _labels_to_tokens(events: Any) -> List[str]:
    if not isinstance(events, list) or not events:
        return DEFAULT_EVENT_TOKENS.copy()
    tokens: List[str] = []
    for item in events:
        if isinstance(item, str):
            candidate = item.strip()
            if not candidate:
                continue
            if candidate in EVENT_TOKEN_TO_LABEL:
                token = candidate
            else:
                token = EVENT_LABEL_TO_TOKEN.get(candidate.lower(), candidate)
            if token not in tokens:
                tokens.append(token)
        else:
            tokens.append(str(item))
    if not tokens:
        return DEFAULT_EVENT_TOKENS.copy()
    return tokens


def _sanitize_settings(raw_settings: Mapping[str, Any] | None) -> Dict[str, Any]:
    merged = _merge_existing_settings(raw_settings)

    chrome_cfg = merged.get("chrome", {})
    chrome_cfg["enabled"] = _bool_from_value(chrome_cfg.get("enabled"), True)
    chrome_cfg["config"] = dict(chrome_cfg.get("config") or {})
    chrome_cfg["events"] = _events_to_labels(chrome_cfg.get("events"))

    telegram_cfg = merged.get("telegram", {})
    telegram_cfg["enabled"] = _bool_from_value(telegram_cfg.get("enabled"), False)
    raw_tg_config = dict(telegram_cfg.get("config") or {})
    bot_token = str(raw_tg_config.get("bot_token") or "").strip()
    chat_id = str(raw_tg_config.get("chat_id") or "").strip()
    telegram_cfg["config"] = {
        "bot_token": "***" if bot_token else "",
        "chat_id": "***" if chat_id else "",
    }
    telegram_cfg["events"] = _events_to_labels(telegram_cfg.get("events"))

    reminder_cfg = merged.get("trading_reminder", {})
    reminder_cfg["enabled"] = _bool_from_value(reminder_cfg.get("enabled"), False)
    reminder_cfg["start_trading_time"] = str(reminder_cfg.get("start_trading_time") or "09:30")
    reminder_cfg["end_trading_time"] = str(reminder_cfg.get("end_trading_time") or "16:00")
    reminder_cfg["browser_notification_enabled"] = _bool_from_value(
        reminder_cfg.get("browser_notification_enabled"), True
    )
    reminder_cfg["telegram_reminder_enabled"] = _bool_from_value(
        reminder_cfg.get("telegram_reminder_enabled"), False
    )

    sanitized = {
        "chrome": chrome_cfg,
        "telegram": telegram_cfg,
        "trading_reminder": reminder_cfg,
        "email_enabled": _bool_from_value(merged.get("email_enabled"), False),
        "email_address": str(merged.get("email_address") or ""),
    }
    return sanitized


def _prepare_storage_settings(
    payload: Mapping[str, Any], existing: Mapping[str, Any] | None
) -> Dict[str, Any]:
    prepared = _merge_existing_settings(existing)
    if not isinstance(payload, Mapping):
        return prepared

    for channel_name in ("chrome", "telegram"):
        channel_payload = payload.get(channel_name)
        if not isinstance(channel_payload, Mapping):
            continue
        channel_cfg = prepared[channel_name]
        channel_cfg["enabled"] = _bool_from_value(
            channel_payload.get("enabled"), channel_cfg.get("enabled", False)
        )
        channel_cfg["events"] = _labels_to_tokens(channel_payload.get("events"))
        if isinstance(channel_payload.get("config"), Mapping):
            config = dict(channel_cfg.get("config") or {})
            for key, value in channel_payload["config"].items():
                if channel_name == "telegram" and key in {"bot_token", "chat_id"}:
                    text_value = str(value or "").strip()
                    if text_value == "***":
                        continue
                    config[key] = text_value
                else:
                    config[key] = value if isinstance(value, (dict, list)) else str(value or "")
            channel_cfg["config"] = config
        prepared[channel_name] = channel_cfg

    reminder_payload = payload.get("trading_reminder")
    if isinstance(reminder_payload, Mapping):
        reminder_cfg = prepared["trading_reminder"]
        if "enabled" in reminder_payload:
            reminder_cfg["enabled"] = _bool_from_value(
                reminder_payload.get("enabled"), reminder_cfg.get("enabled", False)
            )
        if "start_trading_time" in reminder_payload:
            reminder_cfg["start_trading_time"] = (
                str(reminder_payload.get("start_trading_time") or "").strip()
                or reminder_cfg.get("start_trading_time", "09:30")
            )
        if "end_trading_time" in reminder_payload:
            reminder_cfg["end_trading_time"] = (
                str(reminder_payload.get("end_trading_time") or "").strip()
                or reminder_cfg.get("end_trading_time", "16:00")
            )
        if "browser_notification_enabled" in reminder_payload:
            reminder_cfg["browser_notification_enabled"] = _bool_from_value(
                reminder_payload.get("browser_notification_enabled"),
                reminder_cfg.get("browser_notification_enabled", True),
            )
        if "telegram_reminder_enabled" in reminder_payload:
            reminder_cfg["telegram_reminder_enabled"] = _bool_from_value(
                reminder_payload.get("telegram_reminder_enabled"),
                reminder_cfg.get("telegram_reminder_enabled", False),
            )
        prepared["trading_reminder"] = reminder_cfg

    if "email_enabled" in payload:
        prepared["email_enabled"] = _bool_from_value(payload.get("email_enabled"), prepared.get("email_enabled", False))
    if "email_address" in payload:
        prepared["email_address"] = str(payload.get("email_address") or "").strip()

    prepared["chrome"]["events"] = _labels_to_tokens(prepared["chrome"].get("events"))
    prepared["telegram"]["events"] = _labels_to_tokens(prepared["telegram"].get("events"))
    return prepared


async def _load_settings(redis: Any) -> tuple[Dict[str, Any], Optional[str]]:
    data: Any = None
    updated_at: Optional[str] = None

    json_accessor = getattr(redis, "json", None)
    if callable(json_accessor):
        try:
            data = await json_accessor().get(SETTINGS_STORE_KEY)
        except Exception as exc:  # pragma: no cover - redis module mismatch
            logger.warning("Failed to load notification settings via RedisJSON: {}", exc)

    if data is None:
        try:
            raw = await redis.get(SETTINGS_STORE_KEY)
        except AttributeError:
            raw = None
        except Exception as exc:  # pragma: no cover - connection issues
            logger.warning("Failed to read notification settings", error=str(exc))
            raw = None
        if raw:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    data = {}

    if not isinstance(data, Mapping):
        data = {}

    if "settings" in data and isinstance(data["settings"], Mapping):
        settings = dict(data["settings"])
        updated_raw = data.get("updated_at")
        if isinstance(updated_raw, str):
            updated_at = updated_raw
    else:
        settings = {k: v for k, v in data.items() if k != "updated_at"}
        updated_raw = data.get("updated_at")
        if isinstance(updated_raw, str):
            updated_at = updated_raw

    return settings, updated_at


async def _persist_settings(redis: Any, settings: Mapping[str, Any]) -> Dict[str, Any]:
    record = {
        "settings": settings,
        "updated_at": now_local().isoformat(),
        "version": SETTINGS_VERSION,
    }
    stored = False
    json_accessor = getattr(redis, "json", None)
    if callable(json_accessor):
        try:
            await json_accessor().set(SETTINGS_STORE_KEY, "$", record)
            stored = True
        except Exception as exc:  # pragma: no cover - redis module mismatch
            logger.warning("Failed to persist notification settings via RedisJSON: {}", exc)

    if not stored:
        try:
            payload = json.dumps(record, ensure_ascii=False)
            await redis.set(SETTINGS_STORE_KEY, payload)
            stored = True
        except Exception as exc:
            logger.error("Failed to persist notification settings", error=str(exc))
            raise HTTPException(status_code=502, detail="Failed to persist notification settings") from exc

    return record


async def _persist_config_view(redis: Any, settings: Mapping[str, Any]) -> None:
    telegram_cfg = settings.get("telegram") if isinstance(settings, Mapping) else None
    telegram_map = telegram_cfg if isinstance(telegram_cfg, Mapping) else {}
    telegram_conf = telegram_map.get("config") if isinstance(telegram_map, Mapping) else {}
    chrome_cfg = settings.get("chrome") if isinstance(settings, Mapping) else None
    chrome_map = chrome_cfg if isinstance(chrome_cfg, Mapping) else {}
    reminder_cfg = settings.get("trading_reminder") if isinstance(settings, Mapping) else None
    reminder_map = reminder_cfg if isinstance(reminder_cfg, Mapping) else {}

    mapping: Dict[str, str] = {
        "telegram_enabled": "1" if _bool_from_value(telegram_map.get("enabled"), False) else "0",
        "telegram_bot_token": str((telegram_conf or {}).get("bot_token") or ""),
        "telegram_chat_id": str((telegram_conf or {}).get("chat_id") or ""),
        "email_enabled": "1" if _bool_from_value(settings.get("email_enabled"), False) else "0",
        "email_address": str(settings.get("email_address") or ""),
        "websocket_enabled": "1" if _bool_from_value(chrome_map.get("enabled"), True) else "0",
        "trading_reminder_enabled": "1" if _bool_from_value(reminder_map.get("enabled"), False) else "0",
        "trading_reminder_start": str(reminder_map.get("start_trading_time") or ""),
        "trading_reminder_end": str(reminder_map.get("end_trading_time") or ""),
        "chrome_events": json.dumps(chrome_map.get("events") or [], ensure_ascii=False),
        "telegram_events": json.dumps(telegram_map.get("events") or [], ensure_ascii=False),
    }

    try:
        await redis.hset(CONFIG_HASH_KEY, mapping=mapping)
    except Exception as exc:
        logger.warning("Failed to update notification config hash", error=str(exc))

    try:
        await redis.sadd(CONFIG_MODULE_SET_KEY, "notification")
    except Exception as exc:  # pragma: no cover - depends on redis features
        logger.debug("Failed to register notification module", error=str(exc))

    try:
        await redis.publish(CONFIG_UPDATE_CHANNEL, "notification")
    except Exception as exc:
        logger.warning("Failed to publish notification config update", error=str(exc))


def _interpret_service_response(payload: Any) -> tuple[bool, Optional[str]]:
    if isinstance(payload, Mapping):
        success = bool(payload.get("success"))
        message = payload.get("message")
        results = payload.get("results")
        if not success and isinstance(results, Mapping):
            success = any(bool(value) for value in results.values())
        if not success and not message:
            detail = payload.get("detail")
            if isinstance(detail, str):
                message = detail
        return success, message if isinstance(message, str) else None
    return bool(payload), None


async def _dispatch_notification(
    client: NotificationServiceClient | None,
    payload: Mapping[str, Any],
    *,
    test_mode: bool = False,
) -> Dict[str, Any]:
    if client is None:
        return {"success": False, "message": "Notification service not enabled"}

    try:
        response = await (client.test(payload) if test_mode else client.dispatch(payload))
    except ServiceUnavailableError:
        return {"success": False, "message": "Notification service not enabled"}
    except ServiceResponseError as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        logger.error(
            "Notification service responded with an error",
            status=exc.status_code,
            detail=detail,
        )
        return {"success": False, "message": detail or "Notification service request failed"}
    except ServiceClientError as exc:
        logger.error("Notification service request failed", error=str(exc))
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unexpected notification dispatch failure")
        return {"success": False, "message": f"Error: {exc}"}

    success, message = _interpret_service_response(response)
    result: Dict[str, Any] = {"success": success}
    if message:
        result["message"] = message
    if isinstance(response, Mapping):
        if "results" in response:
            result["results"] = response["results"]
        if "details" in response:
            result["details"] = response["details"]
    if not success and "message" not in result:
        result["message"] = "Notification dispatch failed"
    return result


def _normalise_event_type(event_type: Optional[str]) -> str:
    if not isinstance(event_type, str):
        return "general"
    candidate = event_type.strip()
    if not candidate:
        return "general"
    if candidate in EVENT_TOKEN_TO_LABEL:
        return candidate
    return EVENT_LABEL_TO_TOKEN.get(candidate.lower(), candidate)


@router.post("/api/notify")
async def send_notification(
    req: NotificationRequest,
    user: Optional[str] = Depends(get_current_user),
    client: NotificationServiceClient | None = Depends(_get_notification_service_client),
) -> Dict[str, Any]:
    payload = {
        "message": req.message,
        "event_type": _normalise_event_type(req.event_type),
        "category": "api",
    }
    result = await _dispatch_notification(client, payload, test_mode=False)
    return result


@router.get("/api/notifications/settings")
async def get_notification_settings(
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=503, detail="Notification config store unavailable")

    raw_settings, updated_at = await _load_settings(redis)
    sanitized = _sanitize_settings(raw_settings)

    response: Dict[str, Any] = {
        "success": True,
        "data": sanitized,
        "version": SETTINGS_VERSION,
    }
    if updated_at:
        response["updated_at"] = updated_at
    return response


@router.put("/api/notifications/settings")
async def update_notification_settings(
    settings: Dict[str, Any],
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=503, detail="Notification config store unavailable")

    existing_settings, _ = await _load_settings(redis)
    prepared = _prepare_storage_settings(settings, existing_settings)
    record = await _persist_settings(redis, prepared)
    await _persist_config_view(redis, prepared)

    sanitized = _sanitize_settings(prepared)
    response: Dict[str, Any] = {
        "success": True,
        "message": "Notification settings updated",
        "data": sanitized,
        "version": SETTINGS_VERSION,
    }
    if record.get("updated_at"):
        response["updated_at"] = record["updated_at"]
    return response


@router.get("/api/notifications/history")
async def get_notification_history(
    channel: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    user: Optional[str] = Depends(get_current_user),
    client: NotificationServiceClient | None = Depends(_get_notification_service_client),
) -> Dict[str, Any]:
    if client is None:
        raise HTTPException(status_code=503, detail="Notification service not available")

    try:
        payload = await client.history(channel=channel, status=status, since=since, limit=limit)
    except ServiceUnavailableError as exc:
        logger.info("Notification history unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail="Notification service not available") from exc
    except ServiceResponseError as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Notification service request failed"
        logger.error("Notification history request failed", status=exc.status_code, detail=detail)
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except ServiceClientError as exc:
        logger.error("Notification history client error", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unexpected notification history error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(payload, Mapping):
        return dict(payload)
    return {"success": True, "history": payload}


@router.delete("/api/notifications/history")
async def clear_notification_history(
    user: Optional[str] = Depends(get_current_user),
    client: NotificationServiceClient | None = Depends(_get_notification_service_client),
) -> Dict[str, Any]:
    if client is None:
        raise HTTPException(status_code=503, detail="Notification service not available")

    try:
        payload = await client.clear_history()
    except ServiceUnavailableError as exc:
        logger.info("Notification service unavailable for history clear", error=str(exc))
        raise HTTPException(status_code=503, detail="Notification service not available") from exc
    except ServiceResponseError as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Notification service request failed"
        logger.error("Notification history clear failed", status=exc.status_code, detail=detail)
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except ServiceClientError as exc:
        logger.error("Notification history clear client error", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unexpected notification history clear error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(payload, Mapping):
        return dict(payload)
    return {"success": True, "cleared": payload}


@router.post("/api/notifications/test")
async def test_notification(
    req: NotificationTestRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
    client: NotificationServiceClient | None = Depends(_get_notification_service_client),
) -> Dict[str, Any]:
    channel = (req.channel or "websocket").strip().lower()
    if channel == "all":
        channels = ["telegram", "websocket"]
    elif channel == "chrome":
        channels = ["websocket"]
    else:
        channels = [channel]

    payload = {
        "channel": channel,
        "channels": channels,
        "message": req.message,
        "event_type": "test",
        "category": "api",
    }
    result = await _dispatch_notification(client, payload, test_mode=True)
    return result
