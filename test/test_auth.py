#!/usr/bin/env python3
"""
认证与JWT令牌相关测试
- 登录成功/失败
- 受保护接口在无/无效/过期令牌下返回401
- 使用有效令牌访问受保护接口成功

运行方式：
  cd test
  python test_auth.py
前提：
  主应用运行在 http://localhost:8000
  .env 中配置 ADMIN_USERNAME/ADMIN_PASSWORD（若不配置则使用默认 admin/change_me）
"""

import os
import time
from datetime import datetime, timedelta

try:
    from . import http_client as requests
except ImportError:
    import http_client as requests
from dotenv import load_dotenv
from loguru import logger
from jose import jwt

# 加载.env配置
load_dotenv()

BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')
WS_URL = os.getenv('WS_BASE_URL', 'ws://localhost:8000/ws')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')
JWT_SECRET = os.getenv('JWT_SECRET', 'please_change_me')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')


def login(username: str, password: str):
    try:
        resp = requests.post(
            f"{BASE_URL}/api/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        return resp
    except Exception as e:
        logger.error(f"登录请求异常: {e}")
        return None


def get_with_auth(endpoint: str, token: str | None, timeout: int = 10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{BASE_URL}{endpoint}", headers=headers, timeout=timeout)


def test_login_success():
    logger.info("测试: 登录成功应返回access_token")
    resp = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert resp is not None, "登录请求失败"
    assert resp.status_code == 200, f"期望200, 实际{resp.status_code}, body={resp.text}"
    data = resp.json()
    token = data.get('access_token')
    assert token, "登录响应未包含access_token"


def test_login_failure():
    logger.info("测试: 登录失败应返回401")
    resp = login(ADMIN_USERNAME, "wrong_password")
    assert resp is not None, "登录请求失败"
    assert resp.status_code == 401, f"期望401, 实际{resp.status_code}, body={resp.text}"


def test_protected_without_token():
    logger.info("测试: 受保护接口无令牌应返回401")
    resp = get_with_auth("/api/connection_status", None)
    assert resp.status_code == 401, f"期望401, 实际{resp.status_code}, body={resp.text}"


def test_protected_with_invalid_token():
    logger.info("测试: 受保护接口使用无效令牌应返回401")
    resp = get_with_auth("/api/connection_status", "invalid.token.value")
    assert resp.status_code == 401, f"期望401, 实际{resp.status_code}, body={resp.text}"


def test_protected_with_expired_token():
    logger.info("测试: 受保护接口使用过期令牌应返回401")
    expired_payload = {
        "sub": ADMIN_USERNAME,
        "exp": datetime.utcnow() - timedelta(seconds=1)
    }
    token = jwt.encode(expired_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    resp = get_with_auth("/api/connection_status", token)
    assert resp.status_code == 401, f"期望401, 实际{resp.status_code}, body={resp.text}"


def test_protected_with_valid_token():
    logger.info("测试: 使用有效令牌访问受保护接口应成功")
    # 先登录获取token
    resp = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert resp is not None and resp.status_code == 200, f"登录失败，无法继续: {None if not resp else resp.text}"
    token = resp.json().get('access_token')
    assert token, "登录响应未包含access_token"

    # 使用token访问受保护接口
    resp2 = get_with_auth("/api/connection_status", token)
    assert resp2.status_code == 200, f"期望200, 实际{resp2.status_code}, body={resp2.text}"
    data = resp2.json()
    assert 'connected' in data and 'account_type' in data, f"响应缺少必要字段: {data}"


def main():
    logger.info("=== 认证与JWT测试开始 ===")
    tests = [
        ("登录成功", test_login_success),
        ("登录失败", test_login_failure),
        ("无令牌访问受保护接口", test_protected_without_token),
        ("无效令牌访问受保护接口", test_protected_with_invalid_token),
        ("过期令牌访问受保护接口", test_protected_with_expired_token),
        ("有效令牌访问受保护接口", test_protected_with_valid_token),
    ]

    all_passed = True
    for name, func in tests:
        try:
            func()
            logger.info(f"✅ {name} - 通过")
        except Exception as e:
            logger.exception(f"❌ {name} - 异常: {e}")
            all_passed = False
        # 小间隔，避免日志拥挤
        time.sleep(0.2)

    if all_passed:
        logger.info("✅ 所有认证相关测试通过")
    else:
        logger.error("❌ 存在失败的认证相关测试")


if __name__ == "__main__":
    main()
