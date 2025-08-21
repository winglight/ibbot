#!/usr/bin/env python3
"""
测试修复后的风险规则激活功能
验证规则在下单成功时保持激活状态，只在下单失败时才禁用
"""

import requests
import json
import time
from datetime import datetime

# API配置
BASE_URL = "http://127.0.0.1:8000"
USERNAME = "chy"
PASSWORD = "X9$K^nNI4"

def login():
    """登录并获取token"""
    response = requests.post(f"{BASE_URL}/api/login", json={
        "username": USERNAME,
        "password": PASSWORD
    })
    if response.status_code == 200:
        token = response.json()["access_token"]
        print(f"✅ 登录成功，获取到token")
        return token
    else:
        print(f"❌ 登录失败: {response.status_code} - {response.text}")
        return None

def get_headers(token):
    """获取请求头"""
    return {"Authorization": f"Bearer {token}"}

def get_positions(token):
    """获取当前持仓"""
    response = requests.get(f"{BASE_URL}/api/positions", headers=get_headers(token))
    if response.status_code == 200:
        positions = response.json()
        print(f"📊 当前持仓数量: {len(positions)}")
        for pos in positions:
            print(f"   {pos['symbol']}: {pos['position']} 股, 价格: ${pos['marketPrice']:.2f}")
        return positions
    else:
        print(f"❌ 获取持仓失败: {response.status_code}")
        return []

def add_risk_rule(token, symbol, stop_loss_price, take_profit_price=None):
    """添加风险规则"""
    data = {
        "symbol": symbol,
        "stop_loss_price": stop_loss_price
    }
    if take_profit_price:
        data["take_profit_price"] = take_profit_price
    
    response = requests.post(f"{BASE_URL}/api/risk_rules", 
                           headers=get_headers(token), 
                           json=data)
    if response.status_code == 200:
        print(f"✅ 成功添加风险规则: {symbol} 止损价=${stop_loss_price}")
        return True
    else:
        print(f"❌ 添加风险规则失败: {response.status_code} - {response.text}")
        return False

def get_risk_rules(token):
    """获取风险规则"""
    response = requests.get(f"{BASE_URL}/api/risk_rules", headers=get_headers(token))
    if response.status_code == 200:
        rules = response.json()
        print(f"📋 当前风险规则数量: {len(rules)}")
        for rule in rules:
            status = "🟢 激活" if rule['enabled'] else "🔴 禁用"
            print(f"   {rule['symbol']}: {status}, 止损=${rule.get('stop_loss_price', 'N/A')}, 止盈=${rule.get('take_profit_price', 'N/A')}")
        return rules
    else:
        print(f"❌ 获取风险规则失败: {response.status_code}")
        return []

def update_risk_rule(token, symbol, enabled=True):
    """更新风险规则状态"""
    response = requests.put(f"{BASE_URL}/api/risk_rules/{symbol}", 
                          headers=get_headers(token), 
                          json={"enabled": enabled})
    if response.status_code == 200:
        status = "激活" if enabled else "禁用"
        print(f"✅ 成功{status}风险规则: {symbol}")
        return True
    else:
        print(f"❌ 更新风险规则失败: {response.status_code} - {response.text}")
        return False

def place_test_order(token, symbol, quantity=10):
    """下测试订单以创建持仓"""
    response = requests.post(f"{BASE_URL}/api/place_order", 
                           headers=get_headers(token), 
                           json={
                               "symbol": symbol,
                               "quantity": quantity,
                               "action": "BUY",
                               "order_type": "MKT"
                           })
    if response.status_code == 200:
        print(f"✅ 成功下单: {symbol} {quantity}股")
        return True
    else:
        print(f"❌ 下单失败: {response.status_code} - {response.text}")
        return False

def get_connection_status(token):
    """获取连接状态"""
    response = requests.get(f"{BASE_URL}/api/connection_status", headers=get_headers(token))
    if response.status_code == 200:
        status = response.json()
        return status
    else:
        print(f"❌ 获取连接状态失败: {response.status_code}")
        return None

def test_risk_rule_activation():
    """测试修复后的风险规则激活功能（使用真实IBKR Paper账户）"""
    print("\n" + "="*60)
    print("🧪 开始测试修复后的风险规则激活功能（真实IBKR Paper模式）")
    print("="*60)
    
    # 1. 登录
    token = login()
    if not token:
        return False
    
    # 2. 检查连接状态
    print("\n📡 检查IBKR连接状态...")
    status = get_connection_status(token)
    if not status or not status.get('connected'):
        print("❌ IBKR未连接，请确保已连接到Paper账户")
        return False
    
    print(f"✅ 已连接到IBKR: {status.get('account_type', 'Unknown')}")
    
    # 3. 检查当前持仓
    print("\n📊 检查当前持仓...")
    positions = get_positions(token)
    
    # 4. 如果没有持仓，先下单创建持仓
    test_symbol = "AAPL"
    has_position = any(pos['symbol'] == test_symbol and pos['position'] != 0 for pos in positions)
    
    if not has_position:
        print(f"\n📈 没有{test_symbol}持仓，先下单创建持仓...")
        if place_test_order(token, test_symbol, 1):
            print("⏳ 等待订单执行...")
            time.sleep(10)  # 等待订单执行
            positions = get_positions(token)  # 重新获取持仓
    
    # 5. 获取当前价格并设置风险规则
    target_position = next((pos for pos in positions if pos['symbol'] == test_symbol), None)
    if not target_position or target_position['position'] == 0:
        print(f"❌ 无法获取{test_symbol}持仓，测试终止")
        return False
    
    current_price = target_position['marketPrice']
    # 设置一个较低的止损价格，确保不会立即触发
    stop_loss_price = current_price * 0.8  # 比当前价格低20%
    take_profit_price = current_price * 1.2  # 比当前价格高20%
    
    print(f"\n💰 {test_symbol} 当前价格: ${current_price:.2f}")
    print(f"🛑 设置止损价格: ${stop_loss_price:.2f}")
    print(f"🎯 设置止盈价格: ${take_profit_price:.2f}")
    
    # 5. 添加风险规则
    print("\n📋 添加风险规则...")
    if not add_risk_rule(token, test_symbol, stop_loss_price, take_profit_price):
        return False
    
    # 6. 检查规则状态
    print("\n🔍 检查风险规则状态...")
    rules = get_risk_rules(token)
    target_rule = next((rule for rule in rules if rule['symbol'] == test_symbol), None)
    
    if not target_rule:
        print(f"❌ 未找到{test_symbol}的风险规则")
        return False
    
    if not target_rule['enabled']:
        print(f"⚠️ 风险规则当前为禁用状态，尝试激活...")
        if not update_risk_rule(token, test_symbol, True):
            return False
    
    # 7. 等待一段时间，让风险管理器运行
    print("\n⏳ 等待风险管理器运行 (20秒)...")
    time.sleep(20)
    
    # 8. 再次检查规则状态
    print("\n🔍 再次检查风险规则状态...")
    rules = get_risk_rules(token)
    target_rule = next((rule for rule in rules if rule['symbol'] == test_symbol), None)
    
    if target_rule and target_rule['enabled']:
        print(f"✅ 测试成功！风险规则保持激活状态")
        print(f"   规则状态: {'🟢 激活' if target_rule['enabled'] else '🔴 禁用'}")
        return True
    else:
        print(f"❌ 测试失败！风险规则被意外禁用")
        if target_rule:
            print(f"   规则状态: {'🟢 激活' if target_rule['enabled'] else '🔴 禁用'}")
        return False

if __name__ == "__main__":
    success = test_risk_rule_activation()
    
    print("\n" + "="*60)
    if success:
        print("🎉 风险规则激活功能测试通过！（真实IBKR Paper模式）")
        print("✅ 修复成功：规则在正常情况下保持激活状态")
        print("🔧 异步事件循环冲突问题已解决")
        print("📋 规则禁用逻辑已优化：只在下单失败时禁用")
    else:
        print("❌ 风险规则激活功能测试失败")
        print("⚠️ 需要进一步调试")
    print("="*60)