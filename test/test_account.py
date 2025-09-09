#!/usr/bin/env python3
"""
账户管理相关测试
- /api/account_info
- /api/positions

在无真实IBKR连接环境下：
- /api/account_info 应返回 400 (Not connected to IBKR)
- /api/positions 应返回 200，返回空列表并在 warning 字段中提示未连接

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
import pytest

import requests
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')  # 可通过环境变量API_BASE_URL覆盖
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


@pytest.fixture
def token():
    t = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert t is not None, "无法获取令牌，终止测试"
    return t


def test_account_info(token: str):
    logger.info("测试: GET /api/account_info")
    connected = is_connected(token)
    assert connected is not None, "无法获取连接状态"

    resp = api_get('/api/account_info', token, timeout=20)

    if not connected:
        assert resp.status_code == 400 and 'Not connected' in resp.text, \
            f"未连接时应返回400, 实际{resp.status_code}, body={resp.text}"
    else:
        assert resp.status_code == 200, \
            f"已连接时期望200, 实际{resp.status_code}, body={resp.text}"

        # 已连接时，允许 success=True（含account_info）或 success=False（如超时）。
        try:
            data = resp.json()
        except Exception:
            raise AssertionError("响应非JSON格式")

        assert data.get('success') in (True, False), \
            f"响应结构不符合预期: {data}"

        if data.get('success') is True:
            assert 'account_info' in data, \
                f"缺少account_info字段: {data}"


def test_positions(token: str):
    logger.info("测试: GET /api/positions")
    connected = is_connected(token)
    assert connected is not None, "无法获取连接状态"

    resp = api_get('/api/positions', token, timeout=20)

    if not connected:
        assert resp.status_code == 200, \
            f"未连接时应返回200, 实际{resp.status_code}, body={resp.text}"

        try:
            data = resp.json()
        except Exception:
            raise AssertionError("响应非JSON格式")

        assert data.get('success') is True and data.get('positions') == [], \
            f"未连接时应返回空positions: {data}"
        assert 'warning' in data and 'Not connected' in data['warning'], \
            f"未连接时应包含 Not connected warning: {data}"
    else:
        assert resp.status_code == 200, \
            f"已连接时期望200, 实际{resp.status_code}, body={resp.text}"

        # 已连接时，允许 success=True（含positions）或 success=False（如超时）。
        try:
            data = resp.json()
        except Exception:
            raise AssertionError("响应非JSON格式")

        assert data.get('success') in (True, False), \
            f"响应结构不符合预期: {data}"

        if data.get('success') is True:
            assert 'positions' in data, \
                f"缺少positions字段: {data}"


def main():
    logger.info("=== 账户信息与持仓测试开始 ===")
    t = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not t:
        logger.error("无法获取令牌，终止测试")
        return

    tests = [
        ("账户信息", lambda: test_account_info(t)),
        ("持仓列表", lambda: test_positions(t)),
    ]

    all_passed = True
    for name, func in tests:
        try:
            func()
            logger.info(f"✅ {name} - 通过")
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