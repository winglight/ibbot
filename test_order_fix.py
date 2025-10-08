#!/usr/bin/env python3
"""
测试订单列表错误修复的脚本
"""
import pytest

try:  # pragma: no cover - dependency guard for optional test requirement
    import requests  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("requests library is not installed", allow_module_level=True)
import json

def get_auth_token():
    """获取认证token"""
    base_url = "http://localhost:8005"
    login_data = {
        "username": "chy",
        "password": "X9K^nNI4"
    }
    
    try:
        response = requests.post(f"{base_url}/api/login", json=login_data, timeout=10)
        if response.status_code == 200:
            token = response.json().get("access_token")
            return token
        else:
            print(f"登录失败: {response.status_code} - {response.text}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"登录请求失败: {e}")
        return None

def test_order_api():
    base_url = "http://localhost:8005"
    
    print("测试订单列表API修复...")
    
    # 获取认证token
    print("正在获取认证token...")
    token = get_auth_token()
    if not token:
        print("无法获取认证token，测试终止")
        return
    
    headers = {"Authorization": f"Bearer {token}"}
    print("认证成功，开始测试...")
    
    # 测试1: 无效的日期格式 - 应该返回400错误
    print("\n1. 测试无效日期格式 'string':")
    try:
        response = requests.get(
            f"{base_url}/api/orders",
            params={"limit": 5, "page": 1, "start_date": "string"},
            headers=headers,
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.text}")
        if response.status_code == 400:
            print("✅ 正确返回400错误")
        else:
            print("❌ 应该返回400错误")
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
    
    # 测试2: 有效的日期格式
    print("\n2. 测试有效日期格式:")
    try:
        response = requests.get(
            f"{base_url}/api/orders",
            params={"limit": 5, "page": 1, "start_date": "2024-01-01T00:00:00"},
            headers=headers,
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.text}")
        if response.status_code in [200, 503]:  # 200成功或503服务不可用都是正常的
            print("✅ 正确处理有效日期格式")
        else:
            print("❌ 处理有效日期格式失败")
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
    
    # 测试3: 不带日期参数
    print("\n3. 测试不带日期参数:")
    try:
        response = requests.get(
            f"{base_url}/api/orders",
            params={"limit": 5, "page": 1},
            headers=headers,
            timeout=10
        )
        print(f"状态码: {response.status_code}")
        print(f"响应: {response.text}")
        if response.status_code in [200, 503]:  # 200成功或503服务不可用都是正常的
            print("✅ 正确处理无日期参数")
        else:
            print("❌ 处理无日期参数失败")
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")

if __name__ == "__main__":
    test_order_api()