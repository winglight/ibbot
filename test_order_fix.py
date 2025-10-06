#!/usr/bin/env python3
"""
测试订单列表错误修复的脚本
"""
import requests
import json

def test_order_api():
    base_url = "http://localhost:8001"
    
    print("测试订单列表API修复...")
    
    # 测试1: 无效的日期格式 - 应该返回400错误
    print("\n1. 测试无效日期格式 'string':")
    try:
        response = requests.get(
            f"{base_url}/api/orders",
            params={"limit": 5, "page": 1, "start_date": "string"},
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
    
    # 测试2: 有效的日期格式
    print("\n2. 测试有效日期格式:")
    try:
        response = requests.get(
            f"{base_url}/api/orders",
            params={"limit": 5, "page": 1, "start_date": "2024-01-01T00:00:00"},
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
    
    # 测试3: 不带日期参数
    print("\n3. 测试不带日期参数:")
    try:
        response = requests.get(
            f"{base_url}/api/orders",
            params={"limit": 5, "page": 1},
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")

if __name__ == "__main__":
    test_order_api()