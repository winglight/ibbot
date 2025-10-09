"""Interactive Brokers integration metadata endpoints for the main API.

These endpoints describe how the refactored account, orders, and market data
services expose their Redis channels and REST APIs so that downstream
consumers can discover the available integration points without inspecting the
individual microservice repositories.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from fastapi import APIRouter

from src.ib.events import RedisChannel


router = APIRouter()


def _channel_names(*channels: RedisChannel) -> list[str]:
    return [channel.value for channel in channels]


CHANNEL_METADATA: Dict[str, Mapping[str, Any]] = {
    "account": {
        "description": "Account service Redis channels and subscription endpoints",
        "redis_channels": _channel_names(
            RedisChannel.ACCOUNT_UPDATES,
            RedisChannel.PORTFOLIO,
        ),
        "rest_endpoints": {
            "subscribe": {
                "method": "POST",
                "path": "/subscriptions/account",
                "summary": "Subscribe to account updates and portfolio events",
            },
            "unsubscribe": {
                "method": "DELETE",
                "path": "/subscriptions/account",
                "summary": "Cancel an account subscription",
            },
            "summary": {
                "method": "GET",
                "path": "/accounts/{account_id}/summary",
                "summary": "Fetch the latest account summary snapshot",
            },
            "positions": {
                "method": "GET",
                "path": "/accounts/{account_id}/positions",
                "summary": "Fetch the latest open positions snapshot",
            },
        },
    },
    "orders": {
        "description": "Orders service Redis channels and REST orchestration",
        "redis_channels": _channel_names(
            RedisChannel.ORDERS,
            RedisChannel.EXECUTIONS,
        ),
        "rest_endpoints": {
            "subscribe": {
                "method": "POST",
                "path": "/subscriptions/orders",
                "summary": "Subscribe to live order status updates",
            },
            "unsubscribe": {
                "method": "DELETE",
                "path": "/subscriptions/orders",
                "summary": "Cancel an orders subscription",
            },
            "place_order": {
                "method": "POST",
                "path": "/orders",
                "summary": "Submit a new order",
            },
            "cancel_order": {
                "method": "DELETE",
                "path": "/orders/{order_id}",
                "summary": "Cancel an existing order",
            },
            "open_orders": {
                "method": "GET",
                "path": "/orders/open",
                "summary": "Fetch the currently open orders",
            },
            "completed_orders": {
                "method": "GET",
                "path": "/orders/completed",
                "summary": "Fetch completed order history",
            },
            "executions": {
                "method": "GET",
                "path": "/orders/executions",
                "summary": "Fetch execution reports for historical orders",
            },
        },
    },
    "market_data": {
        "description": "Market data service Redis channels and subscription endpoints",
        "redis_channels": _channel_names(
            RedisChannel.MARKET_TICKS,
            RedisChannel.MARKET_DOM,
            RedisChannel.MARKET_TICK_BY_TICK,
            RedisChannel.MARKET_REAL_TIME_BARS,
        ),
        "rest_endpoints": {
            "quotes": {
                "subscribe": {
                    "method": "POST",
                    "path": "/subscriptions/quotes",
                    "summary": "Subscribe to market data quotes",
                },
                "unsubscribe": {
                    "method": "DELETE",
                    "path": "/subscriptions/quotes/{subscription_id}",
                    "summary": "Cancel a quote subscription",
                },
            },
            "dom": {
                "subscribe": {
                    "method": "POST",
                    "path": "/subscriptions/dom",
                    "summary": "Subscribe to market depth updates",
                },
                "unsubscribe": {
                    "method": "DELETE",
                    "path": "/subscriptions/dom/{subscription_id}",
                    "summary": "Cancel a DOM subscription",
                },
            },
            "tick_by_tick": {
                "subscribe": {
                    "method": "POST",
                    "path": "/subscriptions/tick-by-tick",
                    "summary": "Subscribe to tick-by-tick data",
                },
                "unsubscribe": {
                    "method": "DELETE",
                    "path": "/subscriptions/tick-by-tick/{subscription_id}",
                    "summary": "Cancel a tick-by-tick subscription",
                },
            },
            "real_time_bars": {
                "subscribe": {
                    "method": "POST",
                    "path": "/subscriptions/real-time-bars",
                    "summary": "Subscribe to real-time bar updates",
                },
                "unsubscribe": {
                    "method": "DELETE",
                    "path": "/subscriptions/real-time-bars/{subscription_id}",
                    "summary": "Cancel a real-time bar subscription",
                },
            },
            "historical": {
                "method": "GET",
                "path": "/historical",
                "summary": "Fetch historical market data bars",
            },
        },
    },
}


@router.get("/ib/channels")
async def list_ib_channels() -> Dict[str, Any]:
    """Return metadata describing Redis channels and REST endpoints per service."""

    return {"services": CHANNEL_METADATA, "version": 1}

