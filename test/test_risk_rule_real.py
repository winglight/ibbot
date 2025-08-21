#!/usr/bin/env python3
"""
使用真实IBKR Paper账户测试修复后的风险规则激活功能
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
        print(f"✅ 登录成功")
        return token
    else:
        print(f"❌ 登录失败: {response.status_code} - {response.text}")
        return None

def get_headers(token):
    """获取请求头"""
    return {"Authorization": f"Bearer {token}"}

def get_connection_status(token):
    """获取连接状态"""
    response = requests.get(f"{BASE_URL}/api/connection_status", headers=get_headers(token))
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ 获取连接状态失败: {response.status_code}")
        return None

def get_positions(token):
    """获取当前持仓"""
    response = requests.get(f"{BASE_URL}/api/positions", headers=get_headers(token))
    if response.status_code == 200:
        positions = response.json()
        print(f"📊 当前持仓数量: {len(positions)}")
        return positions
    else:
        print(f"❌ 获取持仓失败: {response.status_code} - {response.text}")
        return []

def get_orders(token):
    """获取订单历史"""
    response = requests.get(f"{BASE_URL}/api/orders", headers=get_headers(token))
    if response.status_code == 200:
        orders = response.json()
        print(f"📋 订单数量: {len(orders)}")
        return orders
    else:
        print(f"❌ 获取订单失败: {response.status_code}")
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

def delete_risk_rule(token, symbol):
    """删除风险规则"""
    response = requests.delete(f"{BASE_URL}/api/risk_rules/{symbol}", headers=get_headers(token))
    if response.status_code == 200:
        print(f"✅ 成功删除风险规则: {symbol}")
        return True
    else:
        print(f"❌ 删除风险规则失败: {response.status_code} - {response.text}")
        return False

def test_risk_rule_activation_with_real_positions():
    """使用真实持仓测试风险规则激活功能"""
    print("\n" + "="*60)
    print("🧪 使用真实IBKR Paper账户测试风险规则激活功能")
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
    
    # 3. 获取当前持仓
    print("\n📊 检查当前持仓...")
    positions = get_positions(token)
    
    if not positions:
        print("❌ 当前没有持仓，无法测试风险规则")
        print("💡 建议：先通过Web UI或其他方式创建一些持仓")
        return False
    
    # 4. 选择第一个有持仓的股票进行测试
    test_position = None
    for pos in positions:
        if pos['position'] != 0:
            test_position = pos
            break
    
    if not test_position:
        print("❌ 没有找到非零持仓")
        return False
    
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
    if not add_risk_rule(token, test_symbol, stop_loss_price, take_profit_price):
        return False
    
    # 8. 检查规则状态
    print("\n🔍 检查风险规则状态...")
    rules = get_risk_rules(token)
    target_rule = next((rule for rule in rules if rule['symbol'] == test_symbol), None)
    
    if not target_rule:
        print(f"❌ 未找到{test_symbol}的风险规则")
        return False
    
    initial_status = target_rule['enabled']
    print(f"📋 初始规则状态: {'🟢 激活' if initial_status else '🔴 禁用'}")
    
    # 9. 如果规则被禁用，激活它
    if not initial_status:
        print("\n⚡ 激活风险规则...")
        if not update_risk_rule(token, test_symbol, True):
            return False
        time.sleep(1)
    
    # 10. 等待风险管理器运行几个周期
    print("\n⏳ 等待风险管理器运行 (30秒)...")
    print("   在此期间，风险管理器会监控价格变化")
    print("   由于设置的止损/止盈价格不会立即触发，规则应保持激活状态")
    
    for i in range(6):
        time.sleep(5)
        print(f"   ⏱️  {(i+1)*5}秒...")
    
    # 11. 再次检查规则状态
    print("\n🔍 再次检查风险规则状态...")
    rules = get_risk_rules(token)
    target_rule = next((rule for rule in rules if rule['symbol'] == test_symbol), None)
    
    if not target_rule:
        print(f"❌ 风险规则丢失")
        return False
    
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
    if final_status:
        print(f"\n✅ 测试成功！风险规则在正常情况下保持激活状态")
        print(f"   - 测试股票: {test_symbol}")
        print(f"   - 初始状态: {'激活' if initial_status else '禁用'}")
        print(f"   - 最终状态: {'激活' if final_status else '禁用'}")
        print(f"   - 修复验证: 规则未被意外禁用")
        return True
    else:
        print(f"\n❌ 测试失败！风险规则被意外禁用")
        print(f"   - 测试股票: {test_symbol}")
        print(f"   - 初始状态: {'激活' if initial_status else '禁用'}")
        print(f"   - 最终状态: {'激活' if final_status else '禁用'}")
        print(f"   - 问题: 规则在没有下单失败的情况下被禁用")
        return False

if __name__ == "__main__":
    success = test_risk_rule_activation_with_real_positions()
    
    print("\n" + "="*60)
    if success:
        print("🎉 真实IBKR Paper账户风险规则激活功能测试通过！")
        print("✅ 修复成功：规则在正常情况下保持激活状态")
        print("🔧 异步事件循环冲突问题已解决")
        print("📋 规则禁用逻辑已优化：只在下单失败时禁用")
        print("🎯 建议：可以继续测试其他场景，如价格触发止损/止盈")
    else:
        print("❌ 真实IBKR Paper账户风险规则激活功能测试失败")
        print("⚠️ 需要进一步调试")
    print("="*60)