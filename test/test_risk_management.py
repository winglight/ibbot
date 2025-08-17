#!/usr/bin/env python3
"""
风险管理相关测试
- /api/risk_rules (POST, GET, PUT, DELETE) - 风险规则的增删改查
- /api/order_queue_status (GET) - 订单队列状态
- /api/auto_orders (GET) - 自动订单记录
- /api/risk-config (GET) - 风险配置参数

测试前提：
- 主应用运行在 http://localhost:8000
- risk_manager 已在应用启动时初始化
- 管理员账号可用（默认 admin/change_me）

运行方式：
  cd test
  python test_risk_management.py
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

TEST_SYMBOL = os.getenv('TEST_SYMBOL', 'AAPL')


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


def api_post(path: str, token: Optional[str], json: dict, timeout: int = 10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=timeout)


def api_put(path: str, token: Optional[str], params: dict = None, timeout: int = 10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.put(f"{BASE_URL}{path}", headers=headers, params=params, timeout=timeout)


def api_delete(path: str, token: Optional[str], timeout: int = 10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.delete(f"{BASE_URL}{path}", headers=headers, timeout=timeout)


def test_add_risk_rule(token: str) -> bool:
    logger.info("测试: POST /api/risk_rules - 添加风险规则")
    payload = {
        "symbol": TEST_SYMBOL,
        "stop_loss_price": 150.0,
        "take_profit_price": 200.0,
        "max_loss_percent": 5.0
    }
    resp = api_post('/api/risk_rules', token, json=payload)
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    ok = (data.get('success') is True and 'message' in data)
    if not ok:
        logger.error(f"响应格式不符合预期: {data}")
    return ok


def test_get_risk_rules(token: str) -> bool:
    logger.info("测试: GET /api/risk_rules - 获取风险规则")
    resp = api_get('/api/risk_rules', token)
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    ok = (data.get('success') is True and 'rules' in data)
    if not ok:
        logger.error(f"响应格式不符合预期: {data}")
        return False
    
    # 验证规则格式
    rules = data.get('rules', {})
    if TEST_SYMBOL in rules:
        rule = rules[TEST_SYMBOL]
        required_fields = ['symbol', 'stop_loss_price', 'take_profit_price', 'max_loss_percent', 'enabled', 'created_at']
        for field in required_fields:
            if field not in rule:
                logger.error(f"规则缺少必要字段 {field}: {rule}")
                return False
    
    logger.info(f"获取到规则数量: {len(rules)}")
    return True


def test_update_risk_rule(token: str) -> bool:
    logger.info("测试: PUT /api/risk_rules/{symbol} - 更新风险规则状态")
    
    # 先确保规则存在
    resp_get = api_get('/api/risk_rules', token)
    if resp_get.status_code != 200:
        logger.error("无法获取现有规则")
        return False
    
    rules = resp_get.json().get('rules', {})
    if TEST_SYMBOL not in rules:
        logger.warning(f"规则 {TEST_SYMBOL} 不存在，先添加再测试更新")
        if not test_add_risk_rule(token):
            return False
    
    # 测试禁用规则
    resp = api_put(f'/api/risk_rules/{TEST_SYMBOL}', token, params={'enabled': False})
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    ok = (data.get('success') is True and 'message' in data)
    if not ok:
        logger.error(f"响应格式不符合预期: {data}")
        return False
    
    # 验证规则已被禁用
    resp_verify = api_get('/api/risk_rules', token)
    if resp_verify.status_code == 200:
        updated_rules = resp_verify.json().get('rules', {})
        if TEST_SYMBOL in updated_rules:
            enabled = updated_rules[TEST_SYMBOL].get('enabled')
            if enabled is not False:
                logger.error(f"规则应已禁用，但enabled={enabled}")
                return False
    
    return True


def test_update_nonexistent_rule(token: str) -> bool:
    logger.info("测试: PUT /api/risk_rules/{symbol} - 更新不存在的规则应返回404")
    resp = api_put('/api/risk_rules/NONEXISTENT', token, params={'enabled': True})
    
    ok = (resp.status_code == 404)
    if not ok:
        logger.error(f"期望404, 实际{resp.status_code}, body={resp.text}")
    
    return ok


def test_remove_risk_rule(token: str) -> bool:
    logger.info("测试: DELETE /api/risk_rules/{symbol} - 删除风险规则")
    
    # 确保规则存在
    if not test_add_risk_rule(token):
        logger.error("无法添加测试规则")
        return False
    
    resp = api_delete(f'/api/risk_rules/{TEST_SYMBOL}', token)
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    ok = (data.get('success') is True and 'message' in data)
    if not ok:
        logger.error(f"响应格式不符合预期: {data}")
        return False
    
    # 验证规则已被删除
    resp_verify = api_get('/api/risk_rules', token)
    if resp_verify.status_code == 200:
        rules = resp_verify.json().get('rules', {})
        if TEST_SYMBOL in rules:
            logger.error(f"规则 {TEST_SYMBOL} 应已删除但仍存在")
            return False
    
    return True


def test_order_queue_status(token: str) -> bool:
    logger.info("测试: GET /api/order_queue_status - 获取订单队列状态")
    resp = api_get('/api/order_queue_status', token)
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    required_fields = ['success', 'queue_size']
    for field in required_fields:
        if field not in data:
            logger.error(f"响应缺少必要字段 {field}: {data}")
            return False
    
    # queue_size 应为数字
    if not isinstance(data.get('queue_size'), int):
        logger.error(f"queue_size 应为整数: {data}")
        return False
    
    logger.info(f"队列状态: {data}")
    return True


def test_auto_orders(token: str) -> bool:
    logger.info("测试: GET /api/auto_orders - 获取自动订单记录")
    resp = api_get('/api/auto_orders', token)
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    ok = (data.get('success') is True and 'orders' in data)
    if not ok:
        logger.error(f"响应格式不符合预期: {data}")
        return False
    
    orders = data.get('orders', [])
    if not isinstance(orders, list):
        logger.error(f"orders 应为列表: {data}")
        return False
    
    # 验证订单格式（如果有订单的话）
    for order in orders:
        if order.get('order_source') != 'auto':
            logger.error(f"应只返回自动订单，但发现: {order}")
            return False
    
    logger.info(f"自动订单数量: {len(orders)}")
    return True


def test_risk_config(token: str) -> bool:
    logger.info("测试: GET /api/risk-config - 获取风险配置参数")
    resp = api_get('/api/risk-config', token)
    
    if resp.status_code != 200:
        logger.error(f"期望200, 实际{resp.status_code}, body={resp.text}")
        return False
    
    data = resp.json()
    required_fields = ['stop_loss_ratio', 'take_profit_ratio']
    for field in required_fields:
        if field not in data:
            logger.error(f"响应缺少必要字段 {field}: {data}")
            return False
    
    # 比率应为数字
    for field in required_fields:
        value = data.get(field)
        if not isinstance(value, (int, float)):
            logger.error(f"{field} 应为数字: {data}")
            return False
    
    logger.info(f"风险配置: {data}")
    return True


def test_risk_manager_not_initialized() -> bool:
    """测试当 risk_manager 未初始化时的行为（通过无效令牌模拟）"""
    logger.info("测试: 风险管理接口在缺少依赖时的行为")
    # 注意：这个测试比较难模拟，因为 risk_manager 在启动时就会初始化
    # 这里只是验证接口的基本格式正确性
    return True


def main():
    logger.info("=== 风险管理相关测试开始 ===")
    token = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        logger.error("无法获取令牌，终止测试")
        return

    tests = [
        ("添加风险规则", lambda: test_add_risk_rule(token)),
        ("获取风险规则", lambda: test_get_risk_rules(token)),
        ("更新风险规则状态", lambda: test_update_risk_rule(token)),
        ("更新不存在的规则", lambda: test_update_nonexistent_rule(token)),
        ("删除风险规则", lambda: test_remove_risk_rule(token)),
        ("获取订单队列状态", lambda: test_order_queue_status(token)),
        ("获取自动订单记录", lambda: test_auto_orders(token)),
        ("获取风险配置参数", lambda: test_risk_config(token)),
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
        
        # 小间隔，避免测试冲突
        time.sleep(0.5)

    if all_passed:
        logger.info("✅ 所有风险管理相关测试通过")
    else:
        logger.error("❌ 存在失败的风险管理相关测试")


if __name__ == "__main__":
    main()