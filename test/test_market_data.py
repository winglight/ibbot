#!/usr/bin/env python3
"""
市场数据相关测试
- /api/realtime_data/{symbol}
- /api/historical_data
- /api/chart_data

在无真实IBKR连接环境下，预期这些接口返回 400（未连接）。
若已连接，则接受 200（成功）或 404（无数据）作为通过。

运行方式：
  cd test
  python test_market_data.py
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


def test_realtime_data(token: str) -> bool:
    logger.info("测试: /api/realtime_data/{symbol}")
    connected = is_connected(token)
    if connected is None:
        return False
    resp = api_get(f"/api/realtime_data/{DEFAULT_SYMBOL}", token, timeout=15)
    if not connected:
        ok = (resp.status_code == 400)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        ok = (resp.status_code in (200, 404))
        if not ok:
            logger.error(f"已连接时应返回200或404, 实际{resp.status_code}, body={resp.text}")
        return ok


def test_historical_data(token: str) -> bool:
    logger.info("测试: /api/historical_data")
    connected = is_connected(token)
    if connected is None:
        return False
    payload = {
        "symbol": DEFAULT_SYMBOL,
        "duration": "1 D",
        "bar_size": "5 mins",
        "exchange": "SMART",
    }
    resp = api_post('/api/historical_data', token, json=payload, timeout=40)
    if not connected:
        ok = (resp.status_code == 400)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        ok = (resp.status_code in (200, 404))
        if not ok:
            logger.error(f"已连接时应返回200或404, 实际{resp.status_code}, body={resp.text}")
        return ok


def test_chart_data(token: str) -> bool:
    logger.info("测试: /api/chart_data")
    connected = is_connected(token)
    if connected is None:
        return False
    payload = {
        "symbol": DEFAULT_SYMBOL,
        "duration": "1 D",
        "bar_size": "5 mins",
        "exchange": "SMART",
    }
    resp = api_post('/api/chart_data', token, json=payload, timeout=40)
    if not connected:
        ok = (resp.status_code == 400)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        ok = (resp.status_code in (200, 404))
        if not ok:
            logger.error(f"已连接时应返回200或404, 实际{resp.status_code}, body={resp.text}")
        return ok


def main():
    logger.info("=== 市场数据相关测试开始 ===")
    token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        logger.error("无法获取令牌，终止测试")
        return

    tests = [
        ("实时数据", lambda: test_realtime_data(token)),
        ("历史数据", lambda: test_historical_data(token)),
        ("图表数据", lambda: test_chart_data(token)),
    ]

    all_passed = True
    for name, func in tests:
        try:
            result = func()
            if result:
                logger.info(f"✅ {name} - 通过")
            else:
                logger.error(f"❌ {name} - 失败")
                all_passed = False
        except Exception as e:
            logger.exception(f"❌ {name} - 异常: {e}")
            all_passed = False
        time.sleep(0.2)

    if all_passed:
        logger.info("✅ 市场数据相关测试完成（根据连接状态进行判断）")
    else:
        logger.error("❌ 存在失败的市场数据相关测试")


if __name__ == '__main__':
    main()