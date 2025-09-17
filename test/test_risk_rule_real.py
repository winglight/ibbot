#!/usr/bin/env python3
"""
使用真实IBKR Paper账户测试修复后的风险规则激活功能
"""

import time
from typing import Any, Dict, List

import pytest
import requests

# API配置
BASE_URL = "http://127.0.0.1:8000"
USERNAME = "chy"
PASSWORD = "X9$K^nNI4"


def _request_json(method: str, path: str, *, token: str | None = None, payload: Dict[str, Any] | None = None) -> Any:
    """发送请求并返回JSON响应."""

    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = requests.request(method, f"{BASE_URL}{path}", headers=headers, json=payload, timeout=10)
    except requests.RequestException as exc:
        pytest.skip(f"无法连接到API服务器: {exc}")

    if response.status_code == 401:
        pytest.skip(f"请求{path}需要有效凭据: {response.text}")
    if response.status_code != 200:
        pytest.fail(f"请求{path}失败: {response.status_code} - {response.text}")

    try:
        return response.json()
    except ValueError as exc:
        pytest.fail(f"解析{path}的JSON响应失败: {exc}")


def login() -> str:
    """登录并获取token"""

    data = {"username": USERNAME, "password": PASSWORD}
    response = _request_json("POST", "/api/login", payload=data)
    token = response.get("access_token")
    if not token:
        pytest.fail("登录响应缺少access_token")
    print("✅ 登录成功")
    return token


def get_connection_status(token: str) -> Dict[str, Any]:
    return _request_json("GET", "/api/connection_status", token=token)


def get_positions(token: str) -> List[Dict[str, Any]]:
    positions = _request_json("GET", "/api/positions", token=token)
    print(f"📊 当前持仓数量: {len(positions)}")
    return positions


def get_orders(token: str) -> List[Dict[str, Any]]:
    orders = _request_json("GET", "/api/orders", token=token)
    print(f"📋 订单数量: {len(orders)}")
    return orders


def add_risk_rule(token: str, symbol: str, stop_loss_price: float, take_profit_price: float | None = None) -> None:
    data: Dict[str, Any] = {"symbol": symbol, "stop_loss_price": stop_loss_price}
    if take_profit_price is not None:
        data["take_profit_price"] = take_profit_price
    _request_json("POST", "/api/risk_rules", token=token, payload=data)
    print(f"✅ 成功添加风险规则: {symbol} 止损价=${stop_loss_price}")


def get_risk_rules(token: str) -> List[Dict[str, Any]]:
    rules = _request_json("GET", "/api/risk_rules", token=token)
    print(f"📋 当前风险规则数量: {len(rules)}")
    for rule in rules:
        status = "🟢 激活" if rule['enabled'] else "🔴 禁用"
        print(f"   {rule['symbol']}: {status}, 止损=${rule.get('stop_loss_price', 'N/A')}, 止盈=${rule.get('take_profit_price', 'N/A')}")
    return rules


def update_risk_rule(token: str, symbol: str, enabled: bool = True) -> None:
    _request_json("PUT", f"/api/risk_rules/{symbol}", token=token, payload={"enabled": enabled})
    status = "激活" if enabled else "禁用"
    print(f"✅ 成功{status}风险规则: {symbol}")


def delete_risk_rule(token: str, symbol: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.delete(f"{BASE_URL}/api/risk_rules/{symbol}", headers=headers, timeout=10)
    except requests.RequestException as exc:
        pytest.skip(f"无法连接到API服务器: {exc}")

    if response.status_code not in (200, 404):
        pytest.fail(f"删除风险规则失败: {response.status_code} - {response.text}")
    if response.status_code == 200:
        print(f"✅ 成功删除风险规则: {symbol}")


def test_risk_rule_activation_with_real_positions():
    """使用真实持仓测试风险规则激活功能"""

    print("\n" + "=" * 60)
    print("🧪 使用真实IBKR Paper账户测试风险规则激活功能")
    print("=" * 60)

    # 1. 登录
    token = login()

    # 2. 检查连接状态
    print("\n📡 检查IBKR接状态...")
    status = get_connection_status(token)
    if not status.get('connected'):
        pytest.skip("IBKR未连接，请确保已连接到Paper账户")

    print(f"✅ 已连接到IBKR: {status.get('account_type', 'Unknown')}")

    # 3. 获取当前持仓
    print("\n📊 检查当前持仓...")
    positions = get_positions(token)

    if not positions:
        pytest.skip("当前没有持仓，无法测试风险规则")

    # 4. 选择第一个有持仓的股票进行测试
    test_position = next((pos for pos in positions if pos['position'] != 0), None)
    if not test_position:
        pytest.skip("没有找到非零持仓")

    test_symbol = test_position['symbol']
    current_price = test_position['marketPrice']
    position_size = test_position['position']

    print(f"\n🎯 选择测试股票: {test_symbol}")
    print(f"   持仓数量: {position_size}")
    print(f"   当前价格: ${current_price:.2f}")

    # 5. 设置风险规则（设置一个不会立即触发的价格）
    if position_size > 0:  # 多头持仓
        stop_loss_price = current_price * 0.8  # 比当前价格低20%
        take_profit_price = current_price * 1.2  # 比当前价格高20%
    else:  # 空头持仓
        stop_loss_price = current_price * 1.2  # 比当前价格高20%
        take_profit_price = current_price * 0.8  # 比当前价格低20%

    print(f"🛑 设置止损价格: ${stop_loss_price:.2f}")
    print(f"🎯 设置止盈价格: ${take_profit_price:.2f}")

    # 6. 清理可能存在的旧规则
    print(f"\n🧹 清理{test_symbol}的旧风险规则...")
    delete_risk_rule(token, test_symbol)
    time.sleep(1)

    # 7. 添加新的风险规则
    print("\n📋 添加新的风险规则...")
    add_risk_rule(token, test_symbol, stop_loss_price, take_profit_price)

    # 8. 检查规则状态
    print("\n🔍 检查风险规则状态...")
    rules = get_risk_rules(token)
    target_rule = next((rule for rule in rules if rule['symbol'] == test_symbol), None)

    if not target_rule:
        pytest.fail(f"未找到{test_symbol}的风险规则")

    initial_status = target_rule['enabled']
    print(f"📋 初始规则状态: {'🟢 激活' if initial_status else '🔴 禁用'}")

    # 9. 如果规则被禁用，激活它
    if not initial_status:
        print("\n⚡ 激活风险规则...")
        update_risk_rule(token, test_symbol, True)
        time.sleep(1)

    # 10. 等待风险管理器运行几个周期
    print("\n⏳ 等待风险管理器运行 (30秒)...")
    print("   在此期间，风险管理器会监控价格变化")
    print("   由于设置的止损/止盈价格不会立即触发，规则应保持激活状态")

    for i in range(6):
        time.sleep(5)
        print(f"   ⏱️  {(i + 1) * 5}秒...")

    # 11. 再次检查规则状态
    print("\n🔍 再次检查风险规则状态...")
    rules = get_risk_rules(token)
    target_rule = next((rule for rule in rules if rule['symbol'] == test_symbol), None)

    if not target_rule:
        pytest.fail("风险规则丢失")

    final_status = target_rule['enabled']
    print(f"📋 最终规则状态: {'🟢 激活' if final_status else '🔴 禁用'}")

    # 12. 检查是否有新的订单生成
    print("\n📋 检查订单历史...")
    orders = get_orders(token)
    recent_orders = [order for order in orders if order['symbol'] == test_symbol]
    if recent_orders:
        print(f"   发现{len(recent_orders)}个{test_symbol}相关订单")
        for order in recent_orders[-3:]:  # 显示最近3个订单
            print(f"   - {order['action']} {order['quantity']} {order['symbol']} @ {order.get('price', 'MKT')}")
    else:
        print(f"   没有发现{test_symbol}相关的新订单")

    # 13. 验证结果
    assert final_status, (
        f"风险规则被意外禁用: 初始状态={'激活' if initial_status else '禁用'}, 最终状态={'激活' if final_status else '禁用'}"
    )
    print("\n✅ 测试成功！风险规则在正常情况下保持激活状态")


if __name__ == "__main__":
    try:
        test_risk_rule_activation_with_real_positions()
    except pytest.SkipTest as exc:  # pragma: no cover - 手动运行时的提示
        print(f"⚠️ 测试被跳过: {exc}")
    except AssertionError as exc:  # pragma: no cover - 手动运行时的提示
        print(f"❌ 测试失败: {exc}")
    else:
        print("🎉 真实IBKR Paper账户风险规则激活功能测试通过！")
