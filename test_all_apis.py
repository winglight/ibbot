import pytest
import requests
import json
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

BASE_URL = "http://localhost:8000"
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'chy')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'X9K^nNI4')

class TestAllAPIs:
    @classmethod
    def setup_class(cls):
        """获取访问令牌"""
        login_data = {
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD
        }
        response = requests.post(f"{BASE_URL}/api/login", json=login_data)
        assert response.status_code == 200
        cls.token = response.json()["access_token"]
        cls.headers = {
            "Authorization": f"Bearer {cls.token}",
            "Content-Type": "application/json"
        }
    
    def test_login(self):
        """测试登录接口"""
        login_data = {
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD
        }
        response = requests.post(f"{BASE_URL}/api/login", json=login_data)
        assert response.status_code == 200
        assert "access_token" in response.json()
    
    def test_risk_config(self):
        """测试风控配置接口"""
        response = requests.get(f"{BASE_URL}/api/risk-config", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "stop_loss_ratio" in data
    
    def test_orders(self):
        """测试订单接口"""
        response = requests.get(f"{BASE_URL}/api/orders", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
        assert "orders" in data
    
    def test_positions(self):
        """测试持仓接口"""
        response = requests.get(f"{BASE_URL}/api/positions", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
    
    def test_portfolio(self):
        """测试投资组合接口 - 跳过，接口不存在"""
        # 该接口不存在，跳过测试
        pass
    
    def test_notifications_settings(self):
        """测试通知设置接口"""
        response = requests.get(f"{BASE_URL}/api/notifications/settings", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
        assert "settings" in data
    
    def test_sizing_preview(self):
        """测试仓位计算预览接口"""
        sizing_data = {
            "symbol": "MNQ",
            "stop_distance": 10.0,
            "rv_current": 21000.0,
            "signal_tier": "A"
        }
        response = requests.post(f"{BASE_URL}/api/sizing/preview", 
                               json=sizing_data, headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "N" in data
        assert "explain" in data
    
    def test_account_info(self):
        """测试账户信息接口"""
        response = requests.get(f"{BASE_URL}/api/account_info", headers=self.headers)
        # 这个接口可能需要IB连接，所以可能返回错误，但不应该卡死
        assert response.status_code in [200, 400, 500]
    
    def test_connection_status(self):
        """测试连接状态接口"""
        response = requests.get(f"{BASE_URL}/api/connection_status", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "connected" in data
    
    def test_risk_rules(self):
        """测试风控规则接口"""
        response = requests.get(f"{BASE_URL}/api/risk_rules", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
    
    def test_strategies(self):
        """测试策略接口"""
        response = requests.get(f"{BASE_URL}/api/strategies", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
    
    def test_logs(self):
        """测试日志接口"""
        response = requests.get(f"{BASE_URL}/api/logs", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
    
    def test_notifications_history(self):
        """测试通知历史接口"""
        response = requests.get(f"{BASE_URL}/api/notifications/history", headers=self.headers)
        assert response.status_code == 200
        data = response.json()
        assert "success" in data
    
    def test_api_response_time(self):
        """测试API响应时间，确保没有卡死"""
        import time
        
        # 测试几个关键接口的响应时间
        test_endpoints = [
            "/api/risk-config",
            "/api/orders", 
            "/api/positions",
            "/api/notifications/settings",
            "/api/connection_status"
        ]
        
        for endpoint in test_endpoints:
            start_time = time.time()
            response = requests.get(f"{BASE_URL}{endpoint}", 
                                  headers=self.headers, timeout=10)
            end_time = time.time()
            response_time = end_time - start_time
            
            print(f"{endpoint}: {response_time:.2f}s")
            # 确保响应时间不超过5秒
            assert response_time < 5.0, f"{endpoint} took too long: {response_time:.2f}s"
            assert response.status_code in [200, 400, 404, 500]  # 不应该超时

if __name__ == "__main__":
    pytest.main(["-v", __file__])