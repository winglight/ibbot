#!/usr/bin/env python3
"""
账户管理相关测试
- /api/account_info
- /api/positions

在无真实IBKR连接环境下：
- 两个接口均应返回 400 (Not connected to IBKR)

若已连接：
- 两个接口应返回 200；可能 success=True 并返回数据，或因超时返回 success=False 且带 error 信息（均视为通过）

运行方式：
  cd test
  python test_account.py
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


def api_get(path: str, token: Optional[str], timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{BASE_URL}{path}", headers=headers, timeout=timeout)


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


def test_account_info(token: str) -> bool:
    logger.info("测试: GET /api/account_info")
    connected = is_connected(token)
    if connected is None:
        return False

    resp = api_get('/api/account_info', token, timeout=20)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        if resp.status_code != 200:
            logger.error(f"已连接时期望200, 实际{resp.status_code}, body={resp.text}")
            return False
        # 已连接时，允许 success=True（含account_info）或 success=False（如超时）。
        try:
            data = resp.json()
        except Exception:
            logger.error("响应非JSON格式")
            return False
        ok = (data.get('success') in (True, False))
        if data.get('success') is True and 'account_info' not in data:
            logger.error(f"缺少account_info字段: {data}")
            ok = False
        if not ok:
            logger.error(f"响应结构不符合预期: {data}")
        return ok


def test_positions(token: str) -> bool:
    logger.info("测试: GET /api/positions")
    connected = is_connected(token)
    if connected is None:
        return False

    resp = api_get('/api/positions', token, timeout=20)

    if not connected:
        ok = (resp.status_code == 400 and 'Not connected' in resp.text)
        if not ok:
            logger.error(f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}")
        return ok
    else:
        if resp.status_code != 200:
            logger.error(f"已连接时期望200, 实际{resp.status_code}, body={resp.text}")
            return False
        # 已连接时，允许 success=True（含positions）或 success=False（如超时）。
        try:
            data = resp.json()
        except Exception:
            logger.error("响应非JSON格式")
            return False
        ok = (data.get('success') in (True, False))
        if data.get('success') is True and 'positions' not in data:
            logger.error(f"缺少positions字段: {data}")
            ok = False
        if not ok:
            logger.error(f"响应结构不符合预期: {data}")
        return ok


def main():
    logger.info("=== 账户信息与持仓测试开始 ===")
    token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        logger.error("无法获取令牌，终止测试")
        return

    tests = [
        ("账户信息", lambda: test_account_info(token)),
        ("持仓列表", lambda: test_positions(token)),
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
        logger.info("✅ 账户相关测试完成（根据连接状态进行判断）")
    else:
        logger.error("❌ 存在失败的账户相关测试")


if __name__ == '__main__':
    main()