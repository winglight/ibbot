#!/usr/bin/env python3
"""
内部策略管理与执行相关测试
- 策略设置（鉴权开关、令牌生成）
- 策略CRUD（创建、查询、更新、删除、启停切换）
- 策略执行记录与性能指标
- /api/strategy 内部执行入口（同步/异步）

运行方式：
  cd test
  python -m pytest -q test_strategy.py
前提：
  主应用运行在 http://localhost:8000 （可通过环境变量 API_BASE_URL 修改）
  已有管理员账号（默认 admin/change_me，可通过 .env 配置）
说明：
  覆盖内部策略管理端到端基本流程，复用与外部策略相同的约定。
"""

import os
import time
from typing import Optional

import pytest
import requests
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')
TEST_STRATEGY_NAME = os.getenv('TEST_STRATEGY_NAME', 'strategy-internal-smoke')


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


def api_put(path: str, token: Optional[str], json: dict, timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.put(f"{BASE_URL}{path}", headers=headers, json=json, timeout=timeout)


def api_delete(path: str, token: Optional[str], timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.delete(f"{BASE_URL}{path}", headers=headers, timeout=timeout)


@pytest.fixture(scope="module")
def token():
    token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert token is not None, "无法获取认证令牌"
    return token


@pytest.fixture(autouse=True)
def cleanup_before_after(token):
    # 清理策略与执行记录，确保测试可重复
    try:
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}/executions", token)
    except Exception:
        pass
    try:
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}", token)
    except Exception:
        pass
    yield
    try:
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}/executions", token)
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}", token)
    except Exception:
        pass


def test_settings_toggle_and_token(token):
    # 开启鉴权并生成一次令牌
    r1 = api_post("/api/strategy/settings", token, {"enable_auth": True})
    assert r1.status_code in (200, 204), f"设置失败: {r1.status_code} {r1.text}"
    r2 = api_get("/api/strategy/settings", token)
    data2 = r2.json()
    assert r2.status_code == 200 and data2.get("settings", {}).get("enable_auth") is True
    r3 = api_post("/api/strategy/settings/generate-token", token, {})
    assert r3.status_code == 200 and r3.json().get("token")


def test_crud_and_toggle(token):
    # 创建
    strategy_data = {
        "name": TEST_STRATEGY_NAME,
        "strategy_type": "momentum",
        "enabled": True,
        "api_endpoint": "/api/strategy",
        "description": "内部策略端到端冒烟用例",
        "sl_enabled": True,
        "sl_price": 100.0,
        "sl_type": "amount",
        "tp_enabled": False
    }
    r_create = api_post("/api/strategies", token, strategy_data)
    assert r_create.status_code in (200, 201), f"创建失败: {r_create.status_code} {r_create.text}"

    # 查询
    r_get = api_get(f"/api/strategies/{TEST_STRATEGY_NAME}", token)
    data_get = r_get.json()
    assert r_get.status_code == 200 and data_get.get("strategy", {}).get("name") == TEST_STRATEGY_NAME

    # 更新（描述、enabled切换）
    r_update = api_put(f"/api/strategies/{TEST_STRATEGY_NAME}", token, {
        "description": "更新后的描述",
        "enabled": False
    })
    assert r_update.status_code in (200, 204)

    # 切换启用
    r_toggle = api_post(f"/api/strategies/{TEST_STRATEGY_NAME}/toggle?enabled=true", token, {})
    assert r_toggle.status_code in (200, 204)


def test_strategy_execution_and_records(token):
    # 确保存在一个已启用策略
    api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}", token)
    r_create = api_post("/api/strategies", token, {
        "name": TEST_STRATEGY_NAME,
        "strategy_type": "mean_reversion",
        "enabled": True,
        "api_endpoint": "/api/strategy",
    })
    assert r_create.status_code in (200, 201)

    # 为避免鉴权影响，这里确保关闭策略接口鉴权
    api_post("/api/strategy/settings", token, {"enable_auth": False})
    # 发起一次策略执行（内部接口）
    payload = {
        "strategy": TEST_STRATEGY_NAME,
        "symbol": "AAPL",
        "action": "BUY",
        "qty": 1,
        "type": "MARKET"
    }
    r_exec = api_post("/api/strategy", None, payload)
    assert r_exec.status_code in (200, 202), f"执行失败: {r_exec.status_code} {r_exec.text}"
 
    # 等待任务入库
    time.sleep(0.5)
 
    # 查询执行记录
    r_execs = api_get(f"/api/strategies/{TEST_STRATEGY_NAME}/executions", token)
    data_execs = r_execs.json()
    assert r_execs.status_code == 200 and isinstance(data_execs.get("executions", []), list)
    for exec_item in data_execs.get("executions", []):
        assert exec_item.get("order_source") == "strategy"
 
    # 查看性能统计
    r_perf = api_get(f"/api/strategies/{TEST_STRATEGY_NAME}/performance", token)
    assert r_perf.status_code == 200 and "metrics" in r_perf.json()


def test_delete_records_and_strategy(token):
    # 创建后删除执行记录与策略
    r_create = api_post("/api/strategies", token, {
        "name": TEST_STRATEGY_NAME,
        "strategy_type": "basic",
        "enabled": True,
        "api_endpoint": "/api/strategy",
    })
    assert r_create.status_code in (200, 201)

    r_del_exec = api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}/executions", token)
    assert r_del_exec.status_code in (200, 204)

    r_del = api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}", token)
    assert r_del.status_code in (200, 204, 404)  # 允许并发清理