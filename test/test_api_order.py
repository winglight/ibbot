#!/usr/bin/env python3
"""
通过API测试手动下单功能修复
"""

import requests
import json
import time
import os
from loguru import logger
from dotenv import load_dotenv

# 加载.env文件
load_dotenv()


def get_auth_token():
    """获取认证token"""
    base_url = os.getenv('API_BASE_URL', 'http://localhost:8000')
    username = os.getenv('ADMIN_USERNAME', 'admin')
    password = os.getenv('ADMIN_PASSWORD', 'change_me')
    
    auth_data = {
        "username": username,
        "password": password
    }
    
    try:
        response = requests.post(f"{base_url}/api/login", json=auth_data, timeout=10)
        if response.status_code == 200:
            result = response.json()
            return result.get('access_token')
        else:
            logger.error(f"登录失败: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"登录时发生错误: {e}")
        return None


def is_connected(token: str):
    """检查连接状态"""
    try:
        base_url = os.getenv('API_BASE_URL', 'http://localhost:8000')
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{base_url}/api/connection_status", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get('connected', False)
        return False
    except Exception as e:
        logger.error(f"检查连接状态异常: {e}")
        return False


def test_order_via_api():
    """通过API测试下单功能"""
    base_url = os.getenv('API_BASE_URL', 'http://localhost:8000')
    
    logger.info("开始通过API测试下单功能...")
    
    # 获取认证token
    token = get_auth_token()
    assert token, "无法获取认证token"
    
    # 检查连接状态
    connected = is_connected(token)
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # 测试下限价单
    order_data = {
        "symbol": "AAPL",
        "quantity": 1,
        "price": 100.0,  # 远低于市价，不会成交
        "action": "BUY",
        "order_type": "LIMIT"
    }
    
    try:
        logger.info(f"发送下单请求: {order_data}")
        response = requests.post(f"{base_url}/api/place_order", json=order_data, headers=headers, timeout=30)
        
        logger.info(f"响应状态码: {response.status_code}")
        logger.info(f"响应内容: {response.text}")
        
        if not connected:
            # 未连接时应返回400
            assert response.status_code == 400 and 'Not connected' in response.text, \
                f"未连接时应返回400, 实际{response.status_code}, body={response.text}"
        else:
            # 已连接时应返回200
            assert response.status_code == 200, f"已连接时期望200, 实际{response.status_code}, body={response.text}"
            
            result = {}
            try:
                result = response.json() if response.text.strip() else {}
            except Exception:
                # 即使非JSON，也要求状态码为200
                result = {}
                
            if result:
                assert result.get("success"), f"API下单失败: {result}"
                order_id = result.get("order_id")
                if order_id:
                    logger.info(f"订单ID: {order_id}")
                    
                    # 等待一下然后尝试取消订单
                    time.sleep(2)
                    cancel_response = requests.post(f"{base_url}/api/cancel_order", json={"order_id": order_id}, headers=headers)
                    logger.info(f"取消订单响应: {cancel_response.status_code} - {cancel_response.text}")
            else:
                # 无详细响应也视为成功
                logger.info("✅ API下单请求成功（无详细响应）")
            
    except Exception as e:
        logger.error(f"API测试时发生错误: {e}")
        raise


if __name__ == "__main__":
    logger.info("=== API下单功能修复测试 ===")
    try:
        test_order_via_api()
        logger.info("✅ API下单功能测试通过")
    except Exception:
        logger.error("❌ API下单功能测试失败")
        raise
    finally:
        logger.info("测试完成")