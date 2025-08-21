#!/usr/bin/env python3
"""
测试API连接和基本功能
"""

import requests
import json

# API配置
BASE_URL = "http://127.0.0.1:8000"
USERNAME = "chy"
PASSWORD = "X9$K^nNI4"

def test_api_connection():
    """测试API连接"""
    print("🔍 测试API连接...")
    
    # 1. 测试登录
    print("\n1. 测试登录...")
    try:
        response = requests.post(f"{BASE_URL}/api/login", json={
            "username": USERNAME,
            "password": PASSWORD
        }, timeout=10)
        
        if response.status_code == 200:
            token = response.json()["access_token"]
            print(f"✅ 登录成功")
        else:
            print(f"❌ 登录失败: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ 登录异常: {e}")
        return False
    
    headers = {"Authorization": f"Bearer {token}"}
    
    # 2. 测试连接状态
    print("\n2. 测试连接状态...")
    try:
        response = requests.get(f"{BASE_URL}/api/connection_status", headers=headers, timeout=10)
        if response.status_code == 200:
            status = response.json()
            print(f"✅ 连接状态: {json.dumps(status, indent=2)}")
        else:
            print(f"❌ 获取连接状态失败: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ 连接状态异常: {e}")
        return False
    
    # 3. 测试持仓获取
    print("\n3. 测试持仓获取...")
    try:
        response = requests.get(f"{BASE_URL}/api/positions", headers=headers, timeout=10)
        if response.status_code == 200:
            positions = response.json()
            print(f"✅ 持仓获取成功，数量: {len(positions)}")
            if positions:
                for pos in positions[:3]:  # 只显示前3个
                    print(f"   {pos['symbol']}: {pos['position']} 股")
        else:
            print(f"❌ 获取持仓失败: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ 持仓获取异常: {e}")
        return False
    
    # 4. 测试风险规则获取
    print("\n4. 测试风险规则获取...")
    try:
        response = requests.get(f"{BASE_URL}/api/risk_rules", headers=headers, timeout=10)
        if response.status_code == 200:
            rules = response.json()
            print(f"✅ 风险规则获取成功，数量: {len(rules)}")
            if rules:
                for rule in rules[:3]:  # 只显示前3个
                    status = "激活" if rule['enabled'] else "禁用"
                    print(f"   {rule['symbol']}: {status}")
        else:
            print(f"❌ 获取风险规则失败: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ 风险规则获取异常: {e}")
        return False
    
    print("\n✅ 所有API测试通过！")
    return True

if __name__ == "__main__":
    print("="*50)
    print("🧪 API连接测试")
    print("="*50)
    
    success = test_api_connection()
    
    print("\n" + "="*50)
    if success:
        print("🎉 API连接测试通过！")
        print("✅ 系统运行正常，可以进行风险规则测试")
    else:
        print("❌ API连接测试失败")
        print("⚠️ 需要检查系统状态")
    print("="*50)