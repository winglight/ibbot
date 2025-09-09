"""
External Strategy API Tests
测试外部策略API接口：令牌生成、设置配置、/api/strategy调用、队列状态检查、执行记录验证
"""

import pytest
import requests
import json
import time
import uuid
import os
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# 在导入环境变量后立即加载 .env 文件
load_dotenv()

# 测试配置
BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')
TEST_ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
TEST_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")

# 测试常量
TEST_STRATEGY_NAME = "test_external_strategy"
TEST_SYMBOL = "AAPL"
TEST_QUANTITY = 100


def get_auth_token() -> str:
    """获取认证令牌"""
    response = requests.post(
        f"{BASE_URL}/api/login",
        headers={"Content-Type": "application/json"},
        json={"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def api_get(endpoint: str, token: str, params: Dict[str, Any] = None) -> requests.Response:
    """API GET请求辅助函数"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    return requests.get(f"{BASE_URL}{endpoint}", headers=headers, params=params)


def api_post(endpoint: str, token: str, data: Dict[str, Any] = None) -> requests.Response:
    """API POST请求辅助函数"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    return requests.post(f"{BASE_URL}{endpoint}", headers=headers, json=data)


def api_put(endpoint: str, token: str, data: Dict[str, Any] = None) -> requests.Response:
    """API PUT请求辅助函数"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    return requests.put(f"{BASE_URL}{endpoint}", headers=headers, json=data)


def api_delete(endpoint: str, token: str) -> requests.Response:
    """API DELETE请求辅助函数"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    return requests.delete(f"{BASE_URL}{endpoint}", headers=headers)


class TestExternalStrategyAPI:
    """外部策略API测试类"""

    @pytest.fixture(autouse=True)
    def setup_and_cleanup(self):
        """测试前设置和测试后清理"""
        self.token = get_auth_token()
        
        # 确保开启策略接口认证
        api_post("/api/strategy/settings", self.token, {"enable_auth": True})
        
        # 清理可能存在的测试策略
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}", self.token)
        
        yield
        
        # 测试后清理
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}", self.token)
        api_delete(f"/api/strategies/{TEST_STRATEGY_NAME}/executions", self.token)

    def test_generate_strategy_token(self):
        """测试策略令牌生成"""
        response = api_post("/api/strategy/settings/generate-token", self.token)
        assert response.status_code == 200
        
        data = response.json()
        assert "token" in data
        assert isinstance(data["token"], str)
        assert len(data["token"]) > 0

    def test_update_strategy_settings(self):
        """测试策略设置更新"""
        settings_data = {
            "api_endpoint": f"{BASE_URL}/webhook",
            "api_method": "POST",
            "api_payload_template": {"symbol": "{{symbol}}", "action": "{{action}}"},
            "enable_auth": True
        }
        
        response = api_post("/api/strategy/settings", self.token, settings_data)
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_get_strategy_settings(self):
        """测试获取策略设置"""
        response = api_get("/api/strategy/settings", self.token)
        assert response.status_code == 200
        
        data = response.json()
        assert "success" in data
        assert "settings" in data
        assert data["success"] is True

    def test_create_external_strategy(self):
        """测试创建外部策略"""
        strategy_data = {
            "name": TEST_STRATEGY_NAME,
            "strategy_type": "external",
            "enabled": True,
            "api_endpoint": "/api/strategy"
        }
        
        response = api_post("/api/strategies", self.token, strategy_data)
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_get_strategy_info(self):
        """测试获取策略信息"""
        # 先创建策略
        self.test_create_external_strategy()
        
        response = api_get(f"/api/strategies/{TEST_STRATEGY_NAME}", self.token)
        assert response.status_code == 200
        
        data = response.json()
        assert data["success"] is True
        assert "strategy" in data
        strategy = data["strategy"]
        assert strategy["name"] == TEST_STRATEGY_NAME
        assert strategy["strategy_type"] == "external"
        assert strategy["enabled"] is True

    def test_external_strategy_order_with_token_in_body(self):
        """测试外部策略订单请求 - 令牌在请求体中"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 生成策略令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        order_data = {
            "strategy": TEST_STRATEGY_NAME,
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET",
            "token": strategy_token
        }
        
        response = requests.post(
            f"{BASE_URL}/api/strategy",
            headers={"Content-Type": "application/json"},
            json=order_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True
        assert "task_id" in data

    def test_external_strategy_order_with_auth_header(self):
        """测试外部策略订单请求 - 使用Authorization头"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 生成策略令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        order_data = {
            "strategy": TEST_STRATEGY_NAME,
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET"
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {strategy_token}"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/strategy",
            headers=headers,
            json=order_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True
        assert "task_id" in data

    def test_external_strategy_order_with_custom_header(self):
        """测试外部策略订单请求 - 使用自定义头X-Strategy-Token"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 生成策略令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        order_data = {
            "strategy": TEST_STRATEGY_NAME,
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET"
        }
        
        headers = {
            "Content-Type": "application/json",
            "X-Strategy-Token": strategy_token
        }
        
        response = requests.post(
            f"{BASE_URL}/api/strategy",
            headers=headers,
            json=order_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True
        assert "task_id" in data

    def test_external_strategy_order_invalid_token(self):
        """测试外部策略订单请求 - 无效令牌"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 在本用例内显式开启策略接口认证，确保启用
        api_post("/api/strategy/settings", self.token, {"enable_auth": True})
        # 生成一个有效令牌（不使用它，只是确保服务端有一个auth_token用于对比）
        api_post("/api/strategy/settings/generate-token", self.token)
        
        # 读取并断言服务端当前策略设置
        settings_resp = api_get("/api/strategy/settings", self.token)
        assert settings_resp.status_code == 200
        settings_data = settings_resp.json().get("settings", {})
        assert settings_data.get("enable_auth") is True
        assert settings_data.get("auth_token") not in (None, "")
        
        order_data = {
            "strategy": TEST_STRATEGY_NAME,
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET"
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer invalid_token"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/strategy",
            headers=headers,
            json=order_data
        )
        
        assert response.status_code == 401

    def test_external_strategy_order_missing_strategy(self):
        """测试外部策略订单请求 - 策略不存在或未启用"""
        # 先确保策略接口要求认证
        api_post("/api/strategy/settings", self.token, {"enable_auth": True})
        
        # 生成有效令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        order_data = {
            "strategy": "nonexistent_strategy",
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET",
            "token": strategy_token
        }
        
        response = requests.post(
            f"{BASE_URL}/api/strategy",
            headers={"Content-Type": "application/json"},
            json=order_data
        )
        
        assert response.status_code == 400

    def test_external_strategy_order_disabled_strategy(self):
        """测试外部策略订单请求 - 策略已禁用"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 禁用策略
        api_post(f"/api/strategies/{TEST_STRATEGY_NAME}/toggle?enabled=false", self.token)
        
        # 生成有效令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        order_data = {
            "strategy": TEST_STRATEGY_NAME,
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET",
            "token": strategy_token
        }
        
        response = requests.post(
            f"{BASE_URL}/api/strategy",
            headers={"Content-Type": "application/json"},
            json=order_data
        )
        
        assert response.status_code == 400

    def test_queue_status_check(self):
        """测试队列状态检查"""
        response = api_get("/api/queue/status", self.token)
        assert response.status_code == 200
        
        data = response.json()
        # 匹配 queue_manager.get_queue_status 返回的字段
        assert "pending_count" in data
        assert "processing_count" in data
        assert "completed_count" in data
        assert "failed_count" in data
        assert "total_tasks" in data
        assert "is_running" in data
        assert isinstance(data["pending_count"], int)
        assert isinstance(data["processing_count"], int)
        assert isinstance(data["completed_count"], int)
        assert isinstance(data["failed_count"], int)
        assert isinstance(data["total_tasks"], int)
        assert isinstance(data["is_running"], bool)

    def test_task_status_check(self):
        """测试任务状态检查"""
        # 创建策略并提交订单以获取task_id
        self.test_create_external_strategy()
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        order_data = {
            "strategy": TEST_STRATEGY_NAME,
            "symbol": TEST_SYMBOL,
            "action": "BUY",
            "qty": TEST_QUANTITY,
            "type": "MARKET",
            "token": strategy_token
        }
        
        queue_resp = requests.post(
            f"{BASE_URL}/api/strategy",
            headers={"Content-Type": "application/json"},
            json=order_data
        )
        assert queue_resp.status_code == 200
        task_id = queue_resp.json()["task_id"]
        
        # 查询任务状态
        resp = api_get(f"/api/queue/task/{task_id}", self.token)
        assert resp.status_code == 200
        task_data = resp.json()
        assert task_data.get("task_id") == task_id
        assert task_data.get("status") in ["pending", "processing", "completed", "failed"]

    def test_strategy_executions_list(self):
        """测试策略执行记录列表"""
        # 先创建策略
        self.test_create_external_strategy()
        
        response = api_get(f"/api/strategies/{TEST_STRATEGY_NAME}/executions", self.token)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True
        assert "executions" in data
        assert isinstance(data["executions"], list)

    def test_strategy_executions_delete(self):
        """测试清除策略执行记录"""
        # 先创建策略
        self.test_create_external_strategy()
        
        response = requests.delete(
            f"{BASE_URL}/api/strategies/{TEST_STRATEGY_NAME}/executions",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True

    def test_strategy_performance_stats(self):
        """测试策略性能统计接口"""
        # 先创建策略
        self.test_create_external_strategy()
        response = api_get(f"/api/strategies/{TEST_STRATEGY_NAME}/performance", self.token)
        assert response.status_code == 200
        
        data = response.json()
        assert "metrics" in data

    def test_invalid_order_parameters(self):
        """测试无效订单参数"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 生成策略令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        # 测试缺少必需字段
        invalid_orders = [
            # 缺少symbol
            {
                "strategy": TEST_STRATEGY_NAME,
                "action": "BUY",
                "qty": TEST_QUANTITY,
                "type": "MARKET",
                "token": strategy_token
            },
            # 缺少action
            {
                "strategy": TEST_STRATEGY_NAME,
                "symbol": TEST_SYMBOL,
                "qty": TEST_QUANTITY,
                "type": "MARKET",
                "token": strategy_token
            },
            # 限价单缺少价格
            {
                "strategy": TEST_STRATEGY_NAME,
                "symbol": TEST_SYMBOL,
                "action": "BUY",
                "qty": TEST_QUANTITY,
                "type": "LIMIT",
                "token": strategy_token
            }
        ]
        
        for invalid_order in invalid_orders:
            response = requests.post(
                f"{BASE_URL}/api/strategy",
                headers={"Content-Type": "application/json"},
                json=invalid_order
            )
            # 应该返回400或422状态码
            assert response.status_code in [400, 422]

    def test_concurrent_strategy_orders(self):
        """测试并发策略订单"""
        # 先创建策略
        self.test_create_external_strategy()
        
        # 生成策略令牌
        token_response = api_post("/api/strategy/settings/generate-token", self.token)
        strategy_token = token_response.json()["token"]
        
        orders = []
        for i in range(5):
            orders.append({
                "strategy": TEST_STRATEGY_NAME,
                "symbol": TEST_SYMBOL,
                "action": "BUY" if i % 2 == 0 else "SELL",
                "qty": TEST_QUANTITY,
                "type": "MARKET",
                "token": strategy_token
            })
        
        task_ids = []
        for order in orders:
            resp = requests.post(
                f"{BASE_URL}/api/strategy",
                headers={"Content-Type": "application/json"},
                json=order
            )
            assert resp.status_code == 200
            task_ids.append(resp.json().get("task_id"))
        
        # 简单检查任务ID唯一性
        assert len(set(task_ids)) == len(task_ids)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])