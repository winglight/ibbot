#!/usr/bin/env python3
"""
交易功能相关测试
- /api/place_order（市价/限价）
- /api/cancel_order (DELETE & POST)
- /api/cancel_all_orders
- /api/orders

在无真实IBKR连接环境下：
- 下单/取消/批量取消接口应返回 400 (Not connected to IBKR)
- /api/orders 应返回 200 且包含订单列表（可能为空）

若已连接：
- 下单接口应返回 200（或服务端定义的成功结构）
- 取消接口应返回 200（成功取消）或 404（订单不存在/已完成）
- 批量取消应返回 200

运行方式：
  cd test
  python test_trading.py
前提：
  主应用运行在 http://localhost:8000 （可通过环境变量 API_BASE_URL 修改）
  已有管理员账号（默认 admin/change_me，可通过 .env 配置）
"""

import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')
DEFAULT_SYMBOL = os.getenv('TEST_SYMBOL', 'AAPL')


def login(username: str, password: str) -> Optional[str]:
    try:
        resp = requests.post(
            f"{BASE_URL}/api/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error(f"登录失败: {resp.status_code} {resp.text}")
            return None
        return resp.json().get('access_token')
    except Exception as e:
        logger.error(f"登录请求异常: {e}")
        return None


def api_get(path: str, token: Optional[str], timeout: int = 12):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{BASE_URL}{path}", headers=headers, timeout=timeout)


def api_post(path: str, token: Optional[str], json: dict, timeout: int = 30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=timeout)


def api_delete(path: str, token: Optional[str], timeout: int = 20):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.delete(f"{BASE_URL}{path}", headers=headers, timeout=timeout)


def is_connected(token: str) -> Optional[bool]:
    try:
        resp = api_get('/api/connection_status', token)
        if resp.status_code != 200:
            logger.error(f"查询连接状态失败: {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        return bool(data.get('connected'))
    except Exception as e:
        logger.error(f"查询连接状态异常: {e}")
        return None


def test_place_order_market(token: str) -> bool:
    logger.info("测试: /api/place_order 市价单")
    connected = is_connected(token)
    if connected is None:
        return False

    payload = {
        "symbol": DEFAULT_SYMBOL,
        "quantity": 1,
        "action": "BUY",
        "order_type": "MARKET",
        "exchange": "SMART",
        "sec_type": "STK",
    }
    resp = api_post('/api/place_order', token, json=payload, timeout=40)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = {}
            ok = (data.get('success') is True and 'order_id' in data)
            if not ok:
                logger.error(f"下单响应结构不符合预期: {data}")
            return ok
        else:
            logger.error(f"已连接时期望200, 实际{resp.status_code}, body={resp.text}")
            return False


def test_place_order_limit(token: str) -> Optional[int]:
    """尝试提交一个远离市价的限价单，返回订单ID（若成功）
    未连接时断言返回400；连接时返回订单ID用于后续取消测试。
    """
    logger.info("测试: /api/place_order 限价单")
    connected = is_connected(token)
    if connected is None:
        return None

    payload = {
        "symbol": DEFAULT_SYMBOL,
        "quantity": 1,
        "price": 1.0,  # 远离市价，避免成交
        "action": "BUY",
        "order_type": "LIMIT",
        "exchange": "SMART",
        "sec_type": "STK",
    }
    resp = api_post('/api/place_order', token, json=payload, timeout=40)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return None
    else:
        if resp.status_code == 200:
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            if data.get('success') and data.get('order_id'):
                order_id = data.get('order_id')
                logger.info(f"限价单已提交，order_id={order_id}")
                return int(order_id)
            logger.error(f"下单响应结构不符合预期: {data}")
            return None
        else:
            logger.error(f"已连接时期望200, 实际{resp.status_code}, body={resp.text}")
            return None


def test_cancel_order_delete(token: str, order_id: Optional[int]) -> bool:
    logger.info("测试: DELETE /api/cancel_order/{order_id}")
    connected = is_connected(token)
    if connected is None:
        return False

    target_id = order_id if (connected and order_id is not None) else 999999999
    resp = api_delete(f"/api/cancel_order/{target_id}", token, timeout=30)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        ok = (resp.status_code in (200, 404))
        if not ok:
            logger.error(f"已连接时应返回200或404, 实际{resp.status_code}, body={resp.text}")
        return ok


def test_cancel_order_post(token: str, order_id: Optional[int]) -> bool:
    logger.info("测试: POST /api/cancel_order")
    connected = is_connected(token)
    if connected is None:
        return False

    payload = {"order_id": order_id if (connected and order_id is not None) else 999999999}
    resp = api_post('/api/cancel_order', token, json=payload, timeout=30)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        ok = (resp.status_code in (200, 404)) or (resp.status_code == 200 and resp.json().get('success') in (True, False))
        if not ok:
            logger.error(f"已连接时应返回200或404, 实际{resp.status_code}, body={resp.text}")
        return ok


def test_cancel_all_orders(token: str) -> bool:
    logger.info("测试: DELETE /api/cancel_all_orders")
    connected = is_connected(token)
    if connected is None:
        return False

    resp = api_delete('/api/cancel_all_orders', token, timeout=60)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        ok = (resp.status_code == 200)
        if not ok:
            logger.error(f"已连接时应返回200, 实际{resp.status_code}, body={resp.text}")
        return ok


def test_get_orders(token: str) -> bool:
    logger.info("测试: GET /api/orders")
    resp = api_get('/api/orders', token, timeout=25)
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    try:
        data = resp.json()
    except Exception:
        logger.error("响应非JSON格式")
        return False
    ok = (data.get('success') is True and isinstance(data.get('orders'), list))
    if not ok:
        logger.error(f"响应结构不符合预期: {data}")
    return ok


def main():
    logger.info("=== 交易功能相关测试开始 ===")
    token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        logger.error("无法获取令牌，终止测试")
        return

    # 逐项测试
    results = []
    results.append(("市价单下单", test_place_order_market(token)))

    limit_order_id: Optional[int] = None
    try:
        limit_order_id = test_place_order_limit(token)
        results.append(("限价单下单", limit_order_id is not None or is_connected(token) is False))
    except Exception as e:
        logger.exception(f"限价单下单异常: {e}")
        results.append(("限价单下单", False))

    results.append(("DELETE取消订单", test_cancel_order_delete(token, limit_order_id)))
    results.append(("POST取消订单", test_cancel_order_post(token, limit_order_id)))
    results.append(("批量取消订单", test_cancel_all_orders(token)))
    results.append(("查询订单列表", test_get_orders(token)))

    # 汇总
    all_passed = True
    for name, ok in results:
        if ok:
            logger.info(f"✅ {name} - 通过")
        else:
            logger.error(f"❌ {name} - 失败")
            all_passed = False
        time.sleep(0.2)

    if all_passed:
        logger.info("✅ 交易功能相关测试完成（根据连接状态进行判断）")
    else:
        logger.error("❌ 存在失败的交易功能相关测试")


if __name__ == '__main__':
    main()