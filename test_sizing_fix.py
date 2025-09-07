#!/usr/bin/env python3
"""
测试 sizing preview 接口的连接状态检查修复
"""

import requests
import json

def test_sizing_preview_without_connection():
    """
    测试在没有 IB 连接的情况下调用 sizing preview 接口
    应该返回 400 错误而不是 500 错误
    """
    url = "http://127.0.0.1:8000/api/sizing/preview"
    
    # 测试数据
    test_data = {
        "symbol": "AAPL",
        "action": "BUY",
        "order_type": "LMT",
        "quantity": 100,
        "price": 150.0
    }
    
    try:
        response = requests.post(url, json=test_data, timeout=10)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")
        
        if response.status_code == 400:
            response_data = response.json()
            if "Not connected to IBKR" in response_data.get("detail", ""):
                print("✅ 修复成功：正确返回 400 错误，提示未连接到 IBKR")
                return True
            else:
                print(f"❌ 返回了 400 错误，但错误信息不正确: {response_data}")
                return False
        elif response.status_code == 500:
            print("❌ 修复失败：仍然返回 500 内部服务器错误")
            return False
        else:
            print(f"❌ 意外的状态码: {response.status_code}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求失败: {e}")
        return False

def test_connection_status():
    """
    测试连接状态接口
    """
    url = "http://127.0.0.1:8000/api/connection_status"
    
    try:
        response = requests.get(url, timeout=5)
        print(f"\n连接状态检查:")
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"Connected: {data.get('connected', 'unknown')}")
            print(f"Account Type: {data.get('account_type', 'unknown')}")
        else:
            print(f"Response: {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ 连接状态检查失败: {e}")

if __name__ == "__main__":
    print("开始测试 sizing preview 接口修复...\n")
    
    # 先检查连接状态
    test_connection_status()
    
    # 测试 sizing preview
    print("\n测试 sizing preview 接口:")
    success = test_sizing_preview_without_connection()
    
    if success:
        print("\n🎉 所有测试通过！sizing preview 接口修复成功。")
    else:
        print("\n❌ 测试失败，需要进一步检查。")