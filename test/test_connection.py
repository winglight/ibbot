#!/usr/bin/env python3
"""
连接管理相关测试
- 连接状态查询
- 连接失败路径（不可达端口）
- 断开连接
- 验证 /api/connect 传入的 paper_trading 会更新配置从而影响 /api/connection_status 的 account_type

运行方式：
  cd test
  python test_connection.py
前提：
  主应用运行在 http://localhost:8000 （可通过环境变量 API_BASE_URL 修改）
  已有管理员账号（默认 admin/change_me，可通过 .env 配置）
说明：
  为避免依赖真实IBKR环境，本测试使用不可达端口进行连接，断言返回400，并校验account_type根据提交的paper_trading切换。
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


def api_get(path: str, token: Optional[str], timeout: int = 10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{BASE_URL}{path}", headers=headers, timeout=timeout)


def api_post(path: str, token: Optional[str], json: dict, timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=timeout)


def test_connection_status(token: str) -> bool:
    logger.info("测试: /api/connection_status 应返回连接状态和账户类型")
    resp = api_get('/api/connection_status', token)
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    data = resp.json()
    ok = ("connected" in data and "account_type" in data and data["account_type"] in ("Paper", "Live"))
    if not ok:
        logger.error(f"响应缺少字段或值不在预期范围: {data}")
    return ok


def test_connect_fail_and_update_account_type(token: str) -> bool:
    logger.info("测试: /api/connect 使用不可达端口，期望返回400，并更新account_type为Live")
    # 使用明显不可达端口，触发连接失败
    payload = {
        "host": "127.0.0.1",
        "port": 1,  # 通常不可用端口
        "client_id": 1,
        "paper_trading": False,  # 期望切换为 Live
    }
    resp = api_post('/api/connect', token, json=payload, timeout=25)
    if resp.status_code != 400:
        logger.error(f"期望400, 实际{resp.status_code}, body={resp.text}")
        return False

    # 连接失败后验证配置已更新，account_type 应为 Live
    resp2 = api_get('/api/connection_status', token)
    if resp2.status_code != 200:
        logger.error(f"期望200, 实际{resp2.status_code}, body={resp2.text}")
        return False
    data = resp2.json()
    ok = (data.get('account_type') == 'Live')
    if not ok:
        logger.error(f"期望account_type=Live, 实际: {data}")
    return ok


def test_disconnect_ok(token: str) -> bool:
    logger.info("测试: /api/disconnect 应返回成功，即使当前未连接")
    resp = api_post('/api/disconnect', token, json={})
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    data = resp.json()
    ok = (data.get('success') is True)
    if not ok:
        logger.error(f"期望success=True, 实际: {data}")
    return ok


def main():
    logger.info("=== 连接管理相关测试开始 ===")
    token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        logger.error("无法获取令牌，终止测试")
        return

    tests = [
        ("连接状态查询", lambda: test_connection_status(token)),
        ("连接失败并更新为Live", lambda: test_connect_fail_and_update_account_type(token)),
        ("断开连接", lambda: test_disconnect_ok(token)),
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
        logger.info("✅ 连接管理相关测试全部通过（在无真实IBKR连接环境下的预期路径）")
    else:
        logger.error("❌ 存在失败的连接管理测试")


if __name__ == '__main__':
    main()